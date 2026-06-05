# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# scripts/precompute_samples.py —— 预计算内置样例的完整结果(一次性脚本)
#
# 目的：对两个内置样例(中文《旧城咖啡》short_drama / 英文 P&P film)各真跑一遍完整
#       离线管线(ingest -> bible -> segment -> generate -> annotate -> metrics)，
#       把结果固化成静态 JSON 存到 backend/samples/precomputed/。
#
# 为什么要预计算：公网用户路径已对 LLM 关磁盘缓存(隐私：用户原文不落盘)，若"加载样例"
#       还走真实 convert，每次都得真跑几十秒，demo 秒出体感没了。内置样例不是用户隐私，
#       所以离线真跑一次、结果落盘，运行时 /api/sample/{id}/result 直接读 JSON 秒回。
#
# 输出形状(必须与 /api/convert 的 done 事件一致，前端无缝渲染)：
#   {
#     "stage": "done",
#     "screenplay": <Screenplay.model_dump(by_alias=True)>,
#     "metrics": <compute_metrics(...) 返回的 dict>,
#     "chapters": [{"index", "title", "text"}, ...]
#   }
#
# 运行前必须 source 密钥(DEEPSEEK_* 等)，本脚本不硬编码任何密钥。
# 用法：source <密钥文件> && python3 scripts/precompute_samples.py
# ----------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import sys
import time

# --- sys.path 修正：把 backend 目录加进来，保证 `import app.*` 能成功 ---------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
_BACKEND_DIR = os.path.join(_REPO_ROOT, "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

# --- 业务模块 import(必须在 sys.path 修正之后) -------------------------------
from app.pipeline.ingest import ingest
from app.pipeline.bible import build_bible
from app.pipeline.segment import segment
from app.pipeline.generate import generate
from app.pipeline.continuity import annotate
from app.pipeline.metrics import compute_metrics
from app.schema.models import Screenplay, Meta, SourceMeta
from app.llm.client import LLM


# 样例清单：id / 样本文件名 / 目标媒介。
# 与 backend/app/api/main.py 的 _SAMPLE_FILES 对齐(id 必须一致，端点据此找 JSON)。
_SAMPLES = [
    {
        "id": "zh_oldtown_cafe",
        "filename": "中文网文样本_旧城咖啡.txt",
        "medium": "short_drama",
    },
    {
        "id": "en_pride_prejudice",
        "filename": "english_pride_and_prejudice_ch1-3.txt",
        "medium": "film",
    },
]

_SAMPLES_DIR = os.path.join(_BACKEND_DIR, "samples")
_OUT_DIR = os.path.join(_SAMPLES_DIR, "precomputed")


def _run_one(sample: dict, llm: LLM) -> dict:
    """对单个样例真跑整条管线，返回与 convert done 事件一致的 dict。"""
    sample_id = sample["id"]
    medium = sample["medium"]
    path = os.path.join(_SAMPLES_DIR, sample["filename"])
    print("\n==== 预计算样例 %s (medium=%s) ====" % (sample_id, medium))
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    print("样本字符数：%d" % len(text))

    # Pass0 ingest。
    novel = ingest(text)
    print("章数：%d" % len(novel.chapters))

    # Pass1 bible。这里特意传入预计算脚本自己的 llm(可带缓存，离线脚本无隐私约束)。
    bible = build_bible(novel, llm=llm)
    print("人物 %d / 地点 %d / 时间线 %d"
          % (len(bible.characters), len(bible.locations), len(bible.timeline)))

    # Pass2 segment。
    stubs = segment(novel, bible, llm=llm)
    print("场骨架数：%d" % len(stubs))

    # Pass3 generate(最重一步)。
    scenes = generate(novel, bible, stubs, medium=medium, llm=llm)
    print("生成场数：%d" % len(scenes))

    # 组装顶层 Screenplay(与 main.py _run_pipeline_blocking 同款)。
    chapter_indexes = [c.index for c in novel.chapters]
    meta = Meta(
        title=novel.title,
        source=SourceMeta(type="novel", chapters=chapter_indexes),
        target_medium=medium,
    )
    sp = Screenplay(meta=meta, story_bible=bible, scenes=scenes)

    # Pass4 annotate。
    sp = annotate(sp)

    # Pass5 metrics。
    m = compute_metrics(sp, novel)

    # chapters：每章原文一并存(前端双向溯源高亮所需)。
    chapters_out = [
        {"index": c.index, "title": c.title, "text": c.text}
        for c in novel.chapters
    ]

    # 形状严格对齐 convert 的 done 事件：by_alias=True 让 adaptation.from_ 输出为 "from"。
    result = {
        "stage": "done",
        "screenplay": sp.model_dump(by_alias=True),
        "metrics": m,
        "chapters": chapters_out,
    }

    # 自校验：结果能被 Screenplay.model_validate 重新校验，确保前端拿到的是合法剧本。
    Screenplay.model_validate(result["screenplay"])
    print("[OK] %s 自校验通过：%d 场" % (sample_id, len(sp.scenes)))
    return result


def main() -> int:
    os.makedirs(_OUT_DIR, exist_ok=True)
    # 离线脚本：用带缓存的 LLM(省钱省时，无隐私约束，样例非用户数据)。
    llm = LLM(cache=True)

    t0 = time.time()
    for sample in _SAMPLES:
        result = _run_one(sample, llm)
        out_path = os.path.join(_OUT_DIR, sample["id"] + ".json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print("写出 -> %s (%d bytes, %d 场)"
              % (out_path, os.path.getsize(out_path), len(result["screenplay"]["scenes"])))

    print("\n全部预计算完成，总耗时 %.2fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
