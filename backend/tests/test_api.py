# -*- coding: utf-8 -*-
"""
编排后端 API 测试。

测试策略(关键)：绝不打真实 LLM。用 monkeypatch 把管线里会联网的函数
(build_bible / segment / generate / generate_scene)替换成返回固定假数据的桩，
让测试快速、确定、离线。get_llm 也被桩成假对象，避免初始化时找 API key。

覆盖：
  - /api/health 返回 ok。
  - /api/sample 返回两条且都含 text。
  - /api/convert 的 SSE 能收到若干 stage 进度事件，且最后一个是 done(含
    screenplay + metrics)。
  - /api/export yaml 返回非空文本且能被 Screenplay.from_yaml 解析回来。
  - /api/regenerate_scene 返回合法 Scene dict(且只返回一场)。
  - 限流：超阈值返回 429。
  - import 冒烟：from app.api.main import app 不报错。
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.api import main as api_main
from app.schema.models import Screenplay, Scene


# ---------------------------------------------------------------------------
# 固定假数据构造器
# ---------------------------------------------------------------------------

def _fake_bible():
    """造一个最小合法 StoryBible。"""
    from app.schema.models import StoryBible, Character, Location, TimePoint

    return StoryBible(
        characters=[Character(id="char_lin", name="林深")],
        locations=[Location(id="loc_cafe", name="旧城咖啡馆")],
        timeline=[TimePoint(id="tp_1", label="重逢之日", order=1)],
    )


def _fake_scene(scene_id="sc_001"):
    """造一个最小合法 Scene。"""
    from app.schema.models import Heading, SourceRef, Span, ActionElement

    return Scene(
        id=scene_id,
        heading=Heading(int_ext="INT", location_id="loc_cafe", time_of_day="日"),
        source_ref=SourceRef(chapter=1, spans=[Span(start=0, end=10)]),
        characters=["char_lin"],
        synopsis="林深推门而入",
        elements=[ActionElement(type="action", text="林深推开木门。")],
        continuity_flags=[],
    )


def _fake_stub(novel, bible, llm=None):
    """segment 桩：返回一个 SceneStub。"""
    from app.pipeline.types import SceneStub
    from app.schema.models import SourceRef, Span

    return [
        SceneStub(
            id="sc_001",
            chapter_index=1,
            source_ref=SourceRef(chapter=1, spans=[Span(start=0, end=10)]),
            characters=["char_lin"],
            summary="重逢",
            time_of_day="日",
            location_hint="loc_cafe",
        )
    ]


class _FakeLLM:
    """假 LLM：不联网。被桩进 get_llm，永不真正调用。"""

    def complete(self, messages, json=False, model=None, temperature=0.0, **kw):
        return {}


@pytest.fixture(autouse=True)
def patch_pipeline(monkeypatch):
    """
    自动给每个用例打桩：把会联网的管线函数与 get_llm 全部替换成假实现。

    打桩点选在 main.py 实际引用的模块属性上(main 用 bible_mod.build_bible 形式调用，
    属性在调用时解析，所以 patch 模块属性即可生效)。
    """
    # get_llm：返回假客户端，避免找 API key。
    monkeypatch.setattr(api_main, "get_llm", lambda *a, **k: _FakeLLM())
    # _new_uncached_llm：用户路径(convert/regenerate)现在用关缓存的 LLM 实例。
    # 桩成假客户端，避免测试时真去初始化 OpenAI 客户端/找 API key。
    monkeypatch.setattr(api_main, "_new_uncached_llm", lambda *a, **k: _FakeLLM())

    # build_bible / segment / generate / generate_scene 全部桩掉。
    monkeypatch.setattr(api_main.bible_mod, "build_bible", lambda novel, llm=None: _fake_bible())
    monkeypatch.setattr(api_main.segment_mod, "segment", _fake_stub)
    monkeypatch.setattr(
        api_main.generate_mod,
        "generate",
        lambda novel, bible, stubs, medium="film", llm=None: [_fake_scene("sc_001")],
    )
    monkeypatch.setattr(
        api_main.generate_mod,
        "generate_scene",
        lambda stub, novel, bible, medium="film", prev_tail="", llm=None: _fake_scene(stub.id),
    )

    # 每个用例前重置限流器，避免相互污染。
    api_main.convert_limiter.reset()
    # 重置 sse-starlette 的全局退出事件：它会被绑到首个 TestClient 的事件循环，
    # 同一 client 多次发 SSE 请求(如限流用例)时会触发 "bound to a different event
    # loop"。每个用例前清掉，让它在新循环里重建。
    try:
        from sse_starlette.sse import AppStatus

        AppStatus.should_exit_event = None
    except Exception:
        pass
    yield
    api_main.convert_limiter.reset()


@pytest.fixture()
def client():
    return TestClient(api_main.app)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_sample(client):
    r = client.get("/api/sample")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    for item in data:
        assert "id" in item
        assert "title" in item
        assert item["text"]  # 非空原文


def _parse_sse(raw_text):
    """把 SSE 原始响应文本解析成 event dict 列表。"""
    events = []
    for block in raw_text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                events.append(json.loads(payload))
    return events


def test_convert_sse(client):
    body = {"text": "第一章\n林深推开门，铜铃轻响。", "title": "测试", "medium": "film"}
    r = client.post("/api/convert", json=body)
    assert r.status_code == 200

    events = _parse_sse(r.text)
    # 至少有多个进度事件。
    stages = [e["stage"] for e in events]
    assert len(stages) >= 3
    # 中间有 ingest/bible/generate 等进度。
    assert "ingest" in stages
    assert "generate" in stages
    # 最后一个是 done，且带 screenplay + metrics。
    last = events[-1]
    assert last["stage"] == "done"
    assert "screenplay" in last
    assert "metrics" in last
    # done 事件必须附带 chapters(前端双向溯源高亮所需)，且每章 text 非空。
    assert "chapters" in last
    assert len(last["chapters"]) >= 1
    for ch in last["chapters"]:
        assert "index" in ch
        assert "title" in ch
        assert ch["text"]  # 非空原文
    # screenplay 能被重新校验。
    sp = Screenplay.model_validate(last["screenplay"])
    assert sp.meta.target_medium == "film"
    assert len(sp.scenes) == 1


def test_convert_bad_medium(client):
    r = client.post("/api/convert", json={"text": "x", "medium": "movie"})
    assert r.status_code == 400


def test_convert_empty_text(client):
    r = client.post("/api/convert", json={"text": "   ", "medium": "film"})
    assert r.status_code == 400


def test_convert_text_too_long(client):
    """超长 text 被 pydantic 的 Field(max_length) 拒绝(422)，挡住烧钱/OOM 的 DoS 主面。"""
    huge = "第一章\n" + ("字" * 200_001)
    r = client.post("/api/convert", json={"text": huge, "medium": "film"})
    # pydantic v2 校验失败默认返回 422(请求体不满足模型约束)。
    assert r.status_code == 422


def test_sample_result_precomputed(client, tmp_path, monkeypatch):
    """/api/sample/{id}/result 返回预计算结构(screenplay/metrics/chapters)，秒回、零 LLM。"""
    # 把预计算目录指到临时目录，写一份合法的预计算 JSON。
    monkeypatch.setattr(api_main, "_PRECOMPUTED_DIR", str(tmp_path))
    sp_dict = _make_screenplay_dict()
    payload = {
        "stage": "done",
        "screenplay": sp_dict,
        "metrics": {"scene_count": 1},
        "chapters": [{"index": 1, "title": "第一章", "text": "林深推开木门。"}],
    }
    (tmp_path / "zh_oldtown_cafe.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    r = client.get("/api/sample/zh_oldtown_cafe/result")
    assert r.status_code == 200
    data = r.json()
    assert "screenplay" in data
    assert "metrics" in data
    assert "chapters" in data
    # screenplay 能被重新校验。
    sp = Screenplay.model_validate(data["screenplay"])
    assert len(sp.scenes) == 1


def test_sample_result_unknown_id(client):
    """未知样例 id 返回 404。"""
    r = client.get("/api/sample/not_a_sample/result")
    assert r.status_code == 404


def test_sample_result_missing_file(client, tmp_path, monkeypatch):
    """合法 id 但预计算文件缺失返回 404。"""
    monkeypatch.setattr(api_main, "_PRECOMPUTED_DIR", str(tmp_path))
    r = client.get("/api/sample/en_pride_prejudice/result")
    assert r.status_code == 404


def _make_screenplay_dict():
    """跑一次 convert 拿到一份合法 screenplay dict，给 export/regenerate 用。"""
    from app.schema.models import Meta, SourceMeta, StoryBible

    sp = Screenplay(
        meta=Meta(title="测试", source=SourceMeta(type="novel", chapters=[1])),
        story_bible=_fake_bible(),
        scenes=[_fake_scene("sc_001")],
    )
    return sp.model_dump(by_alias=True)


def test_export_yaml(client):
    body = {"screenplay": _make_screenplay_dict(), "format": "yaml"}
    r = client.post("/api/export", json=body)
    assert r.status_code == 200
    text = r.text
    assert text.strip()  # 非空
    # 能 round-trip 回 Screenplay。
    sp = Screenplay.from_yaml(text)
    assert sp.meta.title == "测试"


def test_export_fountain(client):
    body = {"screenplay": _make_screenplay_dict(), "format": "fountain"}
    r = client.post("/api/export", json=body)
    assert r.status_code == 200
    assert r.text.strip()


def test_export_bad_format(client):
    body = {"screenplay": _make_screenplay_dict(), "format": "docx"}
    r = client.post("/api/export", json=body)
    assert r.status_code == 400


def test_regenerate_scene(client):
    body = {
        "screenplay": _make_screenplay_dict(),
        "scene_id": "sc_001",
        "instruction": "更紧张一些",
        "medium": "short_drama",
    }
    r = client.post("/api/regenerate_scene", json=body)
    assert r.status_code == 200
    scene_dict = r.json()
    # 返回合法 Scene dict(且只返回单场)。
    scene = Scene.model_validate(scene_dict)
    assert scene.id == "sc_001"


def test_regenerate_unknown_scene(client):
    body = {"screenplay": _make_screenplay_dict(), "scene_id": "sc_999"}
    r = client.post("/api/regenerate_scene", json=body)
    assert r.status_code == 400


def test_rate_limit_429(client, monkeypatch):
    """超阈值时 /api/convert 返回 429。"""
    # 把限流阈值压到很小，便于快速触发。
    small = api_main.SlidingWindowLimiter(max_requests=2, window_seconds=60)
    monkeypatch.setattr(api_main, "convert_limiter", small)

    from sse_starlette.sse import AppStatus

    body = {"text": "第一章\n内容", "medium": "film"}
    # 前 2 次放行。每次 SSE 请求后重置全局退出事件，避免它被绑到上一次的事件循环。
    AppStatus.should_exit_event = None
    assert client.post("/api/convert", json=body).status_code == 200
    AppStatus.should_exit_event = None
    assert client.post("/api/convert", json=body).status_code == 200
    AppStatus.should_exit_event = None
    # 第 3 次被限流(429 走的是普通 JSONResponse，不涉及 SSE)。
    r = client.post("/api/convert", json=body)
    assert r.status_code == 429


def test_import_smoke():
    """uvicorn 能 import app(冒烟)。"""
    from app.api.main import app as imported_app

    assert imported_app is not None
