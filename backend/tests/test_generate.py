# -*- coding: utf-8 -*-
"""
generate 模块测试(Pass3：逐场剧本生成)。

测试策略：主测全程用 fake llm，离线、确定、不联网。fake llm 预设返回一个
含 action + dialogue + 一条外化 dialogue 的场景 JSON，覆盖三大创新点的代码侧落实：

  ① 媒介注入：断言 short_drama 时 prompt(fake llm 收到的 messages)里含短剧风格指令。
  ② 行级溯源：某元素带命中原文的 source_quote，断言其 source_ref 落在 chapter.text 上自洽；
     某元素带定位不到的 source_quote，断言 source_ref=None(允许缺失)。
  ③ 内心戏外化：外化元素带 from=interior_monologue，断言 adaptation.from_ 正确落字段。

另含：对白 character 经名字映射成 bible id；elements 有序且类型正确；Scene 通过 pydantic 校验。
可选在线冒烟测试在有真实 key 时跑一场真生成。
"""

import os

import pytest

from app.schema.models import (
    Scene,
    StoryBible,
    Character,
    Location,
    ActionElement,
    DialogueElement,
)
from app.schema.models import SourceRef, Span
from app.pipeline.types import Novel, Chapter, SceneStub
from app.pipeline.generate import generate_scene, generate


# ---------------------------------------------------------------------------
# 测试夹具：一章原文 + 最小 bible + 一个 stub
# ---------------------------------------------------------------------------

# 这段原文里："林深推开门" 是会被 action 命中的逐字片段；
# "他心里清楚，自己其实从未放下" 是内心戏(将被外化)。
_CHAPTER_TEXT = (
    "林深推开门，看见沈言坐在靠窗的位置。"
    "他心里清楚，自己其实从未放下。"
    "“好久不见。”他说。"
)


def _make_novel() -> Novel:
    """造一部单章小说，chapter.text 即上面的原文(偏移相对它)。"""
    chapter = Chapter(index=1, title="第一章 测试", text=_CHAPTER_TEXT)
    return Novel(title="测试小说", raw=_CHAPTER_TEXT, chapters=[chapter])


def _make_bible() -> StoryBible:
    """造最小 bible：两个角色(林深/沈言)，一个地点。"""
    return StoryBible(
        characters=[
            Character(id="char_lin", name="林深", aliases=["小林"], traits=["内敛"]),
            Character(id="char_shen", name="沈言", aliases=[], traits=["平静"]),
        ],
        locations=[Location(id="loc_cafe", name="旧城咖啡馆")],
        timeline=[],
    )


def _make_stub() -> SceneStub:
    """造一个覆盖整章的 stub(spans 覆盖 [0, len(text)])。"""
    n = len(_CHAPTER_TEXT)
    return SceneStub(
        id="sc_001",
        chapter_index=1,
        source_ref=SourceRef(chapter=1, spans=[Span(start=0, end=n)]),
        characters=["char_lin", "char_shen"],
        summary="林深与沈言重逢",
        time_of_day="日",
        location_hint="咖啡馆",
    )


# ---------------------------------------------------------------------------
# fake llm：记录收到的 messages，返回预设场景 JSON
# ---------------------------------------------------------------------------

class FakeLLM:
    """
    假 LLM：不联网。complete() 把收到的 messages 存起来供断言，返回预设 dict。

    预设场景覆盖：
      - 一条 action，source_quote 命中原文("林深推开门") -> 应有自洽 source_ref。
      - 一条普通 dialogue，character 用名字"沈言"(测名字->id 映射)，source_quote 命中原文。
      - 一条外化 dialogue，from=interior_monologue + technique=voiceover，
        source_quote 故意给原文里没有的串 -> 应 source_ref=None。
    """

    def __init__(self):
        # 记录最后一次调用的 messages，给"媒介注入"断言用。
        self.last_messages = None

    def complete(self, messages, json=False, model=None, temperature=0.0, **kw):
        # 存下来供测试断言 prompt 内容。
        self.last_messages = messages
        return {
            "heading": {
                "int_ext": "INT",
                "location_id": "loc_cafe",
                "time_of_day": "日",
                "time_ref": None,
            },
            "characters": ["char_lin", "沈言"],
            "synopsis": "林深与沈言在咖啡馆重逢",
            "elements": [
                {
                    "type": "action",
                    "text": "林深推开木门，铜铃轻响。",
                    "source_quote": "林深推开门",
                    "adaptation": None,
                },
                {
                    "type": "dialogue",
                    "character": "沈言",
                    "line": "好久不见。",
                    "parenthetical": None,
                    "source_quote": "“好久不见。”他说。",
                    "adaptation": None,
                },
                {
                    "type": "dialogue",
                    "character": "char_lin",
                    "line": "(画外音)我以为我早就放下了。",
                    "parenthetical": None,
                    # 故意给原文里不存在的逐字串，验证定位失败 -> source_ref=None。
                    "source_quote": "这串原文里根本没有出现过",
                    "adaptation": {"from": "interior_monologue", "technique": "voiceover"},
                },
            ],
        }


# ---------------------------------------------------------------------------
# 主测：generate_scene 三大创新点 + schema 校验
# ---------------------------------------------------------------------------

def test_generate_scene_with_fake_llm():
    novel = _make_novel()
    bible = _make_bible()
    stub = _make_stub()
    fake = FakeLLM()

    scene = generate_scene(stub, novel, bible, medium="film", prev_tail="", llm=fake)

    # 1. 返回的是通过校验的 Scene，id 用 stub.id。
    assert isinstance(scene, Scene)
    assert scene.id == "sc_001"

    # 2. elements 有序且类型正确：action / dialogue / dialogue。
    assert len(scene.elements) == 3
    assert isinstance(scene.elements[0], ActionElement)
    assert isinstance(scene.elements[1], DialogueElement)
    assert isinstance(scene.elements[2], DialogueElement)

    # 3. 对白 character 是 bible id：第二条用名字"沈言"传入，应被映射成 char_shen。
    assert scene.elements[1].character == "char_shen"
    assert scene.elements[2].character == "char_lin"

    # 4. 行级溯源自洽：action 的 source_quote 命中原文，
    #    其 source_ref 落在 chapter.text 上，且切片等于命中片段。
    action = scene.elements[0]
    assert action.source_ref is not None
    assert action.source_ref.chapter == 1
    span = action.source_ref.spans[0]
    assert novel.chapters[0].text[span.start:span.end] == "林深推开门"

    # 普通对白也命中原文，source_ref 自洽。
    normal_dlg = scene.elements[1]
    assert normal_dlg.source_ref is not None
    span2 = normal_dlg.source_ref.spans[0]
    assert novel.chapters[0].text[span2.start:span2.end] == "“好久不见。”他说。"

    # 5. 外化元素：source_quote 定位不到 -> source_ref=None(允许缺失)，
    #    且 adaptation.from_ == interior_monologue(创新点③落字段)。
    ext_dlg = scene.elements[2]
    assert ext_dlg.source_ref is None
    assert ext_dlg.adaptation is not None
    assert ext_dlg.adaptation.from_ == "interior_monologue"
    assert ext_dlg.adaptation.technique == "voiceover"

    # 6. 场级 source_ref 复用 stub 的。
    assert scene.source_ref == stub.source_ref


# ---------------------------------------------------------------------------
# time_of_day 语言：prompt 要求随原文语言 + 兜底默认值随 language 走
# ---------------------------------------------------------------------------

class _FakeLLMNoTimeOfDay:
    """模型回的 heading 故意不给 time_of_day，用来验证代码侧兜底默认值的语言。"""

    def complete(self, messages, json=False, model=None, temperature=0.0, **kw):
        return {
            "heading": {
                "int_ext": "INT",
                "location_id": "loc_cafe",
                # 故意省略 time_of_day。
                "time_ref": None,
            },
            "characters": ["char_lin"],
            "synopsis": "测试兜底时段",
            "elements": [
                {"type": "action", "text": "林深推开门。", "source_quote": "林深推开门", "adaptation": None},
            ],
        }


def test_prompt_requires_time_of_day_in_source_language():
    """prompt 必须明确要求 time_of_day 用本场原文语言书写(英文实证的根因修复)。"""
    novel = _make_novel()
    bible = _make_bible()
    stub = _make_stub()
    fake = FakeLLM()

    generate_scene(stub, novel, bible, medium="film", prev_tail="", llm=fake)
    joined = "\n".join([m["content"] for m in fake.last_messages])
    # 关键约束词：time_of_day 随原文语言；英文用 DAY/NIGHT 等。
    assert "time_of_day" in joined
    assert "原文相同的语言" in joined
    assert "DAY" in joined


def test_time_of_day_fallback_default_chinese():
    """language 不传(默认)时，模型漏填时段兜底为中文 日(既有行为不变)。"""
    novel = _make_novel()
    bible = _make_bible()
    # stub 不给 time_of_day，逼出最终的语言相关默认值。
    n = len(_CHAPTER_TEXT)
    stub = SceneStub(
        id="sc_001",
        chapter_index=1,
        source_ref=SourceRef(chapter=1, spans=[Span(start=0, end=n)]),
        characters=["char_lin"],
    )
    scene = generate_scene(stub, novel, bible, medium="film", llm=_FakeLLMNoTimeOfDay())
    assert scene.heading.time_of_day == "日"


class _FakeLLMChineseTimeOfDay:
    """模型对英文原文却回了中文时段(实测 LLM 偶发)，验证英文路径会归一成英文。"""

    def complete(self, messages, json=False, model=None, temperature=0.0, **kw):
        return {
            "heading": {
                "int_ext": "INT",
                "location_id": "loc_cafe",
                "time_of_day": "黄昏",   # 故意回中文(英文输入下应被归一为 DUSK)。
                "time_ref": None,
            },
            "characters": ["char_lin"],
            "synopsis": "test",
            "elements": [
                {"type": "action", "text": "He pushes the door open.", "source_quote": "", "adaptation": None},
            ],
        }


def test_time_of_day_chinese_normalized_to_english_when_en():
    """language='en' 时，模型回的中文时段被归一成英文 slugline 词。"""
    novel = _make_novel()
    bible = _make_bible()
    n = len(_CHAPTER_TEXT)
    stub = SceneStub(
        id="sc_001",
        chapter_index=1,
        source_ref=SourceRef(chapter=1, spans=[Span(start=0, end=n)]),
        characters=["char_lin"],
    )
    scene = generate_scene(
        stub, novel, bible, medium="film", llm=_FakeLLMChineseTimeOfDay(), language="en"
    )
    # "黄昏" -> "DUSK"。
    assert scene.heading.time_of_day == "DUSK"


def test_time_of_day_chinese_preserved_when_zh():
    """默认(中文)路径不归一：模型回的中文时段原样保留(中文行为零影响)。"""
    novel = _make_novel()
    bible = _make_bible()
    n = len(_CHAPTER_TEXT)
    stub = SceneStub(
        id="sc_001",
        chapter_index=1,
        source_ref=SourceRef(chapter=1, spans=[Span(start=0, end=n)]),
        characters=["char_lin"],
    )
    scene = generate_scene(
        stub, novel, bible, medium="film", llm=_FakeLLMChineseTimeOfDay()
    )
    # 中文路径不动，保留 "黄昏"。
    assert scene.heading.time_of_day == "黄昏"


def test_time_of_day_fallback_default_english():
    """language='en' 时，模型漏填时段兜底为英文 DAY，而非中文。"""
    novel = _make_novel()
    bible = _make_bible()
    n = len(_CHAPTER_TEXT)
    stub = SceneStub(
        id="sc_001",
        chapter_index=1,
        source_ref=SourceRef(chapter=1, spans=[Span(start=0, end=n)]),
        characters=["char_lin"],
    )
    scene = generate_scene(
        stub, novel, bible, medium="film", llm=_FakeLLMNoTimeOfDay(), language="en"
    )
    assert scene.heading.time_of_day == "DAY"


# ---------------------------------------------------------------------------
# 媒介注入测试：short_drama 风格指令进了 prompt
# ---------------------------------------------------------------------------

def test_medium_short_drama_injected_into_prompt():
    novel = _make_novel()
    bible = _make_bible()
    stub = _make_stub()
    fake = FakeLLM()

    generate_scene(stub, novel, bible, medium="short_drama", prev_tail="", llm=fake)

    # fake 记录了收到的 messages；把所有内容拼起来检查短剧关键词。
    assert fake.last_messages is not None
    joined = "\n".join([m["content"] for m in fake.last_messages])
    # 短剧风格指令的特征词(来自 _MEDIUM_STYLE["short_drama"])。
    assert "短剧" in joined
    assert "钩子" in joined
    assert "反转" in joined
    # 反证：不应注入电影专属的措辞特征(确认是按媒介切换的，不是恒定文案)。
    assert "目标媒介=短剧(short_drama)" in joined
    assert "目标媒介=电影(film)" not in joined


def test_medium_film_injected_into_prompt():
    """对照组：film 时注入电影风格，不应出现短剧标识。"""
    novel = _make_novel()
    bible = _make_bible()
    stub = _make_stub()
    fake = FakeLLM()

    generate_scene(stub, novel, bible, medium="film", prev_tail="", llm=fake)

    joined = "\n".join([m["content"] for m in fake.last_messages])
    assert "目标媒介=电影(film)" in joined
    assert "目标媒介=短剧(short_drama)" not in joined


# ---------------------------------------------------------------------------
# 驱动器 generate：多场顺序 + prev_tail 传递
# ---------------------------------------------------------------------------

def test_generate_driver_passes_prev_tail():
    novel = _make_novel()
    bible = _make_bible()
    n = len(_CHAPTER_TEXT)
    stub1 = SceneStub(
        id="sc_001",
        chapter_index=1,
        source_ref=SourceRef(chapter=1, spans=[Span(start=0, end=n)]),
        characters=["char_lin"],
    )
    stub2 = SceneStub(
        id="sc_002",
        chapter_index=1,
        source_ref=SourceRef(chapter=1, spans=[Span(start=0, end=n)]),
        characters=["char_lin"],
    )
    fake = FakeLLM()

    scenes = generate(novel, bible, [stub1, stub2], medium="film", llm=fake)

    # 两场都生成，id 对应。
    assert [s.id for s in scenes] == ["sc_001", "sc_002"]

    # 第二场调用时 prev_tail 非空(上一场最后元素文本被注入了)。
    joined = "\n".join([m["content"] for m in fake.last_messages])
    assert "上一场结尾" in joined
    # 上一场最后一个元素是外化对白台词，其文本应出现在 prev_tail 注入中。
    assert "我以为我早就放下了" in joined


# ---------------------------------------------------------------------------
# 可选在线冒烟：有真实 key 时真跑一场(默认 skip，不联网)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.getenv("DEEPSEEK_API_KEY"),
    reason="无 DEEPSEEK_API_KEY，跳过在线冒烟测试",
)
def test_online_smoke_real_llm():
    from app.llm.client import get_llm

    novel = _make_novel()
    bible = _make_bible()
    stub = _make_stub()

    scene = generate_scene(stub, novel, bible, medium="short_drama", llm=get_llm())

    # 只断言出了合法 Scene 且有元素，不对内容做强断言(模型输出有随机性)。
    assert isinstance(scene, Scene)
    assert scene.id == "sc_001"
    assert len(scene.elements) >= 1
