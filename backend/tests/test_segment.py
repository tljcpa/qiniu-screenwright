# -*- coding: utf-8 -*-
"""
segment 模块测试(Pass2 场景切分)。

测试策略：主测全程用 fake llm(不联网)。fake llm 对一章预设返回 2 个场景的
start_marker / end_marker，断言：
1. 切出的 SceneStub 数正确(等于 fake 返回的场景数)。
2. 每个 chapter.text[span.start:span.end] 命中，且包含对应 marker(自洽性)。
3. id 为 sc_001 / sc_002，跨章连续递增。
4. spans 不越界(0<=start<end<=len(text))，start<end。
5. 人物名映射：命中 bible 用其 id，未命中用原名兜底。

兜底测：marker 找不到(LLM 报了原文里不存在的片段)时不崩、spans 仍连续覆盖。

可选在线冒烟：仅当有 DEEPSEEK_API_KEY 时跑，对真实样本 ingest 后传最小
StoryBible 真跑 segment，断言每个 span 自洽。无密钥则 skip。
"""

import os

import pytest

from app.pipeline.segment import segment
from app.pipeline.types import Novel, Chapter
from app.schema.models import StoryBible, Character, Location, TimePoint, SourceRef


# ----------------------------------------------------------------------------
# fake llm：可编程返回的假客户端。它只实现 segment 用到的 complete(messages, json)。
# 用一个队列存预设响应，按调用顺序逐个弹出，从而对每一章给不同的返回。
# ----------------------------------------------------------------------------
class FakeLLM:
    def __init__(self, responses):
        # responses：list[dict]，每个元素是某一次 complete 调用要返回的 JSON dict。
        self._responses = list(responses)
        # 记录被调了几次，供测试断言"每章一次"。
        self.calls = 0

    def complete(self, messages, json=False, model=None, temperature=0.0, **kw):
        # 弹出下一个预设响应。segment 对每章调一次，调用次数应等于章数。
        self.calls += 1
        if not self._responses:
            # 预设用尽时返回空场景，触发 segment 的"整章一场"兜底，不抛错。
            return {"scenes": []}
        return self._responses.pop(0)


# ----------------------------------------------------------------------------
# 构造一章测试用原文。刻意让两场在时间/地点上可区分，并记下用于断言的 marker。
# ----------------------------------------------------------------------------
# 第一场：清晨咖啡馆；第二场：夜晚天台。两段拼成一章正文。
SCENE1_TEXT = "清晨的旧城咖啡馆里，林姐擦着吧台，阳光斜照进来，铜铃在门口轻响。"
SCENE2_TEXT = "入夜后，阿哲独自登上天台，城市的灯火在脚下铺开，他点了一支烟。"
CHAPTER_TEXT = SCENE1_TEXT + SCENE2_TEXT


def _make_novel():
    """造一个单章小说，正文为 CHAPTER_TEXT。"""
    chapter = Chapter(index=1, title="第一章 旧城的清晨与夜", text=CHAPTER_TEXT)
    return Novel(title="旧城咖啡", raw=CHAPTER_TEXT, chapters=[chapter])


def _make_bible():
    """造一个最小 bible：林姐 -> char_lin(别名'林女士')，阿哲 -> char_zhe。"""
    chars = [
        Character(id="char_lin", name="林姐", aliases=["林女士"]),
        Character(id="char_zhe", name="阿哲", aliases=[]),
    ]
    return StoryBible(characters=chars, locations=[], timeline=[])


# 主测用的两场预设响应：marker 都是从 CHAPTER_TEXT 里逐字复制的真实片段。
GOOD_RESPONSE = {
    "scenes": [
        {
            "start_marker": "清晨的旧城咖啡馆里",      # 第一场开头，确在原文
            "end_marker": "铜铃在门口轻响。",          # 第一场结尾，确在原文
            "characters": ["林姐"],
            "summary": "清晨林姐在咖啡馆开店。",
            "time_of_day": "清晨",
            "location_hint": "旧城咖啡馆",
        },
        {
            "start_marker": "入夜后，阿哲独自登上天台",  # 第二场开头，确在原文
            "end_marker": "他点了一支烟。",            # 第二场结尾，确在原文
            "characters": ["阿哲"],
            "summary": "夜晚阿哲在天台独处。",
            "time_of_day": "夜",
            "location_hint": "天台",
        },
    ]
}


def test_segment_basic_with_fake_llm():
    """主测：两场全部 marker 命中，断言数量/自洽/id/越界/人物映射。"""
    novel = _make_novel()
    bible = _make_bible()
    fake = FakeLLM([GOOD_RESPONSE])

    stubs = segment(novel, bible, llm=fake)

    # 单章一次 complete 调用。
    assert fake.calls == 1
    # 切出恰好 2 场。
    assert len(stubs) == 2

    text = CHAPTER_TEXT
    # id 全局连续编号。
    assert stubs[0].id == "sc_001"
    assert stubs[1].id == "sc_002"

    # 第一场自洽性 + marker 命中。
    s0 = stubs[0]
    assert s0.chapter_index == 1
    assert s0.source_ref.chapter == 1
    span0 = s0.source_ref.spans[0]
    # 越界与 start<end。
    assert 0 <= span0.start < span0.end <= len(text)
    sub0 = text[span0.start:span0.end]
    # 取回的原文必须包含该场的 start/end marker。
    assert "清晨的旧城咖啡馆里" in sub0
    assert "铜铃在门口轻响。" in sub0

    # 第二场自洽性 + marker 命中。
    s1 = stubs[1]
    span1 = s1.source_ref.spans[0]
    assert 0 <= span1.start < span1.end <= len(text)
    sub1 = text[span1.start:span1.end]
    assert "入夜后，阿哲独自登上天台" in sub1
    assert "他点了一支烟。" in sub1

    # 两场连续不重叠：第一场 end <= 第二场 start。
    assert span0.end <= span1.start

    # 人物映射：林姐 -> char_lin(命中 bible)，阿哲 -> char_zhe。
    assert s0.characters == ["char_lin"]
    assert s1.characters == ["char_zhe"]


def test_segment_character_fallback_no_bible():
    """bible 为 None 时，人物名映射用原名兜底当 id，不报错。"""
    novel = _make_novel()
    fake = FakeLLM([GOOD_RESPONSE])

    stubs = segment(novel, None, llm=fake)

    assert len(stubs) == 2
    # 没有 bible，原名即 id。
    assert stubs[0].characters == ["林姐"]
    assert stubs[1].characters == ["阿哲"]


def test_segment_character_alias_match():
    """别名命中 bible：用'林女士'(alias)应映射到 char_lin。"""
    novel = _make_novel()
    bible = _make_bible()
    resp = {
        "scenes": [
            {
                "start_marker": "清晨的旧城咖啡馆里",
                "end_marker": "铜铃在门口轻响。",
                "characters": ["林女士"],  # 用别名
                "summary": "",
                "time_of_day": "",
                "location_hint": "",
            }
        ]
    }
    stubs = segment(novel, bible, llm=FakeLLM([resp]))
    assert len(stubs) == 1
    assert stubs[0].characters == ["char_lin"]


def test_segment_marker_not_found_fallback():
    """兜底路径：第一场 start_marker 不存在、第二场 end_marker 不存在。

    断言：不崩；两场仍连续覆盖整章；每个 span 自洽不越界。
    """
    novel = _make_novel()
    bible = _make_bible()
    resp = {
        "scenes": [
            {
                "start_marker": "原文里根本没有的开头XYZ",  # 找不到 -> 从章首接续
                "end_marker": "铜铃在门口轻响。",            # 这个能找到
                "characters": [],
                "summary": "",
                "time_of_day": "",
                "location_hint": "",
            },
            {
                "start_marker": "入夜后，阿哲独自登上天台",    # 能找到
                "end_marker": "原文里根本没有的结尾QWE",      # 找不到 -> 兜底到章末
                "characters": [],
                "summary": "",
                "time_of_day": "",
                "location_hint": "",
            },
        ]
    }
    stubs = segment(novel, bible, llm=FakeLLM([resp]))
    assert len(stubs) == 2

    text = CHAPTER_TEXT
    span0 = stubs[0].source_ref.spans[0]
    span1 = stubs[1].source_ref.spans[0]

    # 各自不越界、start<end。
    assert 0 <= span0.start < span0.end <= len(text)
    assert 0 <= span1.start < span1.end <= len(text)

    # 第一场 start 找不到 -> 从章首 0 接续。
    assert span0.start == 0
    # 连续不重叠。
    assert span0.end <= span1.start
    # 第二场 end 找不到 -> 兜底到章末，覆盖到结尾。
    assert span1.end == len(text)


def test_segment_empty_scenes_fallback():
    """LLM 不给 scenes 键(或空列表)时，整章兜底为一场，覆盖全章。"""
    novel = _make_novel()
    stubs = segment(novel, None, llm=FakeLLM([{"scenes": []}]))
    assert len(stubs) == 1
    span = stubs[0].source_ref.spans[0]
    # 整章一场：从 0 覆盖到章末。
    assert span.start == 0
    assert span.end == len(CHAPTER_TEXT)


def test_segment_multi_chapter_id_continuity():
    """跨章场景 id 连续递增：第一章 2 场 + 第二章 1 场 -> sc_001..sc_003。"""
    ch1 = Chapter(index=1, title="一", text=CHAPTER_TEXT)
    ch2_text = "第二天午后，林姐又见到了阿哲，两人在码头边谈起往事。"
    ch2 = Chapter(index=2, title="二", text=ch2_text)
    novel = Novel(title="t", raw=CHAPTER_TEXT + ch2_text, chapters=[ch1, ch2])

    resp_ch2 = {
        "scenes": [
            {
                "start_marker": "第二天午后",
                "end_marker": "谈起往事。",
                "characters": ["林姐", "阿哲"],
                "summary": "",
                "time_of_day": "午后",
                "location_hint": "码头",
            }
        ]
    }
    fake = FakeLLM([GOOD_RESPONSE, resp_ch2])
    stubs = segment(novel, _make_bible(), llm=fake)

    # 两章 -> 两次调用。
    assert fake.calls == 2
    assert [s.id for s in stubs] == ["sc_001", "sc_002", "sc_003"]
    # 第三场属于第二章，偏移落在第二章 text 上、自洽。
    s3 = stubs[2]
    assert s3.chapter_index == 2
    span = s3.source_ref.spans[0]
    assert 0 <= span.start < span.end <= len(ch2_text)
    assert "第二天午后" in ch2_text[span.start:span.end]


# ----------------------------------------------------------------------------
# 可选在线冒烟：真调 LLM 跑一遍 segment，验证每个 span 在真实模型输出下也自洽。
# 无 DEEPSEEK_API_KEY 时跳过，保证默认 CI 不联网。
# ----------------------------------------------------------------------------
@pytest.mark.skipif(
    not os.getenv("DEEPSEEK_API_KEY"),
    reason="无 DEEPSEEK_API_KEY，跳过在线冒烟",
)
def test_segment_online_smoke():
    # 一段含明显时空切换的中文样本：白天医院 -> 夜晚出租屋。
    text = (
        "白天的医院走廊里，苏晴攥着检查单，护士叫到她的名字，她深吸一口气走进诊室。"
        "医生的话像潮水一样涌来，她几乎站不稳。"
        "深夜，她回到狭小的出租屋，屋里没有开灯，她靠着门坐下，泪水无声地流。"
        "窗外的雨打在玻璃上，她想起了很多年前的那个夏天。"
    )
    chapter = Chapter(index=1, title="第一章", text=text)
    novel = Novel(title="冒烟样本", raw=text, chapters=[chapter])
    bible = StoryBible(
        characters=[Character(id="char_su", name="苏晴", aliases=[])],
        locations=[],
        timeline=[],
    )

    # llm=None -> 走 get_llm() 默认 deepseek。
    stubs = segment(novel, bible, llm=None)

    # 至少切出 1 场。
    assert len(stubs) >= 1
    # 每个 span 必须自洽、不越界、start<end，并按出现顺序连续不重叠。
    prev_end = 0
    for i, s in enumerate(stubs):
        assert s.id == "sc_%03d" % (i + 1)
        assert s.source_ref.chapter == 1
        span = s.source_ref.spans[0]
        assert 0 <= span.start < span.end <= len(text)
        # 取回原文非空。
        assert len(text[span.start:span.end]) > 0
        # 顺序连续(允许相接，不允许重叠回退)。
        assert span.start >= prev_end - 0  # start 不早于上一场起点之前
        prev_end = span.end
