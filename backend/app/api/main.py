# -*- coding: utf-8 -*-
"""
Screenwright 编排后端(FastAPI)。

职责：把已实现的离线管线(ingest -> bible -> segment -> generate -> annotate ->
metrics)与导出能力编排成 HTTP/SSE API，供前端工作台调用。

设计要点(为什么这么做)：
- 这一层只做"编排 + 传输 + 安全"，不重写任何业务逻辑。所有原子能力都从
  app.pipeline.* / app.schema.models / app.llm.client import，避免代码重复。
- LLM 调用是阻塞同步的；FastAPI 事件循环是单线程异步的。直接在协程里调阻塞函数
  会卡死整个事件循环(所有并发请求都被堵)。所以 /api/convert 把整条同步管线丢进
  线程池(asyncio.to_thread)跑，事件循环只负责把进度事件推给客户端。
- 隐私(复用上批教训)：用户上传的小说原文绝不写日志、不落盘。密钥只在服务端经
  get_llm 从环境变量读取，绝不出现在任何响应体或错误信息里。
- 安全：公网端点必须有 CORS 白名单 + 限流，防滥用刷 token。
"""

import asyncio
import json
import os
import tempfile
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field, ValidationError
from sse_starlette.sse import EventSourceResponse

# 业务能力全部从既有模块 import(不复制实现)。
from app.llm.client import get_llm
from app.pipeline import ingest as ingest_mod
from app.pipeline import bible as bible_mod
from app.pipeline import segment as segment_mod
from app.pipeline import generate as generate_mod
from app.pipeline import continuity as continuity_mod
from app.pipeline import metrics as metrics_mod
from app.pipeline import export as export_mod
from app.pipeline.types import SceneStub
from app.schema.models import Screenplay, Meta, SourceMeta, SourceRef, Span


# ----------------------------------------------------------------------------
# 应用实例 + 中间件
# ----------------------------------------------------------------------------

app = FastAPI(title="Screenwright API", version="1.0")


def _parse_origins() -> List[str]:
    """
    解析 CORS 允许来源。

    从环境变量 ALLOWED_ORIGINS 读，逗号分隔；缺省 "*"(开发期放开)。
    用环境变量而非硬编码，是为了部署到 VM 时只放行真实前端域名，收紧攻击面。
    """
    raw = os.environ.get("ALLOWED_ORIGINS", "*")
    parts = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            parts.append(item)
    if not parts:
        parts = ["*"]
    return parts


app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------------
# 限流：内存滑动窗口，对 /api/convert 按 IP 限速
# ----------------------------------------------------------------------------

class SlidingWindowLimiter:
    """
    进程内滑动窗口限流器(复用上批"公网端点必加限流"的教训)。

    为什么用滑动窗口而非令牌桶：实现简单、行为直观——"每 IP 在过去 window 秒内
    最多 max_requests 次"。对单实例后端足够；多实例需换 Redis，此处不过度设计。

    为什么只锁 /api/convert：它是唯一会触发大量 LLM 调用(烧 token)的端点，是滥用
    的主要面。health/sample/export 都是廉价只读，不必限流。

    线程安全：限流计数可能被事件循环线程并发读写，用 Lock 保护 deque。
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # ip -> 最近请求时间戳队列。
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """
        判断 key(IP)此刻是否允许放行。允许则记一次命中并返回 True；否则 False。
        """
        now = time.monotonic()
        boundary = now - self.window_seconds
        with self._lock:
            q = self._hits[key]
            # 弹出窗口外的旧时间戳。
            while q and q[0] < boundary:
                q.popleft()
            if len(q) >= self.max_requests:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        """清空所有计数。测试用，避免相邻用例互相污染。"""
        with self._lock:
            self._hits.clear()


# 阈值可经环境变量调；缺省每 IP 60 秒内 10 次 convert。
_CONVERT_LIMIT = int(os.environ.get("CONVERT_RATE_LIMIT", "10"))
_CONVERT_WINDOW = float(os.environ.get("CONVERT_RATE_WINDOW", "60"))
convert_limiter = SlidingWindowLimiter(_CONVERT_LIMIT, _CONVERT_WINDOW)


def _client_ip(request: Request) -> str:
    """
    取客户端 IP 作为限流 key。

    优先 X-Forwarded-For 第一跳(部署在 Caddy 反代后，真实 IP 在这个头里)，
    回退到 socket 对端地址。注意：仅用于限流分桶，不写日志、不落盘。
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    if request.client is not None:
        return request.client.host
    return "unknown"


# ----------------------------------------------------------------------------
# 请求体模型
# ----------------------------------------------------------------------------

class ConvertRequest(BaseModel):
    """/api/convert 入参。"""
    text: str
    title: Optional[str] = None
    medium: str = "film"


class RegenerateRequest(BaseModel):
    """/api/regenerate_scene 入参。"""
    screenplay: dict
    scene_id: str
    instruction: Optional[str] = None
    medium: Optional[str] = None
    # 可选原文：若提供，用它重建 Novel 以便精确切出本场原文做溯源。
    # 不提供时退化为基于 bible+stub 生成(编辑安全，不动其他场)。
    text: Optional[str] = None


class ExportRequest(BaseModel):
    """/api/export 入参。"""
    screenplay: dict
    format: str = "yaml"


# ----------------------------------------------------------------------------
# 端点 1：健康检查
# ----------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    """存活探针。部署后给反代/监控用。"""
    return {"status": "ok"}


# ----------------------------------------------------------------------------
# 端点 2：内置样本
# ----------------------------------------------------------------------------

# samples 目录：backend/samples。本文件在 backend/app/api/main.py，上溯三级到 backend。
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SAMPLES_DIR = os.path.join(_BACKEND_DIR, "samples")

# 样本清单：id / 标题 / 文件名。固定两份(中文网文 + 英文 P&P)。
_SAMPLE_FILES = [
    {
        "id": "zh_oldtown_cafe",
        "title": "旧城咖啡",
        "filename": "中文网文样本_旧城咖啡.txt",
    },
    {
        "id": "en_pride_prejudice",
        "title": "Pride and Prejudice (Ch.1-3)",
        "filename": "english_pride_and_prejudice_ch1-3.txt",
    },
]


@app.get("/api/sample")
def sample() -> List[dict]:
    """
    返回内置样本(一键试用)。读 backend/samples 下两份文本。

    读不到的样本静默跳过(不让缺文件拖垮整个端点)，但正常环境两份都在。
    """
    out: List[dict] = []
    for item in _SAMPLE_FILES:
        path = os.path.join(_SAMPLES_DIR, item["filename"])
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        out.append({"id": item["id"], "title": item["title"], "text": text})
    return out


# ----------------------------------------------------------------------------
# 端点 3：转换(SSE 进度流)
# ----------------------------------------------------------------------------

_VALID_MEDIA = {"film", "series", "short_drama"}


def _run_pipeline_blocking(text: str, title: Optional[str], medium: str, emit) -> dict:
    """
    同步跑整条管线，每完成一个 Pass 用 emit 回调推一个进度事件。

    这是纯阻塞函数，必须在线程池里调用(见 convert)。emit 是线程安全的进度发射器
    (内部把事件塞进 asyncio 队列)。

    返回最终 done 事件的 payload(含 screenplay + metrics)。

    隐私：text 只在本函数内存里流转，不写日志、不落盘。
    """
    llm = get_llm()

    # Pass0 ingest。
    emit({"stage": "ingest", "detail": "解析小说、分章分块", "pct": 5})
    novel = ingest_mod.ingest(text, title=title)

    # Pass1 bible。
    emit({"stage": "bible", "detail": "抽取人物/地点/时间线(单一事实源)", "pct": 25})
    story_bible = bible_mod.build_bible(novel, llm=llm)

    # Pass2 segment。
    emit({"stage": "segment", "detail": "场景切分 + 行级溯源", "pct": 45})
    stubs = segment_mod.segment(novel, story_bible, llm=llm)

    # Pass3 generate(最重的一步)。
    emit({"stage": "generate", "detail": "逐场生成剧本(内心戏外化)", "pct": 60})
    scenes = generate_mod.generate(novel, story_bible, stubs, medium=medium, llm=llm)

    # 组装顶层 Screenplay。
    chapters = [c.index for c in novel.chapters]
    meta = Meta(
        title=(title or novel.title),
        source=SourceMeta(type="novel", chapters=chapters),
        target_medium=medium,
    )
    sp = Screenplay(meta=meta, story_bible=story_bible, scenes=scenes)

    # Pass4 annotate(连贯性检查回填)。
    emit({"stage": "annotate", "detail": "连贯性检查、冲突标注", "pct": 85})
    sp = continuity_mod.annotate(sp)

    # Pass5 metrics(量化看板)。
    emit({"stage": "metrics", "detail": "计算质量指标", "pct": 95})
    m = metrics_mod.compute_metrics(sp, novel)

    return {
        "stage": "done",
        "screenplay": sp.model_dump(by_alias=True),
        "metrics": m,
    }


@app.post("/api/convert")
async def convert(req: ConvertRequest, request: Request):
    """
    小说 -> 剧本，SSE 流式返回进度。

    流程：
      - 校验入参(text 非空、medium 合法)，限流。
      - 起一个后台线程跑同步管线，线程通过线程安全队列把进度事件投递回事件循环。
      - 事件循环这边的异步生成器从队列取事件、yield 给 EventSourceResponse。
      - 管线抛异常时推一个 error 事件并结束流(不泄露堆栈/密钥)。

    用"线程 + 队列"而非简单 asyncio.to_thread 包整条管线，是因为我们要在管线
    每个 Pass 完成时就把进度推给前端(渐进反馈)，而不是等全跑完才出一个结果。
    """
    # 校验：medium 合法。
    if req.medium not in _VALID_MEDIA:
        return JSONResponse(
            status_code=400,
            content={"detail": "medium 必须是 film/series/short_drama 之一"},
        )
    # 校验：text 非空。
    if not req.text or not req.text.strip():
        return JSONResponse(status_code=400, content={"detail": "text 不能为空"})

    # 限流：按 IP。超阈值直接 429，不消耗 token。
    ip = _client_ip(request)
    if not convert_limiter.allow(ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "请求过于频繁，请稍后再试"},
        )

    loop = asyncio.get_running_loop()
    # 线程安全的事件队列：管线线程 put，事件循环 get。
    queue: asyncio.Queue = asyncio.Queue()
    # 哨兵：标记流结束。
    SENTINEL = object()

    def emit(event: dict) -> None:
        """供管线线程调用，把进度事件投递回事件循环(线程安全)。"""
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def worker() -> None:
        """后台线程体：跑同步管线，结果/错误都通过队列回传。"""
        try:
            done = _run_pipeline_blocking(req.text, req.title, req.medium, emit)
            loop.call_soon_threadsafe(queue.put_nowait, done)
        except Exception as exc:  # noqa: BLE001
            # 只回送一句简短信息，绝不回送堆栈/密钥/原文。
            msg = str(exc)
            if len(msg) > 200:
                msg = msg[:200]
            err = {"stage": "error", "detail": "转换失败: " + msg}
            loop.call_soon_threadsafe(queue.put_nowait, err)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, SENTINEL)

    # 起后台线程(daemon，进程退出不被它挂住)。
    t = threading.Thread(target=worker, daemon=True)
    t.start()

    async def event_gen():
        """SSE 事件生成器：从队列取事件，逐个 yield。"""
        while True:
            event = await queue.get()
            if event is SENTINEL:
                break
            # EventSourceResponse 约定：yield dict，data 字段为字符串。
            yield {"data": json.dumps(event, ensure_ascii=False)}

    return EventSourceResponse(event_gen())


# ----------------------------------------------------------------------------
# 端点 4：单场重生成(编辑安全)
# ----------------------------------------------------------------------------

def _rebuild_novel(screenplay: Screenplay, raw_text: Optional[str]):
    """
    为重生成重建一个 Novel。

    优先用前端回传的原文 raw_text(经 ingest 还原精确章文本，溯源最准)。
    没有原文时退化：用空字符串占位每章文本，generate 的切片会得到空原文，
    此时生成依赖 bible 切片 + stub 梗概(仍能产出合法场，且绝不动其他场)。
    """
    if raw_text and raw_text.strip():
        return ingest_mod.ingest(raw_text, title=screenplay.meta.title)

    # 退化路径：按 meta.source.chapters 造占位章。
    from app.pipeline.types import Novel, Chapter

    chapters = []
    for idx in screenplay.meta.source.chapters:
        chapters.append(Chapter(index=idx, title="第%d章" % idx, text=""))
    if not chapters:
        chapters.append(Chapter(index=1, title="第1章", text=""))
    return Novel(title=screenplay.meta.title, raw="", chapters=chapters)


@app.post("/api/regenerate_scene")
def regenerate_scene(req: RegenerateRequest):
    """
    只重生成指定场，返回更新后的该 Scene(dict)。

    编辑安全(创新加分项)：本端点只接受并返回**单场**，绝不触碰其他场。前端拿到
    新场后自行替换本地 screenplay.scenes 里对应 id 的那一项，其余场原样保留。

    上下文重建：从回传 screenplay 取出目标场 -> 重建 bible/novel/SceneStub ->
    调 generate_scene。instruction(用户编辑指令)拼进 stub 梗概，温和影响生成。
    """
    # 1. 校验整份 screenplay 合法(同时把 dict 变成强类型对象)。
    try:
        sp = Screenplay.model_validate(req.screenplay)
    except ValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"detail": "screenplay 不合法: " + str(exc.error_count()) + " 处校验错误"},
        )

    # 2. 定位目标场。
    target = None
    for sc in sp.scenes:
        if sc.id == req.scene_id:
            target = sc
            break
    if target is None:
        return JSONResponse(
            status_code=400,
            content={"detail": "找不到 scene_id=" + req.scene_id},
        )

    # 3. 重建上下文。
    novel = _rebuild_novel(sp, req.text)
    medium = req.medium or sp.meta.target_medium

    # 4. 由目标场反推 SceneStub(场级 source_ref 直接复用)。
    summary = target.synopsis or ""
    if req.instruction:
        # 把用户编辑指令拼进梗概，作为生成提示(温和、可控)。
        summary = (summary + " [编辑要求] " + req.instruction).strip()
    stub = SceneStub(
        id=target.id,
        chapter_index=target.source_ref.chapter,
        source_ref=target.source_ref,
        characters=list(target.characters),
        summary=summary,
        time_of_day=target.heading.time_of_day,
        location_hint=target.heading.location_id,
    )

    # 5. 调单场生成。失败不泄露堆栈/密钥。
    try:
        new_scene = generate_mod.generate_scene(
            stub=stub,
            novel=novel,
            bible=sp.story_bible,
            medium=medium,
            prev_tail="",
            llm=get_llm(),
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if len(msg) > 200:
            msg = msg[:200]
        return JSONResponse(status_code=400, content={"detail": "重生成失败: " + msg})

    # 只返回这一场(编辑安全)。
    return new_scene.model_dump(by_alias=True)


# ----------------------------------------------------------------------------
# 端点 5：导出
# ----------------------------------------------------------------------------

_VALID_FORMATS = {"yaml", "fountain", "pdf"}


@app.post("/api/export")
def export(req: ExportRequest):
    """
    导出剧本为 yaml / fountain / pdf。

    yaml/fountain 直接返回文本(media_type 适配)；pdf 写临时文件用 FileResponse 返回。
    先 model_validate 把 dict 收敛成合法 Screenplay，再调对应导出器。
    """
    fmt = req.format
    if fmt not in _VALID_FORMATS:
        return JSONResponse(
            status_code=400,
            content={"detail": "format 必须是 yaml/fountain/pdf 之一"},
        )

    # 校验 + 强类型化。
    try:
        sp = Screenplay.model_validate(req.screenplay)
    except ValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"detail": "screenplay 不合法: " + str(exc.error_count()) + " 处校验错误"},
        )

    if fmt == "yaml":
        text = export_mod.to_yaml(sp)
        # YAML 用 text/yaml；前端可直接下载/展示。
        return PlainTextResponse(content=text, media_type="application/x-yaml")

    if fmt == "fountain":
        text = export_mod.to_fountain(sp)
        return PlainTextResponse(content=text, media_type="text/plain")

    # pdf：写临时文件再用 FileResponse 返回。
    tmp = tempfile.NamedTemporaryFile(prefix="screenwright_", suffix=".pdf", delete=False)
    tmp_path = tmp.name
    tmp.close()
    export_mod.to_pdf(sp, tmp_path)
    filename = "screenplay.pdf"
    return FileResponse(tmp_path, media_type="application/pdf", filename=filename)
