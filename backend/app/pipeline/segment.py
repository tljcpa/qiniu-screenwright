# ----------------------------------------------------------------------------
# pipeline/segment.py —— Pass2 场景切分
#
# 职责：把每一章按"时空连续(同一时间、同一地点为一场)"切成若干场景骨架
# (SceneStub)。每个 SceneStub 带 source_ref，其字符偏移相对该章 Chapter.text，
# 满足不变式 chapter.text[span.start:span.end] 取回该场原文。
#
# ============================================================================
# 核心设计：为什么用 marker 定位，而不让 LLM 直接报字符偏移？
# ============================================================================
# 大模型本质是按 token 预测下一个 token 的，它对"第几个字符"这种逐字计数没有
# 可靠的内部表示。让它直接输出 start=137、end=842 这类字符偏移，模型几乎一定数
# 错——尤其中文，一个汉字一个字符，长文里偏差能到几十上百字符。一旦偏移错了，
# chapter.text[start:end] 取出的就是错位的原文，整个"行级双向溯源"(创新点②)
# 的地基就塌了：前端高亮会指向错误段落。
#
# 正确做法是把"定位"这件确定性的事交还给代码：
#   1. LLM 只负责"语义判断"——它擅长的事：这一章在哪里发生了时空切换、每场
#      的开头/结尾逐字长什么样(start_marker / end_marker，各 10-20 字原文)。
#      复制一小段原文，模型是可靠的(它在做"抄写"而非"计数")。
#   2. 代码负责"算偏移"——用 chapter.text.find(marker) 在原文里精确定位。
#      find 是 Python 内核级的精确子串匹配，零误差。
# 这样把不确定的语义判断和确定的数值计算解耦，各取所长，偏移永远自洽。
#
# ============================================================================
# 兜底契约(钉死)：marker 找不到时，spans 仍必须连续覆盖、不越界、start<end
# ============================================================================
# LLM 复制的 marker 可能因为它自作主张改了个标点、漏了个字而 find 不到。这时
# 不能让该场塌掉，而是用"连续接续"兜底：
#   - start 找不到：该场从上一场末尾接续(第一场则从章首 0 接续)。
#   - end   找不到：该场延伸到下一场 start 之前(最后一场则到章末 len(text))。
# 最终保证：所有场按出现顺序首尾相接、覆盖整章、每个 0<=start<end<=len(text)。
# ----------------------------------------------------------------------------

from __future__ import annotations

from typing import List, Optional

# 复用 schema 的溯源类型，不另起一套(溯源契约全篇单一来源)。
from app.schema.models import SourceRef, Span, StoryBible
# 复用管线中间类型。
from app.pipeline.types import Novel, Chapter, SceneStub
# LLM 工厂；llm 参数为 None 时回退到它(在线路径)。
from app.llm.client import get_llm
# 共享三级回退定位器(精确->归一->模糊)。中文精确命中走第一级，行为不变；
# 英文因 LLM 改写/标点空白不一致时由后两级兜底，显著提升溯源命中率。
from app.pipeline.locate import locate as _shared_locate


# 给 LLM 的系统提示：钉死它的职责边界——只判断切分点 + 复制 marker，绝不报偏移。
_SYSTEM_PROMPT = (
    "你是专业的影视剧本场景切分助手。给你一章小说原文，"
    "你要按【时空连续】原则切分场景：同一时间、同一地点的连续叙事算一场，"
    "时间跳转或地点转移就开新场。\n"
    "对每一场，你只需返回：\n"
    "- start_marker：该场在原文里【开头】的一小段【逐字原文】(10到20个字，必须与原文一字不差)。\n"
    "- end_marker：该场在原文里【结尾】的一小段【逐字原文】(10到20个字，必须与原文一字不差)。\n"
    "- characters：该场出场人物的名字列表(用原文里出现的称呼)。\n"
    "- summary：该场一句话梗概。\n"
    "- time_of_day：日/夜/黄昏/清晨 之一(无法判断填空串)。\n"
    "- location_hint：地点线索(自由文本，无法判断填空串)。\n"
    "严禁返回任何字符位置/偏移数字。marker 必须是从原文里【复制】的连续片段，"
    "不要改写、不要加省略号、不要合并不相邻的句子。\n"
    '返回 JSON：{"scenes": [{"start_marker": "...", "end_marker": "...", '
    '"characters": [...], "summary": "...", "time_of_day": "...", "location_hint": "..."}]}。\n'
    "场景按在原文中出现的先后顺序排列。"
)


def _build_user_prompt(chapter: Chapter) -> str:
    """拼出某一章的用户提示。把章标题与正文原样喂给模型。"""
    # 标题单独给一行，正文整段给出。不对正文做任何清洗，保证 marker 能在 text 里找到。
    return "章标题：%s\n\n正文：\n%s" % (chapter.title, chapter.text)


def _char_name_to_id(bible: Optional[StoryBible]) -> dict:
    """预构建"人物名/别名 -> character id"的查找表。

    匹配规则：name 与每个 alias 都映射到该角色 id。匹配不到的名字，
    调用方用原名兜底当 id(见 _map_characters)。bible 为 None 时返回空表，
    一切名字都走原名兜底。
    """
    table = {}
    if bible is None:
        return table
    # 遍历 bible 里每个角色，把它的正名和所有别名都登记到同一个 id。
    for ch in bible.characters:
        table[ch.name] = ch.id
        for alias in ch.aliases:
            table[alias] = ch.id
    return table


def _map_characters(names: List[str], name_table: dict) -> List[str]:
    """把 LLM 报的出场人物名列表映射成 character id 列表。

    命中查找表用 bible 的稳定 id；命中不到就用原名当 id 兜底(满足"匹配不到
    用原名作 id")。顺带去重并保持首次出现顺序。
    """
    result = []
    seen = set()
    for raw_name in names:
        # 去掉首尾空白，空名直接跳过。
        name = raw_name.strip()
        if not name:
            continue
        if name in name_table:
            cid = name_table[name]
        else:
            # 兜底：bible 里没有这个人，就用原名当 id，不丢信息。
            cid = name
        # 去重：同一 id 只保留一次。
        if cid in seen:
            continue
        seen.add(cid)
        result.append(cid)
    return result


def _locate(text: str, marker: Optional[str], search_from: int) -> int:
    """在 text 里从 search_from 起找 marker 的起始下标，找不到返回 -1。

    从 search_from 起找(而非从 0)，是为了避免同一段文字在前文重复出现时定位
    到错误的早先位置——场景是顺序推进的，下一场的 marker 必在上一场之后。
    marker 为空/None 时直接当作找不到。

    定位走共享三级回退 locate(精确->归一->模糊)：
      - 在 text[search_from:] 这个子串上定位(保持"顺序推进、不回头"语义)，
        命中后把子串内偏移加回 search_from，换算成相对整章 text 的真实下标。
      - 中文 LLM 精确复制 marker 时第一级 find 命中，行为与原 text.find 完全一致；
      - 英文 marker 被改写/标点空白不一致时由归一/模糊兜底，提升命中率。
    返回相对整章 text 的起始下标(end 由调用方按命中片段在原文上重新定界，见下)。
    """
    if marker is None:
        return -1
    m = marker.strip()
    if not m:
        return -1
    # 只在 search_from 之后的子串里找，等价于原 text.find(m, search_from) 的窗口语义。
    sub = text[search_from:]
    hit = _shared_locate(sub, m)
    if hit is None:
        return -1
    sub_start, _sub_end = hit
    return search_from + sub_start


def _locate_span(text: str, marker: Optional[str], search_from: int):
    """在 text[search_from:] 上定位 marker，返回相对整章 text 的 (start, end)，找不到 None。

    与 _locate 的区别：连同 end 一起返回。end 用"命中片段在原文上的真实右边界"，
    而不是 start+len(marker)。这点在归一/模糊命中时很关键——LLM 给的 marker 可能
    与原文长度不同(改写、标点差异)，若仍用 start+len(marker.strip()) 会切错边界，
    破坏 chapter.text[start:end] 自洽。精确命中时二者等价，中文行为不变。
    """
    if marker is None:
        return None
    m = marker.strip()
    if not m:
        return None
    sub = text[search_from:]
    hit = _shared_locate(sub, m)
    if hit is None:
        return None
    sub_start, sub_end = hit
    return search_from + sub_start, search_from + sub_end


def segment(novel: Novel, bible: Optional[StoryBible] = None, llm=None) -> List[SceneStub]:
    """把整部小说逐章切成场景骨架列表。

    参数：
      novel：ingest(Pass0)的产物，含分章后的 chapters。
      bible：StoryBible，用于把出场人物名映射成稳定 character id；可为 None。
      llm  ：LLM 客户端；None 时用 get_llm() 取默认(在线路径)。测试注入 fake llm。

    返回：list[SceneStub]，场景 id 全局连续编号 sc_001、sc_002…(跨章递增)。
          每个 stub 的 source_ref.chapter 为该章 index，spans 偏移相对该章 text，
          满足 chapter.text[start:end] 自洽，且同章内各场首尾相接、覆盖全章。
    """
    # llm 缺省时回退到默认客户端。测试会显式传 fake llm，不触发这里。
    if llm is None:
        llm = get_llm()

    # 预建人物名->id 查找表，整部小说共用一份。
    name_table = _char_name_to_id(bible)

    # 全局场景计数器，跨章连续递增，用于生成 sc_001 这样的 id。
    scene_counter = 0
    stubs: List[SceneStub] = []

    # 逐章处理。
    for chapter in novel.chapters:
        text = chapter.text
        text_len = len(text)

        # 空章(没有正文)跳过，不产生场景，避免造出 start==end 的空 span。
        if text_len == 0:
            continue

        # ---- 1. 问 LLM 要这一章的场景列表(只含 marker 与提示字段) ----
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(chapter)},
        ]
        # json 模式：客户端用 response_format 强制 JSON，返回 dict。
        data = llm.complete(messages, json=True)

        # 容错取出 scenes 列表。模型万一不给 scenes 键，就当本章一整段为一场。
        raw_scenes = None
        if isinstance(data, dict):
            raw_scenes = data.get("scenes")
        if not isinstance(raw_scenes, list) or len(raw_scenes) == 0:
            # 兜底：整章作为单独一场，marker 都缺失，靠下面的偏移兜底覆盖全章。
            raw_scenes = [{}]

        # ---- 2. 先把每场的 start 偏移用 marker 定位算出来 ----
        # cursor 是"下一场 start 至少从哪里开始找"的游标，保证顺序推进、不回头。
        cursor = 0
        # starts[i] 是第 i 场的起始偏移(已应用兜底)。
        starts: List[int] = []
        for raw in raw_scenes:
            start_marker = None
            if isinstance(raw, dict):
                start_marker = raw.get("start_marker")
            pos = _locate(text, start_marker, cursor)
            if pos < 0:
                # start 找不到：从上一场末尾(即当前 cursor)接续。
                # 第一场时 cursor 为 0，等于从章首接续。
                start = cursor
            else:
                start = pos
            # 单调修正：万一 find 命中了一个比游标还靠前的位置(理论上不会，
            # 因为我们从 cursor 起找)，强制不回退，保证 starts 非递减。
            if start < cursor:
                start = cursor
            starts.append(start)
            # 游标推进到这一场起点之后，至少 +1，避免下一场又卡在同一起点。
            cursor = start + 1
            # 游标不能越过章末。
            if cursor > text_len:
                cursor = text_len

        # ---- 3. 再算每场的 end 偏移 ----
        # 规则：第 i 场的 end 优先用它自己的 end_marker 定位(其结尾位置 = 命中处
        # + marker 长度)；定位不到则兜底到"下一场 start 之前"，最后一场兜底到章末。
        # 同时强制 end <= 下一场 start，保证不重叠、整章被连续覆盖。
        n = len(starts)
        for i in range(n):
            raw = raw_scenes[i]
            start = starts[i]

            # 下一场的起点；最后一场则用章末作为上界。
            if i + 1 < n:
                next_start = starts[i + 1]
            else:
                next_start = text_len

            end_marker = None
            if isinstance(raw, dict):
                end_marker = raw.get("end_marker")
            # end_marker 从本场 start 之后开始找，避免匹配到本场之前的文字。
            # 用 _locate_span 同时拿到命中片段在原文上的真实右边界 mend，
            # 而非 start+len(marker)——归一/模糊命中时长度可能与原文不一致。
            mspan = _locate_span(text, end_marker, start)
            if mspan is None:
                # end 找不到：兜底到下一场 start 之前(最后一场即章末)。
                end = next_start
            else:
                # 命中：end 取命中片段在原文上的真实结尾(含整个 marker)。
                _mstart, end = mspan

            # 约束 1：end 不得超过下一场起点，否则会与下一场重叠、破坏连续覆盖。
            if end > next_start:
                end = next_start
            # 约束 2：end 不得越过章末。
            if end > text_len:
                end = text_len
            # 约束 3：保证 start < end。若兜底后塌成空区间(start==end，常见于
            # 相邻两场 start 相同)，至少给 1 个字符宽度并顺延，避免空 span。
            if end <= start:
                end = start + 1
                if end > text_len:
                    end = text_len
                    # 极端情形：start 已在章末，无法再扩。把 start 回退一格保证宽度。
                    if start >= text_len:
                        start = text_len - 1
                        starts[i] = start

            # ---- 4. 组装该场的 SceneStub ----
            scene_counter += 1
            scene_id = "sc_%03d" % scene_counter  # sc_001 三位补零，全局连续

            # 出场人物名 -> id。
            if isinstance(raw, dict):
                raw_chars = raw.get("characters")
            else:
                raw_chars = None
            if not isinstance(raw_chars, list):
                raw_chars = []
            characters = _map_characters(raw_chars, name_table)

            # 其余提示字段，带缺省。
            summary = ""
            time_of_day = ""
            location_hint = ""
            if isinstance(raw, dict):
                summary = str(raw.get("summary") or "")
                time_of_day = str(raw.get("time_of_day") or "")
                location_hint = str(raw.get("location_hint") or "")

            # source_ref：chapter 填该章 index，单段 span 落在该章 text 上。
            source_ref = SourceRef(
                chapter=chapter.index,
                spans=[Span(start=start, end=end)],
            )

            stub = SceneStub(
                id=scene_id,
                chapter_index=chapter.index,
                source_ref=source_ref,
                characters=characters,
                summary=summary,
                time_of_day=time_of_day,
                location_hint=location_hint,
            )
            stubs.append(stub)

    return stubs
