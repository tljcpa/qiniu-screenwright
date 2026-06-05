# ----------------------------------------------------------------------------
# pipeline/types.py —— 管线各 Pass 之间流转的"中间数据类型契约"
#
# 这些类型不是最终输出(最终输出是 app/schema/models.py 里的 Screenplay)，
# 而是 ingest(Pass0) / bible(Pass1) / segment(Pass2) / generate(Pass3)
# 之间互相传递的"半成品"。bible / segment / generate 三个模块会并行开发，
# 它们都依赖这里定义的类型，所以这里的字段必须精确、稳定，作为共享契约。
#
# 关键设计点："章内偏移自洽"。
#   所有溯源(source_ref)里的字符偏移 start/end，都是相对"某一章的纯文本 text"
#   的偏移，而不是相对全文。原因：
#   1. 全文偏移在分章之后会随章节增删而漂移，章内偏移只依赖该章自身，稳定。
#   2. 前端高亮时是按章展示原文的，章内偏移可以直接 text[start:end] 定位，
#      不需要再做"全文偏移 - 本章起始偏移"的换算，少一层出错可能。
#   3. SceneStub.source_ref.chapter 指明是哪一章，spans 里的偏移就落在该章 text 上，
#      两者配合就能无歧义地还原原文片段 —— 这正是创新点②"行级双向溯源"的根基。
#   因此 Chapter.text 必须保存"该章原始文本"，绝不能 strip 掉首尾或做任何会改变
#   字符位置的清洗，否则偏移就对不上原文了。
# ----------------------------------------------------------------------------

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field

# 复用 schema 里已定稿的 SourceRef(chapter:int, spans:list[Span{start,end}])。
# 不在这里另起一套溯源类型，避免两套不一致 —— 溯源契约全篇只有一个来源。
from app.schema.models import SourceRef


class Chapter(BaseModel):
    """
    一章的纯文本。

    index：章号，1-based(第一章 index=1)。与 schema 里 chapters:list[int] 对齐。
    title：该章标题行原文(如"第一章 三年后的铃铛"或"Chapter 1.")。
    text ：该章正文纯文本。后续所有 source_ref 的字符偏移都相对此 text，
           所以这里保存的是"该章原始文本"，不做破坏偏移的清洗。
    """
    index: int
    title: str
    text: str


class Chunk(BaseModel):
    """
    某一章内部的一个上下文窗口(滚动切块的产物)。

    长章节可能超过 LLM 的上下文限制，所以要切成带重叠的窗口分批喂给模型。

    chapter_index：这个窗口属于哪一章(对应 Chapter.index)。
    start/end    ：章内字符偏移(相对该章 text)，不是全文偏移。
    text         ：窗口文本，约定恒满足 text == chapter.text[start:end]。
                   这条不变式被测试强制校验，因为窗口内抽到的任何 span 偏移
                   最终都要能回落到原章 text 上做溯源，自洽是溯源正确的前提。
    """
    chapter_index: int
    start: int
    end: int
    text: str


class Novel(BaseModel):
    """
    一部小说的解析结果(ingest 的产物，Pass0 输出)。

    title   ：书名。
    raw     ：传入的全文原始文本(保留，供调试与"无章节标记退化"等场景回看)。
    chapters：分章后的章节列表，按 index 升序。
    """
    title: str
    raw: str
    chapters: List[Chapter]


class SceneStub(BaseModel):
    """
    场景切分(Pass2 segment)的产物，同时是逐场生成(Pass3 generate)的输入。

    它是"场景的骨架"：还没有展开成完整的 Scene(没有 elements 序列)，
    只携带定位与提示信息，让 generate 阶段据此去抽取本场原文并生成剧本元素。

    id           ：场景稳定标识，如 "sc_001"。
    chapter_index：本场主要来源的章号。
    source_ref   ：本场对应的原文区间(复用 schema 的 SourceRef)。
                   其 chapter 应与 chapter_index 一致，spans 偏移落在该章 text 上。
    characters   ：本场涉及的人物(character id 列表)，segment 可初步填充。
    summary      ：本场一句话梗概，给 generate 当提示。
    time_of_day  ：日/夜/黄昏等，供 Heading.time_of_day 的初值。
    location_hint：地点线索(自由文本)，generate/bible 再映射到 location_id。

    后五个字段都给默认值，因为 segment 阶段未必能一次性填全，
    允许渐进式填充，缺失字段不阻断管线。
    """
    id: str
    chapter_index: int
    source_ref: SourceRef
    characters: List[str] = Field(default_factory=list)
    summary: str = ""
    time_of_day: str = ""
    location_hint: str = ""
