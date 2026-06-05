# -*- coding: utf-8 -*-
"""
pipeline/bible.py —— Pass1：故事圣经(StoryBible)抽取。

这是整个 Screenwright 最重要的差异化能力之一：跨章一致性的"单一事实源"。
后续 generate(Pass3) 每生成一场，都会注入 bible 的切片，
靠它保证"同一个人物在第 1 章和第 3 章不会变成两个人、性格不矛盾"。

================ 为什么是"分章抽取 + 代码合并"，而不是一次性丢给 LLM ================

朴素做法是：把整本小说塞进一个 prompt，让 LLM 一次性吐出全局 StoryBible。
我们不这么做，理由如下(这是本模块的核心设计决策)：

1. 上下文与 token：3 章+ 的网文很容易超出单次上下文的"有效注意力"范围。
   即使硬塞进去，模型对长文本末尾/中部的信息抽取质量会显著下降(中间遗忘)。
   分章后每次只看一章，抽取更准、更全。

2. 稳定性与可控：让 LLM 同时做"抽取 + 跨章归一 + 去重 + 分配 id"是多个职责叠加，
   任何一步出错都会污染整张表，且不可复现(温度、措辞一抖结果就变)。
   把"合并/归一/分配 id"剥离成纯 Python 代码后：
     - 抽取只负责"这一章里有谁、有什么"，职责单一，prompt 短而稳；
     - 合并是确定性算法，同样输入永远同样输出，可单元测试、可断点调试。

3. 省 token：分章抽取的输入是单章文本，合并不花 token(纯代码)。
   一次性做法不仅输入更长，还往往要"反复追问补全"，总成本更高。

4. 增量友好：将来某一章被编辑，只需重抽该章再重跑合并，
   不必把整本书重新喂给模型。

因此本模块的形状是：
    for chapter in novel.chapters:
        per_chapter = _extract_chapter(chapter, llm)   # LLM，json 模式，职责单一
    bible = _merge(all_per_chapter_results)            # 纯 Python，确定性合并

================ 人物归一(merge)策略 ================

难点：同一个人在不同章可能用"全名 / 别名 / 简称"出现。
例：第 1 章叫"林深"，第 2 章别人喊他"深哥"——必须识别成同一个 Character。

做法：维护一张 name/alias(规范化后) -> char_id 的映射表 `name_to_id`。
处理每一章抽到的每个人物时：
  - 先把它的 name 和所有 aliases 都规范化(去空格等)。
  - 在映射表里查这些键，只要命中任意一个已知键，就并入对应的已有 Character；
  - 都没命中才新建一个 Character，并分配稳定 id，
    然后把它的 name 和所有 alias 都登记进映射表，供后续章节回指。
合并时累并(union) aliases / traits / relationships，去重不丢序。

稳定 id：`char_` + 名字 slug(中文转可读拼音、英文转小写去空格)。
若 slug 冲突(两个不同人物 slug 相同)，追加 `_2`、`_3` 序号保证全篇唯一。
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional

# 复用已定稿的 schema 模型——溯源/人物契约全篇只有一个来源，避免两套不一致。
from app.schema.models import (
    Character,
    Location,
    Relationship,
    StoryBible,
    TimePoint,
)
from app.pipeline.types import Chapter, Novel


# ---------------------------------------------------------------------------
# 中文转拼音的小型映射 —— 用来给中文人物/地点生成可读的稳定 slug。
#
# 说明：环境里没有 pypinyin(且 requirements 是共享文件，本模块不去改它)，
# 所以这里内置一张"演示样本 + 常见姓名用字"的拼音表，覆盖常见字；
# 表里查不到的字，统一退化为按字符 unicode 码点拼出的可读串(见 _slug)。
# 这样：
#   - 常见名字(林深/沈言)得到漂亮 id(char_lin_shen / char_shen_yan)；
#   - 生僻字也能得到"确定且唯一"的 id(只是不那么好看)，不影响正确性。
# id 的本质要求是"稳定 + 唯一"，可读只是锦上添花。
# ---------------------------------------------------------------------------
_PINYIN: Dict[str, str] = {
    # 演示样本《旧城咖啡》主要用字
    "林": "lin", "深": "shen", "沈": "shen", "言": "yan",
    # 常见姓氏
    "王": "wang", "李": "li", "张": "zhang", "刘": "liu", "陈": "chen",
    "杨": "yang", "赵": "zhao", "黄": "huang", "周": "zhou", "吴": "wu",
    "徐": "xu", "孙": "sun", "胡": "hu", "朱": "zhu", "高": "gao",
    "何": "he", "郭": "guo", "马": "ma", "罗": "luo", "梁": "liang",
    "宋": "song", "郑": "zheng", "谢": "xie", "韩": "han", "唐": "tang",
    "冯": "feng", "于": "yu", "董": "dong", "萧": "xiao", "程": "cheng",
    "曹": "cao", "袁": "yuan", "邓": "deng", "许": "xu", "傅": "fu",
    "苏": "su", "蒋": "jiang", "叶": "ye", "阎": "yan", "薛": "xue",
    "顾": "gu", "段": "duan", "雷": "lei", "黎": "li", "史": "shi",
    "陆": "lu", "夏": "xia", "钟": "zhong", "卢": "lu", "蔡": "cai",
    "贾": "jia", "丁": "ding", "魏": "wei", "薄": "bo", "秦": "qin",
    # 常见名字用字 / 称谓字
    "哥": "ge", "姐": "jie", "弟": "di", "妹": "mei", "老": "lao",
    "小": "xiao", "阿": "a", "大": "da", "明": "ming", "华": "hua",
    "强": "qiang", "伟": "wei", "芳": "fang", "娟": "juan", "敏": "min",
    "静": "jing", "丽": "li", "军": "jun", "洋": "yang", "勇": "yong",
    "艳": "yan", "杰": "jie", "娜": "na", "燕": "yan", "磊": "lei",
    "云": "yun", "海": "hai", "涛": "tao", "鹏": "peng", "飞": "fei",
    "雪": "xue", "梅": "mei", "霞": "xia", "婷": "ting", "悦": "yue",
    "宇": "yu", "辰": "chen", "轩": "xuan", "睿": "rui", "浩": "hao",
    "城": "cheng", "南": "nan", "北": "bei", "东": "dong", "西": "xi",
    "旧": "jiu", "咖": "ka", "啡": "fei", "馆": "guan",
}


def _slug(name: str) -> str:
    """
    把一个名字转成 ascii slug(只含小写字母数字和下划线)。

    规则：
      - 英文/数字：转小写，非字母数字一律换成下划线，再压缩多余下划线。
      - 中文：逐字查 _PINYIN 表；查到用拼音，查不到用 'u' + 该字 unicode 十六进制
        (保证确定且唯一)；字间用下划线连接。
      - 中英混排：分别处理后拼接。

    注意：本函数只保证"确定性映射"(同名永远同 slug)，
    全局唯一由调用方 _alloc_id 通过冲突加序号来兜底，二者分工。
    """
    # 先按"连续 ascii 词"与"单个非 ascii 字"切分，便于分别处理。
    parts: List[str] = []
    # 缓冲区，攒连续的 ascii 字母数字。
    buf: List[str] = []

    def _flush_buf() -> None:
        # 把缓冲区里攒的 ascii 词整体转小写后入 parts。
        if buf:
            parts.append("".join(buf).lower())
            buf.clear()

    for ch in name:
        # 判断是不是 ascii 字母或数字。
        if ch.isascii() and ch.isalnum():
            buf.append(ch)
            continue
        # 遇到非 ascii 字母数字：先把已攒的 ascii 词收掉。
        _flush_buf()
        # 中文(或其他非 ascii 字符)逐字处理。
        if ch in _PINYIN:
            parts.append(_PINYIN[ch])
        elif ch.isascii():
            # ascii 里的标点/空格之类：当分隔符，跳过(不产出片段)。
            pass
        else:
            # 不在拼音表里的非 ascii 字：用 'u' + 码点十六进制兜底，保证确定且唯一。
            parts.append("u" + format(ord(ch), "x"))
    # 循环结束别忘了收尾缓冲区。
    _flush_buf()

    # 用下划线拼接所有片段。
    slug = "_".join(p for p in parts if p)
    # 兜底：万一名字全是被跳过的字符导致 slug 为空，用名字的哈希前 8 位。
    if not slug:
        slug = "x" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    # 收尾清理：把可能出现的连续下划线压成一个，去掉首尾下划线。
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug


def _norm(name: Optional[str]) -> str:
    """
    名字规范化：用于"是否同一人/同一地点"的比对键。

    去掉首尾空白与名字内部的所有空白(中文姓名内不该有空格，
    英文名内部空格保留语义但比对时也压成单空格)，并转小写。
    这是归一比对的关键——'深哥' 与 ' 深哥 ' 必须算同一个键。
    """
    if name is None:
        return ""
    # strip 去首尾，内部连续空白压成单个空格，再小写。
    s = re.sub(r"\s+", " ", name.strip()).lower()
    return s


# ---------------------------------------------------------------------------
# 单章抽取(LLM，json 模式) —— 职责单一：只回答"这一章里有谁、有什么"。
# ---------------------------------------------------------------------------

# 系统提示：约束 LLM 严格只做单章抽取，不做跨章归一(归一交给代码)。
_EXTRACT_SYSTEM = (
    "你是剧本改编流水线里的'单章信息抽取器'。"
    "你只负责从【当前这一章】的文本里抽取出现的人物、地点、时间线索，"
    "绝不臆测本章未出现的内容，也不要做跨章合并(那一步由后续程序完成)。"
    "必须只输出一个 JSON 对象，不要任何解释性文字。"
)


def _extract_prompt(chapter: Chapter) -> str:
    """构造单章抽取的 user prompt，明确要求的 JSON 结构。"""
    # 用清晰的字段约定让模型产出可被代码直接消费的结构。
    schema_hint = (
        '{\n'
        '  "characters": [\n'
        '    {\n'
        '      "name": "本章对该人物最正式/最常用的称呼",\n'
        '      "aliases": ["本章出现的其他称呼/别名/简称，如 深哥、老林"],\n'
        '      "traits": ["从本章文本可推断的性格/身份特征，简短词组"],\n'
        '      "arc": "本章该人物的处境或心理弧光线索，一句话，可空",\n'
        '      "relationships": [\n'
        '        {"to": "对方人物的name(用本章里的称呼即可)", "type": "关系，如 旧情人/同事"}\n'
        '      ]\n'
        '    }\n'
        '  ],\n'
        '  "locations": [ {"name": "本章出现的地点名"} ],\n'
        '  "time_points": [ {"label": "本章出现的时间线索，如 三年前的雨夜/次日傍晚"} ]\n'
        '}'
    )
    return (
        "请从下面这一章里抽取信息，严格按给定 JSON 结构输出。\n"
        "要求：\n"
        "- name 用本章对该人物最稳定的称呼；aliases 收集本章其他叫法。\n"
        "- 同一个人在本章内只出一条(本章内自行合并)。\n"
        "- relationships.to 用对方在本章里的称呼名字即可，后续程序会归一成 id。\n"
        "- 没有的字段给空数组/空串，不要编造。\n\n"
        "JSON 结构：\n" + schema_hint + "\n\n"
        "本章标题：" + (chapter.title or "") + "\n"
        "本章正文：\n" + chapter.text + "\n"
    )


def _extract_chapter(chapter: Chapter, llm) -> dict:
    """
    调 LLM 抽取单章信息，返回规整后的 dict。

    返回结构固定为 {"characters":[...], "locations":[...], "time_points":[...]}，
    并对每个字段做容错(缺字段/类型不对时退化为空)，
    让下游合并代码可以无脑信任结构，不必到处判空。
    """
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": _extract_prompt(chapter)},
    ]
    # json=True：要求 LLM 返回 JSON 对象，client 会解析成 dict 返回。
    raw = llm.complete(messages, json=True)
    # 防御：万一返回不是 dict(理论上 client 已保证)，退化为空结构。
    if not isinstance(raw, dict):
        raw = {}

    # 逐字段规整，保证结构稳定。
    return {
        "characters": _as_list(raw.get("characters")),
        "locations": _as_list(raw.get("locations")),
        "time_points": _as_list(raw.get("time_points")),
    }


def _as_list(v) -> list:
    """把任意值规整成 list：本来是 list 原样返回，否则空 list。"""
    if isinstance(v, list):
        return v
    return []


def _as_str_list(v) -> List[str]:
    """把任意值规整成 List[str]：过滤非字符串、去空白、去重保序。"""
    out: List[str] = []
    seen = set()
    if not isinstance(v, list):
        return out
    for item in v:
        # 只接受字符串元素。
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s:
            continue
        key = _norm(s)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# 纯 Python 合并 —— 确定性、可测试、可调试。
# ---------------------------------------------------------------------------

class _CharAccumulator:
    """
    合并过程中的人物累加器(可变中间态)。

    与 schema 的 Character(不可变契约、最终产物) 分开，
    是为了在合并途中方便地 union 别名/特征/关系，最后再"冻结"成 Character。
    """

    def __init__(self, char_id: str, name: str):
        # 稳定 id，一旦分配不再变。
        self.id = char_id
        # 展示用主名(取第一次见到的 name)。
        self.name = name
        # 下面三者用"list + seen set"实现去重保序的 union。
        self.aliases: List[str] = []
        self._alias_seen = set()
        self.traits: List[str] = []
        self._trait_seen = set()
        self.arc: str = ""
        # 关系暂存为 (to_name_norm 或 to_id, type) 的中间形式，最后再解析成 id。
        # 这里先存"对方的规范化名字"，因为遍历到某人时对方可能还没建号。
        self.rel_pending: List[dict] = []

    def add_alias(self, alias: str) -> None:
        # 别名去重：用规范化后的键判重，但存原始展示文本。
        key = _norm(alias)
        if not key:
            return
        # 与主名相同的别名不必再记(主名已是身份)。
        if key == _norm(self.name):
            return
        if key in self._alias_seen:
            return
        self._alias_seen.add(key)
        self.aliases.append(alias)

    def add_trait(self, trait: str) -> None:
        # 特征去重(按规范化键)，跨章累并。
        key = _norm(trait)
        if not key:
            return
        if key in self._trait_seen:
            return
        self._trait_seen.add(key)
        self.traits.append(trait)

    def add_arc(self, arc: str) -> None:
        # arc 线索跨章拼接：用 ' / ' 连接各章给出的弧光线索，去重。
        s = (arc or "").strip()
        if not s:
            return
        if not self.arc:
            self.arc = s
            return
        # 避免重复并入同一句。
        if s in self.arc:
            return
        self.arc = self.arc + " / " + s


def _alloc_id(base_slug: str, used_ids: set) -> str:
    """
    基于 slug 分配一个全篇唯一的 char id。

    优先用 'char_' + slug；若已被占用，追加 _2、_3 ... 直到不冲突。
    这保证：相同名字稳定得到相同 id(只要它是第一个用该 slug 的)，
    且任何两个不同人物的 id 必不相同。
    """
    candidate = "char_" + base_slug
    if candidate not in used_ids:
        used_ids.add(candidate)
        return candidate
    # 冲突：从 2 开始加序号。
    n = 2
    while True:
        candidate = "char_" + base_slug + "_" + str(n)
        if candidate not in used_ids:
            used_ids.add(candidate)
            return candidate
        n = n + 1


def _merge(per_chapter: List[dict]) -> StoryBible:
    """
    把各章抽取结果合并成全局一致的 StoryBible(纯 Python，确定性)。

    三类信息分别合并：
      - characters：靠 name/alias 规范化键归一到同一人，union 别名/特征/关系。
      - locations ：按规范化名去重。
      - timeline  ：按出现顺序去重，并赋递增 order。
    """
    # name/alias(规范化) -> char_id 的映射表，是人物归一的核心索引。
    name_to_id: Dict[str, str] = {}
    # char_id -> 累加器，承载合并途中的可变状态。
    accs: Dict[str, _CharAccumulator] = {}
    # 已用 id 集合，给 _alloc_id 判冲突。
    used_ids: set = set()
    # 记录人物首次出现顺序，保证输出顺序稳定可预期(按出场先后)。
    char_order: List[str] = []

    def _resolve_char(name: str, aliases: List[str]) -> Optional[str]:
        """
        给定一个人物的 name 和 aliases，返回它应归属的 char_id。

        逻辑：name 和所有 alias 规范化后逐个查 name_to_id，
        命中任意一个就返回那个已存在的 id(说明是已知人物的不同叫法)；
        全不命中返回 None(说明是新人物)。
        """
        keys = [_norm(name)] + [_norm(a) for a in aliases]
        for k in keys:
            if k and k in name_to_id:
                return name_to_id[k]
        return None

    def _register_keys(char_id: str, name: str, aliases: List[str]) -> None:
        """把一个人物的 name 与所有 alias 都登记到映射表，指向其 id。"""
        for k in [_norm(name)] + [_norm(a) for a in aliases]:
            if k and k not in name_to_id:
                name_to_id[k] = char_id

    # ---- 第一遍：建立所有人物身份并 union 别名/特征/弧光 ----
    for chap in per_chapter:
        for raw_c in chap.get("characters", []):
            if not isinstance(raw_c, dict):
                continue
            name = (raw_c.get("name") or "").strip()
            if not name:
                # 没名字的条目无法归一，跳过。
                continue
            aliases = _as_str_list(raw_c.get("aliases"))

            # 查这个人是不是已知(全名或某个别名命中映射表)。
            char_id = _resolve_char(name, aliases)
            if char_id is None:
                # 新人物：分配稳定 id，建累加器，登记到顺序表。
                char_id = _alloc_id(_slug(name), used_ids)
                accs[char_id] = _CharAccumulator(char_id, name)
                char_order.append(char_id)
            acc = accs[char_id]

            # 把本章这个名字本身也当作潜在别名并入(若与主名不同)。
            acc.add_alias(name)
            for a in aliases:
                acc.add_alias(a)
            for t in _as_str_list(raw_c.get("traits")):
                acc.add_trait(t)
            acc.add_arc(raw_c.get("arc") or "")

            # 关系先暂存(对方此刻可能还没建号)，等第二遍统一解析成 id。
            for rel in _as_list(raw_c.get("relationships")):
                if not isinstance(rel, dict):
                    continue
                to_name = (rel.get("to") or "").strip()
                rel_type = (rel.get("type") or "").strip()
                if not to_name or not rel_type:
                    continue
                acc.rel_pending.append({"to_name": to_name, "type": rel_type})

            # 登记本人的所有 key，让后续章节的别名能回指到这个 id。
            _register_keys(char_id, name, aliases)

    # ---- 第二遍：把关系里的"对方名字"解析成 char_id，并去重 ----
    characters: List[Character] = []
    for char_id in char_order:
        acc = accs[char_id]
        relationships: List[Relationship] = []
        rel_seen = set()
        for rp in acc.rel_pending:
            to_key = _norm(rp["to_name"])
            # 对方名字若在映射表里，解析成其 id；否则保留原名(关系仍有意义，只是未归一)。
            to_id = name_to_id.get(to_key, rp["to_name"])
            # 自己指向自己的关系无意义，丢弃。
            if to_id == char_id:
                continue
            dedup_key = (to_id, rp["type"])
            if dedup_key in rel_seen:
                continue
            rel_seen.add(dedup_key)
            relationships.append(Relationship(to=to_id, type=rp["type"]))

        # 冻结成不可变的 schema Character。
        characters.append(
            Character(
                id=acc.id,
                name=acc.name,
                aliases=acc.aliases,
                traits=acc.traits,
                arc=acc.arc,
                relationships=relationships,
            )
        )

    # ---- 地点合并：按规范化名去重，分配稳定 id ----
    locations: List[Location] = []
    loc_seen: Dict[str, str] = {}
    loc_used_ids: set = set()
    for chap in per_chapter:
        for raw_l in chap.get("locations", []):
            # 地点条目允许是 {"name":...} 或直接字符串，两种都兼容。
            if isinstance(raw_l, dict):
                lname = (raw_l.get("name") or "").strip()
            elif isinstance(raw_l, str):
                lname = raw_l.strip()
            else:
                lname = ""
            if not lname:
                continue
            key = _norm(lname)
            if key in loc_seen:
                continue
            # 分配 loc_ 前缀的稳定唯一 id，复用 _alloc 思路但前缀不同。
            base = _slug(lname)
            candidate = "loc_" + base
            n = 2
            while candidate in loc_used_ids:
                candidate = "loc_" + base + "_" + str(n)
                n = n + 1
            loc_used_ids.add(candidate)
            loc_seen[key] = candidate
            locations.append(Location(id=candidate, name=lname))

    # ---- 时间线合并：按出现顺序去重，赋递增 order ----
    timeline: List[TimePoint] = []
    tp_seen = set()
    order = 0
    for chap in per_chapter:
        for raw_t in chap.get("time_points", []):
            if isinstance(raw_t, dict):
                label = (raw_t.get("label") or "").strip()
            elif isinstance(raw_t, str):
                label = raw_t.strip()
            else:
                label = ""
            if not label:
                continue
            key = _norm(label)
            if key in tp_seen:
                continue
            tp_seen.add(key)
            order = order + 1
            timeline.append(
                TimePoint(id="tp_" + str(order), label=label, order=order)
            )

    # 构造并返回经 pydantic 校验的 StoryBible。
    return StoryBible(
        characters=characters,
        locations=locations,
        timeline=timeline,
    )


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def build_bible(novel: Novel, llm=None) -> StoryBible:
    """
    构建故事圣经(Pass1 入口)。

    参数：
      novel：ingest(Pass0) 的产物，含分章文本。
      llm  ：LLM 客户端(需有 complete(messages, json=False)->str|dict)。
             为 None 时用 get_llm() 取默认 provider 单例。

    流程：分章 LLM 抽取(json 模式) -> 纯 Python 合并 -> StoryBible。
    """
    # llm=None 时延迟导入并取默认客户端(延迟导入避免无密钥环境 import 即失败)。
    if llm is None:
        from app.llm.client import get_llm
        llm = get_llm()

    # 逐章抽取：每章一次 LLM 调用，职责单一，结果可独立缓存。
    per_chapter: List[dict] = []
    for chapter in novel.chapters:
        per_chapter.append(_extract_chapter(chapter, llm))

    # 纯代码合并成全局一致的单一事实源。
    return _merge(per_chapter)
