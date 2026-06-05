# -*- coding: utf-8 -*-
"""
schema 模块测试。

覆盖：
1. 构造完整合法 Screenplay(三种 element + Adaptation + source_ref)，校验通过。
2. to_yaml -> from_yaml 往返一致，且 YAML 里 Adaptation 键是 "from" 非 "from_"。
3. 非法 dict 触发 ValidationError。
4. validate_and_repair：用 fake llm(返回预设修复结果)测修复路径，不依赖网络。
"""

import pytest
import yaml
from pydantic import ValidationError

from app.schema.models import (
    Adaptation,
    ActionElement,
    Character,
    DialogueElement,
    Heading,
    Location,
    Meta,
    Relationship,
    Scene,
    Screenplay,
    SourceMeta,
    SourceRef,
    Span,
    StoryBible,
    TimePoint,
    TransitionElement,
)
from app.schema.validate import validate_and_repair


def _make_full_screenplay() -> Screenplay:
    """构造一份完整合法剧本，含三种 element、Adaptation、source_ref。"""
    return Screenplay(
        meta=Meta(
            title="测试剧本",
            source=SourceMeta(type="novel", chapters=[1, 2, 3]),
            target_medium="short_drama",
            language="zh",
        ),
        story_bible=StoryBible(
            characters=[
                Character(
                    id="char_lin",
                    name="林深",
                    aliases=["小林"],
                    traits=["沉默"],
                    arc="从压抑到爆发",
                    relationships=[Relationship(to="char_su", type="旧友")],
                ),
                Character(id="char_su", name="苏晚"),
            ],
            locations=[Location(id="loc_cafe", name="街角咖啡馆")],
            timeline=[TimePoint(id="tp_1", label="重逢之夜", order=1)],
        ),
        scenes=[
            Scene(
                id="sc_001",
                heading=Heading(
                    int_ext="INT",
                    location_id="loc_cafe",
                    time_of_day="夜",
                    time_ref="tp_1",
                ),
                source_ref=SourceRef(chapter=1, spans=[Span(start=0, end=120)]),
                characters=["char_lin", "char_su"],
                synopsis="林深与苏晚在咖啡馆重逢。",
                elements=[
                    # 动作元素，带元素级 source_ref。
                    ActionElement(
                        type="action",
                        text="林深推门而入，雨水顺着风衣滴落。",
                        source_ref=SourceRef(chapter=1, spans=[Span(start=0, end=30)]),
                    ),
                    # 对白元素，带 adaptation(内心戏外化)。
                    DialogueElement(
                        type="dialogue",
                        character="char_lin",
                        line="好久不见。",
                        parenthetical="低声",
                        source_ref=SourceRef(chapter=1, spans=[Span(start=31, end=60)]),
                        adaptation=Adaptation(
                            from_="interior_monologue",
                            technique="subtext",
                        ),
                    ),
                    # 转场元素。
                    TransitionElement(type="transition", text="CUT TO"),
                ],
            )
        ],
    )


def test_full_screenplay_valid():
    """完整合法剧本应能成功构造，且字段读得到。"""
    sp = _make_full_screenplay()
    # 顶层结构正确。
    assert sp.meta.title == "测试剧本"
    assert sp.meta.target_medium == "short_drama"
    # 判别联合选对了子类型。
    elems = sp.scenes[0].elements
    assert isinstance(elems[0], ActionElement)
    assert isinstance(elems[1], DialogueElement)
    assert isinstance(elems[2], TransitionElement)
    # adaptation 正确挂在对白上。
    assert elems[1].adaptation.from_ == "interior_monologue"


def test_yaml_round_trip_and_from_alias():
    """to_yaml -> from_yaml 往返一致；YAML 中 Adaptation 键为 'from'。"""
    sp = _make_full_screenplay()
    text = sp.to_yaml()

    # YAML 文本里必须出现 "from:" 而不是 "from_:"。
    assert "from:" in text
    assert "from_:" not in text

    # 解析回 dict 进一步确认键名(避免子串误判)。
    raw = yaml.safe_load(text)
    adaptation_dict = raw["scenes"][0]["elements"][1]["adaptation"]
    assert "from" in adaptation_dict
    assert "from_" not in adaptation_dict

    # 往返：再读回来得到的对象应与原对象等价。
    sp2 = Screenplay.from_yaml(text)
    assert sp2 == sp
    # 再 dump 一次应得到完全相同文本(序列化稳定)。
    assert sp2.to_yaml() == text


def test_exclude_none_keeps_yaml_clean():
    """值为 None 的可选字段不应出现在 YAML 中。"""
    sp = _make_full_screenplay()
    raw = yaml.safe_load(sp.to_yaml())
    # 转场元素没有 source_ref/adaptation，不该出现这些键。
    transition = raw["scenes"][0]["elements"][2]
    assert "source_ref" not in transition
    assert "adaptation" not in transition


def test_invalid_dict_raises_validation_error():
    """非法 dict(element.type 不在判别值域)应触发 ValidationError。"""
    bad = {
        "meta": {
            "title": "坏剧本",
            "source": {"type": "novel", "chapters": [1]},
        },
        "story_bible": {"characters": [], "locations": [], "timeline": []},
        "scenes": [
            {
                "id": "sc_001",
                "heading": {
                    "int_ext": "INT",
                    "location_id": "loc_x",
                    "time_of_day": "夜",
                },
                "source_ref": {"chapter": 1, "spans": [{"start": 0, "end": 1}]},
                "characters": [],
                # type 非法判别值，判别联合无法匹配任何子模型。
                "elements": [{"type": "monologue", "text": "x"}],
            }
        ],
    }
    with pytest.raises(ValidationError):
        Screenplay.model_validate(bad)


class _FakeLLM:
    """
    假 LLM：不联网。complete 被调用时返回预设的修复后 YAML，
    并记录被调用次数，用于断言修复路径确实走到了。
    """

    def __init__(self, fixed_yaml: str):
        self.fixed_yaml = fixed_yaml
        self.calls = 0

    def complete(self, messages, json=False):
        # 记录调用次数。
        self.calls = self.calls + 1
        # 始终返回预设修复结果。
        return self.fixed_yaml


def test_validate_and_repair_success_first_try():
    """首次即合法时，不应触发 LLM 修复(calls 保持 0)。"""
    sp = _make_full_screenplay()
    fake = _FakeLLM(fixed_yaml="不会被用到")
    result = validate_and_repair(sp.to_yaml(), fake)
    assert isinstance(result, Screenplay)
    assert result == sp
    assert fake.calls == 0


def test_validate_and_repair_repairs_then_succeeds():
    """首次非法 -> 回喂 fake llm -> 第二次用修复结果校验通过。"""
    # 预设：fake llm 返回的修复结果是一份合法剧本 YAML。
    good_yaml = _make_full_screenplay().to_yaml()
    fake = _FakeLLM(fixed_yaml=good_yaml)

    # 喂一个非法输入(element.type 非法)。
    broken = {
        "meta": {
            "title": "坏剧本",
            "source": {"type": "novel", "chapters": [1]},
        },
        "story_bible": {"characters": [], "locations": [], "timeline": []},
        "scenes": [
            {
                "id": "sc_001",
                "heading": {
                    "int_ext": "INT",
                    "location_id": "loc_x",
                    "time_of_day": "夜",
                },
                "source_ref": {"chapter": 1, "spans": [{"start": 0, "end": 1}]},
                "characters": [],
                "elements": [{"type": "BAD", "text": "x"}],
            }
        ],
    }

    result = validate_and_repair(broken, fake, max_retries=2)
    # 修复后应得到合法 Screenplay。
    assert isinstance(result, Screenplay)
    # LLM 被调用了恰好一次(首次失败触发一次修复，第二次即成功)。
    assert fake.calls == 1


def test_validate_and_repair_exhausts_retries():
    """LLM 一直返回非法结果时，重试耗尽应抛 ValidationError。"""
    # fake 始终返回仍然非法的 YAML。
    bad_yaml = yaml.safe_dump({"meta": {"title": "x"}}, allow_unicode=True)
    fake = _FakeLLM(fixed_yaml=bad_yaml)

    with pytest.raises(ValidationError):
        validate_and_repair(bad_yaml, fake, max_retries=2)
    # 首次 + 2 次修复 = 3 次尝试，其中前 2 次失败各触发一次修复调用。
    assert fake.calls == 2
