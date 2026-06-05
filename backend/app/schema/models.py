# -*- coding: utf-8 -*-
"""
Screenplay 数据契约 —— pydantic v2 模型。

这是整个 Screenwright 项目的"数据契约基石"：
- 管线各 Pass(bible/segment/generate/validate/export) 都围绕这些模型流转。
- LLM 产出经 validate_and_repair 收敛到这里，前端按这里的结构渲染与溯源。

关键设计点(逐处中文讲解)：
1. Element 用"判别联合(discriminated union)"而非分桶 list，
   因为剧本本质是有序时间序列，顺序承载叙事(BRIEF 第6节最重要决策)。
2. Adaptation.from_ 字段名带下划线(避开 Python 关键字 from)，
   但序列化进 YAML/JSON 时键必须是 "from"，用 alias + populate_by_name 实现。
3. to_yaml / from_yaml 必须严格往返一致(round-trip)，
   依赖 by_alias=True(让 from_→from) 与 from_yaml 时 alias 回读。
"""

from __future__ import annotations

from typing import Annotated, List, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field


# ----------------------------------------------------------------------------
# 基础叶子模型
# ----------------------------------------------------------------------------

class Span(BaseModel):
    """原文字符区间偏移。行级溯源的最小单位。"""
    # start/end 是原文中的字符偏移，前端据此精确高亮。
    start: int
    end: int


class SourceRef(BaseModel):
    """场级 / 元素级溯源：指向某一章的若干字符区间。"""
    # chapter 是章号(1-based)；spans 允许一个元素对应原文多段(如跨段落改写)。
    chapter: int
    spans: List[Span]


class Adaptation(BaseModel):
    """
    内心戏外化的透明标记(创新点③)。

    from_ 别名设计：
    - 字段在 Python 端叫 from_(因为 from 是关键字，不能直接当字段名)。
    - alias="from" 让它在序列化(by_alias=True)时写成 "from"。
    - model_config 里 populate_by_name=True 允许同时用字段名 from_ 来构造对象，
      这样测试里写 Adaptation(from_=..., technique=...) 和从 YAML 的 "from" 读入都能成立。
    """
    # populate_by_name=True：构造时既可用别名 "from" 也可用字段名 "from_"。
    model_config = ConfigDict(populate_by_name=True)

    # from_ 记录"原文是什么"：内心独白 / 旁白叙述 / 描写。
    from_: Literal["interior_monologue", "narration", "description"] = Field(alias="from")
    # technique 记录"外化成什么手法"：潜台词 / 动作 / 画外音 / 视觉化。
    technique: Literal["subtext", "action", "voiceover", "visual"]


# ----------------------------------------------------------------------------
# StoryBible —— 跨章一致性的单一事实源(创新点②的基础)
# ----------------------------------------------------------------------------

class Relationship(BaseModel):
    """人物关系边：to 指向另一个 character id，type 描述关系。"""
    to: str
    type: str


class Character(BaseModel):
    """人物条目。id 是稳定标识(如 char_lin)，全篇引用都用 id 不用 name。"""
    id: str
    name: str
    # 下面这些都给默认空值，允许 bible 抽取阶段渐进填充。
    aliases: List[str] = []
    traits: List[str] = []
    arc: str = ""
    relationships: List[Relationship] = []


class Location(BaseModel):
    """地点条目。Heading.location_id 引用它。"""
    id: str
    name: str


class TimePoint(BaseModel):
    """时间线节点。order 给连贯性检查排序用；Heading.time_ref 可关联其 id。"""
    id: str
    label: str
    order: int


class StoryBible(BaseModel):
    """单一事实源：人物 / 地点 / 时间线，生成每场时注入切片防矛盾。"""
    characters: List[Character]
    locations: List[Location]
    timeline: List[TimePoint]


# ----------------------------------------------------------------------------
# Element 判别联合 —— 剧本有序序列的核心(BRIEF 第6节最重要决策)
# ----------------------------------------------------------------------------

class ActionElement(BaseModel):
    """动作 / 场景描述行。"""
    # type 是判别字段(literal 常量)，判别联合靠它选具体子类型。
    type: Literal["action"]
    text: str
    # source_ref 下沉到元素级，支撑行级双向溯源(创新点②)。
    source_ref: Optional[SourceRef] = None
    # adaptation 非空表示这行是内心戏外化来的(创新点③)。
    adaptation: Optional[Adaptation] = None


class DialogueElement(BaseModel):
    """对白行。character 是说话人的 character id。"""
    type: Literal["dialogue"]
    character: str
    line: str
    # parenthetical 是括号提示(如 "冷笑")，可空。
    parenthetical: Optional[str] = None
    source_ref: Optional[SourceRef] = None
    adaptation: Optional[Adaptation] = None


class TransitionElement(BaseModel):
    """转场行，如 CUT TO / FADE OUT。无溯源/外化字段。"""
    type: Literal["transition"]
    text: str


# Element 判别联合：
# - Union 把三种元素并起来。
# - Annotated + Field(discriminator="type") 告诉 pydantic：
#   解析一个 dict 时，看它的 "type" 字段值("action"/"dialogue"/"transition")，
#   直接选对应子模型校验，而不是逐个 try。
#   好处：错误信息精确(不会糊成"三个都不匹配")，性能也更好。
Element = Annotated[
    Union[ActionElement, DialogueElement, TransitionElement],
    Field(discriminator="type"),
]


# ----------------------------------------------------------------------------
# Scene
# ----------------------------------------------------------------------------

class Heading(BaseModel):
    """场景标题行(slugline)。"""
    # 内景/外景/内外景。
    int_ext: Literal["INT", "EXT", "INT/EXT"]
    # 引用 Location.id。
    location_id: str
    # 日/夜/黄昏等自由文本。
    time_of_day: str
    # 可选关联 TimePoint.id，供连贯性检查(创新点④)。
    time_ref: Optional[str] = None


class ContinuityFlag(BaseModel):
    """连贯性检查标记(创新点④)。level 决定前端标红/标黄。"""
    level: Literal["info", "warn", "error"]
    msg: str
    scene_ids: List[str] = []


class Scene(BaseModel):
    """单场。elements 是有序带类型序列，是整个剧本结构的核心载体。"""
    id: str
    heading: Heading
    # 场级溯源。
    source_ref: SourceRef
    # 本场出场人物的 character id 列表。
    characters: List[str]
    synopsis: str = ""
    # 有序元素序列(判别联合)。
    elements: List[Element]
    continuity_flags: List[ContinuityFlag] = []


# ----------------------------------------------------------------------------
# 顶层 Meta / Screenplay
# ----------------------------------------------------------------------------

class SourceMeta(BaseModel):
    """来源元信息。当前只支持小说。"""
    type: Literal["novel"] = "novel"
    chapters: List[int]


class Meta(BaseModel):
    """剧本元信息。target_medium 支撑可控媒介改编(创新点①)。"""
    title: str
    source: SourceMeta
    # 目标媒介：电影/剧集/短剧。同一 bible+scenes 按它重渲染。
    target_medium: Literal["film", "series", "short_drama"] = "film"
    schema_version: str = "1.0"
    # 输出语言，默认中文(随输入)。
    language: str = "zh"


class Screenplay(BaseModel):
    """顶层剧本对象。整个数据契约的根。"""
    meta: Meta
    story_bible: StoryBible
    scenes: List[Scene]

    def to_yaml(self) -> str:
        """
        序列化为 YAML 字符串。

        关键参数：
        - model_dump(by_alias=True)：让 Adaptation.from_ 输出成键 "from"。
        - exclude_none=True：去掉值为 None 的可选字段(如未填的 source_ref)，YAML 更干净。
        - yaml.safe_dump(allow_unicode=True)：中文不转义成 \\uXXXX。
        - sort_keys=False：保持模型字段定义顺序，可读且便于人工 diff/手改。
        """
        # 先转成纯 dict(别名生效、去掉 None)。
        data = self.model_dump(by_alias=True, exclude_none=True)
        # 再 dump 成 YAML 文本。
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

    @classmethod
    def from_yaml(cls, s: str) -> "Screenplay":
        """
        从 YAML 字符串反序列化为 Screenplay。

        round-trip 要点：
        - safe_load 解析出的 dict 里 Adaptation 的键是 "from"。
        - 因为 Adaptation 配了 alias="from"，pydantic 默认按 alias 读，能正确填进 from_。
        - model_validate 会触发判别联合按 type 选子模型校验。
        """
        # 解析 YAML 文本为 Python dict。
        data = yaml.safe_load(s)
        # 用 pydantic 校验并构造对象(alias 自动生效)。
        return cls.model_validate(data)
