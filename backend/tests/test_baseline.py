# -*- coding: utf-8 -*-
"""
baseline 模块测试(朴素基线对照组)。

测试目标(全程离线、用 fake llm，不联网)：
  1. naive_convert 返回非空轻量结构(有 scenes、有 lines)。
  2. naive_convert 确实调用了 LLM(fake llm 记录到了调用)，且按块数调用。
  3. 朴素产物刻意"缺斤少两"——递归扫描整个返回 dict，断言**不出现**
     source_ref / adaptation / story_bible / continuity 这类字段，
     这正是它和我们完整管线的本质差距(对照组的意义)。
  4. naive_vs_ours_summary：给定样例输入，对比维度齐全且方向正确——
     我们这边可溯源/跨章一致/外化/结构化/连贯性检查 = True，
     朴素这边一律 = False。
"""

import json

import pytest

from app.pipeline.baseline import naive_convert, naive_vs_ours_summary


# ---------------------------------------------------------------------------
# fake llm：记录调用次数与收到的 messages，返回预设浅 JSON 剧本
# ---------------------------------------------------------------------------

class FakeLLM:
    """
    假 LLM：不联网。每次 complete() 自增计数并存下 messages，
    返回一个朴素的浅 JSON(只有 heading + lines，没有任何溯源/外化字段)。
    """

    def __init__(self):
        # 调用次数计数器，断言"确实调用了 LLM"且次数 == 块数。
        self.calls = 0
        # 存下所有调用的 messages，供 prompt 内容断言。
        self.all_messages = []

    def complete(self, messages, json=False, model=None, temperature=0.0, **kw):
        # 记录这次调用。
        self.calls += 1
        self.all_messages.append(messages)
        # 返回朴素浅 JSON：故意只有 heading + lines，不含 source_ref/adaptation。
        return {
            "scenes": [
                {
                    "heading": "INT. 咖啡馆 - 日",
                    "lines": [
                        "林深推开门，看见沈言。",
                        "好久不见。",
                    ],
                }
            ]
        }


# ---------------------------------------------------------------------------
# 工具：递归收集一个嵌套结构里出现的所有 dict key
# ---------------------------------------------------------------------------

def _collect_keys(obj) -> set:
    """递归遍历 dict/list，返回所有出现过的 dict 键名集合。"""
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _collect_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _collect_keys(item)
    return keys


# ---------------------------------------------------------------------------
# naive_convert 测试
# ---------------------------------------------------------------------------

def test_naive_convert_returns_nonempty_structure():
    """短文本走单块：返回非空 scenes，且每个 scene 有 heading 与 lines。"""
    fake = FakeLLM()
    text = "林深推开门，看见沈言坐在靠窗的位置。"
    out = naive_convert(text, medium="film", llm=fake)

    # 顶层结构齐全。
    assert isinstance(out, dict)
    assert out["medium"] == "film"
    # 短文本只切一块，故 chunks == 1，且 LLM 被调用 1 次。
    assert out["chunks"] == 1
    assert fake.calls == 1

    # scenes 非空，结构合法。
    scenes = out["scenes"]
    assert isinstance(scenes, list)
    assert len(scenes) >= 1
    first = scenes[0]
    assert isinstance(first["heading"], str) and first["heading"]
    assert isinstance(first["lines"], list) and len(first["lines"]) >= 1


def test_naive_convert_calls_llm_per_chunk():
    """长文本被切成多块：LLM 调用次数 == 块数，scenes 来自各块拼接。"""
    fake = FakeLLM()
    # 造一段远超单块大小(1200 字符)的文本，确保切多块。
    text = "测" * 3000
    out = naive_convert(text, medium="series", llm=fake)

    # 3000 / 1200 -> 3 块(1200 + 1200 + 600)。
    assert out["chunks"] == 3
    # LLM 调用次数与块数一致——体现"每块一次调用"的朴素做法。
    assert fake.calls == 3
    # 每块预设返回 1 个场景，拼接后共 3 个。
    assert len(out["scenes"]) == 3


def test_naive_convert_omits_deep_fields():
    """
    核心对照断言：朴素产物**不含** source_ref / adaptation / story_bible /
    continuity 这类字段。这正是它和我们完整管线的本质差距。
    """
    fake = FakeLLM()
    out = naive_convert("一段测试小说原文。", medium="short_drama", llm=fake)

    # 递归收集返回结构里的所有键名。
    keys = _collect_keys(out)
    # 这些是我们完整管线才有的"深度字段"，朴素基线一律不应出现。
    forbidden = {
        "source_ref",
        "adaptation",
        "story_bible",
        "continuity_flags",
        "characters",
        "timeline",
    }
    overlap = keys & forbidden
    assert overlap == set(), "朴素产物不应含深度字段，却出现了: %s" % overlap


def test_naive_convert_medium_hint_in_prompt():
    """short_drama 时 prompt 里应含短剧风格提示(朴素也回显媒介，但不系统性重渲染)。"""
    fake = FakeLLM()
    naive_convert("测试原文。", medium="short_drama", llm=fake)
    # 取第一次调用的 system 消息内容。
    system_msg = fake.all_messages[0][0]["content"]
    assert "短剧" in system_msg


def test_naive_convert_empty_text():
    """空文本：不切块、不调用 LLM、scenes 为空(健壮性)。"""
    fake = FakeLLM()
    out = naive_convert("   ", medium="film", llm=fake)
    assert out["chunks"] == 0
    assert fake.calls == 0
    assert out["scenes"] == []


# ---------------------------------------------------------------------------
# naive_vs_ours_summary 测试
# ---------------------------------------------------------------------------

def _sample_ours_metrics() -> dict:
    """构造一份"我们这边做到了"的样例指标(模拟 compute_metrics 输出)。"""
    return {
        "scene_count": 3,
        "character_count": 4,
        "element_count": 20,
        "dialogue_count": 12,
        "action_count": 7,
        "transition_count": 1,
        "externalized_count": 3,
        "traceability_coverage": 0.8,
        "continuity_error_count": 1,
        "continuity_warn_count": 2,
        "schema_valid": True,
    }


def test_summary_dimensions_complete_and_directional():
    """对比维度齐全，且方向正确：我们 True / 朴素 False。"""
    naive_out = {"medium": "film", "chunks": 2, "scenes": [{"heading": "x", "lines": ["y"]}]}
    summary = naive_vs_ours_summary(naive_out, _sample_ours_metrics())

    dims = summary["dimensions"]
    # 五个维度都在。
    expected_dims = {
        "cross_chapter_consistency",
        "traceable",
        "externalization_marked",
        "structured_editable",
        "continuity_checked",
    }
    assert set(dims.keys()) == expected_dims

    # 每个维度方向正确：ours=True，naive=False。
    for name, d in dims.items():
        assert d["ours"] is True, "维度 %s 我们这边应为 True" % name
        assert d["naive"] is False, "维度 %s 朴素这边应为 False" % name
        # 每个维度都带一句中文说明。
        assert isinstance(d["note"], str) and len(d["note"]) > 0

    # 顶层有一句话总结。
    assert isinstance(summary["headline"], str) and len(summary["headline"]) > 0


def test_summary_reflects_weak_ours_metrics():
    """
    若我们的指标里某维度其实没做到(如覆盖率为 0)，summary 要诚实标 False，
    证明判定来自客观数字而非写死。
    """
    weak = {
        "character_count": 0,        # 没建 bible
        "traceability_coverage": 0.0,  # 没溯源
        "externalized_count": 0,     # 没外化
        "schema_valid": False,       # schema 不过
        # 故意不放任何 continuity 字段 -> 连贯性检查视为未做。
    }
    summary = naive_vs_ours_summary({"scenes": []}, weak)
    dims = summary["dimensions"]
    # 这些维度我们这边也应为 False(诚实)。
    assert dims["cross_chapter_consistency"]["ours"] is False
    assert dims["traceable"]["ours"] is False
    assert dims["externalization_marked"]["ours"] is False
    assert dims["structured_editable"]["ours"] is False
    assert dims["continuity_checked"]["ours"] is False
