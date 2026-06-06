# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# pipeline/generate.py —— Pass3：逐场剧本生成
#
# 输入一个 SceneStub(场景骨架) + Novel(原文) + StoryBible(单一事实源) + medium(媒介)，
# 让 LLM 把"本场原文"展开成完整的 Scene(有序 elements 序列)，并落实三大创新点：
#
#   创新点① 可控目标媒介改编(film / series / short_drama)：
#       不同媒介把不同的"风格指令"注入到 prompt 里，让同一份原文按目标载体重渲染。
#       短剧(short_drama)强调快节奏、强钩子、场短、情绪密度、多反转(中国短剧市场打法)。
#
#   创新点② 行级双向溯源：
#       让 LLM 给每个 action/dialogue 元素附一段它"逐字依据"的原文片段(source_quote)，
#       但偏移绝不让模型报——由本模块代码用 chapter.text.find 自己定位，
#       构造 SourceRef(chapter=stub.chapter_index, spans=[Span(start,end)])。
#       定位不到(比如纯外化新增的台词)就 source_ref=None，允许缺失。
#
#   创新点③ 内心戏外化：
#       原文里不可拍的心理描写/内心独白，要外化成 动作/潜台词/画外音/视觉化，
#       并在该元素上打 adaptation = Adaptation(from_="interior_monologue", technique=...)。
#       prompt 明确要求模型识别内心戏并给出外化方式，本模块把它落到 adaptation 字段。
#
# 硬约束(与 types.py 一致)：所有溯源偏移相对"该章 chapter.text"，绝不相对全文，
# 也绝不信任 LLM 报的偏移——偏移一律由代码 find 定位，保证自洽。
# ----------------------------------------------------------------------------

from __future__ import annotations

from typing import Dict, List, Optional

# 复用已定稿的 schema 模型(数据契约单一来源)。
from app.schema.models import (
    Scene,
    Heading,
    SourceRef,
    Span,
    ActionElement,
    DialogueElement,
    TransitionElement,
    Adaptation,
    StoryBible,
    Character,
)
# 复用管线中间类型。
from app.pipeline.types import Novel, Chapter, SceneStub

# 复用 LLM 工厂。
from app.llm.client import get_llm


# ----------------------------------------------------------------------------
# 创新点① —— medium 媒介配置
# ----------------------------------------------------------------------------
# 每种媒介对应一段"风格指令"，会被原样拼进 prompt 的 system 段。
# 这是"同一 bible+scenes 按媒介重渲染"的开关：换 medium 就换一套生成倾向。
#
# 设计取舍：
#   - film(电影)：单场较完整，重镜头叙事与画面，节奏可舒缓，允许铺陈。
#   - series(剧集)：单集 beat 节奏，强调本场在整集结构中的功能(承上启下/留扣子)。
#   - short_drama(短剧)：中国竖屏短剧打法——场要短、开场即钩子、情绪密度高、
#     多反转、台词要"金句感"。这是和友商换皮组拉开差距的差异化卖点之一。
# 把这些写成显式文本而不是散落在代码里，是为了 prompt 可审计、可在测试里断言注入。
_MEDIUM_STYLE: Dict[str, str] = {
    "film": (
        "目标媒介=电影(film)。请按电影感来改编：\n"
        "- 单场结构相对完整，重镜头与画面叙事，可用细节动作铺陈氛围。\n"
        "- 节奏可张可弛，允许必要的留白与情绪铺垫。\n"
        "- 动作描写服务于'可被镜头拍到'，避免文学化的抽象修辞。"
    ),
    "series": (
        "目标媒介=剧集(series)。请按剧集节奏来改编：\n"
        "- 以'单集 beat'的节奏推进，明确本场在本集中的叙事功能(推进/铺垫/留扣子)。\n"
        "- 结尾倾向给出一个让观众想看下一场/下一集的小钩子。\n"
        "- 控制单场体量，避免一场塞太多信息。"
    ),
    "short_drama": (
        "目标媒介=短剧(short_drama)。请按中国竖屏短剧打法来改编：\n"
        "- 快节奏：场要短，开场即抛钩子，不铺垫直接进冲突。\n"
        "- 情绪密度高：每场都要有强情绪点或反转，信息密度大。\n"
        "- 台词要有'金句感'，短促有力、便于传播。\n"
        "- 鼓励多反转、强戏剧冲突，留强悬念收尾。"
    ),
}


def _medium_style(medium: str) -> str:
    """取某媒介的风格指令；未知媒介兜底回退到 film，绝不抛错中断管线。"""
    # 用 if/else 兜底，不用三元运算符(遵循代码风格约定)。
    if medium in _MEDIUM_STYLE:
        return _MEDIUM_STYLE[medium]
    return _MEDIUM_STYLE["film"]


# ----------------------------------------------------------------------------
# 取本场原文
# ----------------------------------------------------------------------------

def _find_chapter(novel: Novel, chapter_index: int) -> Optional[Chapter]:
    """按 index 在 novel.chapters 里找章；找不到返回 None。"""
    for ch in novel.chapters:
        if ch.index == chapter_index:
            return ch
    return None


def _slice_scene_text(chapter: Chapter, source_ref: SourceRef) -> str:
    """
    把 source_ref.spans 在 chapter.text 上切片并按出现顺序拼接，得到本场原文。

    偏移自洽前提(见 types.py)：spans 偏移相对 chapter.text。
    这里做防御性裁剪：把 start/end 夹到 [0, len(text)] 区间内，
    避免上游偶发越界偏移导致切片异常。
    """
    text = chapter.text
    n = len(text)
    parts: List[str] = []
    for span in source_ref.spans:
        start = span.start
        end = span.end
        # 夹紧到合法范围。
        if start < 0:
            start = 0
        if end > n:
            end = n
        if start >= end:
            # 非法/空区间跳过，不拼。
            continue
        parts.append(text[start:end])
    return "".join(parts)


# ----------------------------------------------------------------------------
# 创新点②基础 —— bible 切片
# ----------------------------------------------------------------------------

def _name_to_id_map(bible: StoryBible) -> Dict[str, str]:
    """
    构造"名字/别名 -> character id"映射，给对白说话人回填 id 用。

    覆盖每个角色的 name 与所有 aliases，都指向同一个 id。
    后构造的不覆盖先构造的(用 setdefault)，避免别名碰撞时乱序覆盖。
    """
    mapping: Dict[str, str] = {}
    for ch in bible.characters:
        mapping.setdefault(ch.name, ch.id)
        for alias in ch.aliases:
            mapping.setdefault(alias, ch.id)
    return mapping


def _bible_slice(stub: SceneStub, bible: StoryBible) -> str:
    """
    构造注入 prompt 的 bible 切片：只给"本场相关"的人物与地点，控制 token 且防串戏。

    相关人物判定：stub.characters 里列出的 id 命中的角色。
    若 stub.characters 为空(segment 没填全)，则退化为给全部角色，宁多勿漏。
    地点：把 bible 全部地点的 id->name 都给出(地点条目少，全给便于 heading 映射 location_id)。
    """
    # 先决定要展示哪些角色。
    wanted_ids = set(stub.characters)
    chosen: List[Character] = []
    if wanted_ids:
        for ch in bible.characters:
            if ch.id in wanted_ids:
                chosen.append(ch)
    # 兜底：一个都没匹配上(可能 stub 里存的是名字而非 id，或为空)，给全部。
    if not chosen:
        chosen = list(bible.characters)

    lines: List[str] = []
    lines.append("【相关人物(请用 id 引用)】")
    for ch in chosen:
        # 拼一行人物档案：id / 名字 / 别名 / 性格 / 关系。
        alias_str = "、".join(ch.aliases)
        trait_str = "、".join(ch.traits)
        rel_str = "；".join(["%s(%s)" % (r.to, r.type) for r in ch.relationships])
        lines.append(
            "- id=%s 名字=%s 别名=[%s] 性格=[%s] 关系=[%s]"
            % (ch.id, ch.name, alias_str, trait_str, rel_str)
        )

    lines.append("【可用地点(请用 id 作 location_id)】")
    for loc in bible.locations:
        lines.append("- id=%s 名字=%s" % (loc.id, loc.name))

    return "\n".join(lines)


# ----------------------------------------------------------------------------
# prompt 构造
# ----------------------------------------------------------------------------

def _build_messages(
    stub: SceneStub,
    scene_text: str,
    bible_slice: str,
    prev_tail: str,
    medium: str,
) -> List[dict]:
    """
    组装注入四样东西的 messages：
      ① 本场原文 scene_text
      ② 相关 bible 切片 bible_slice
      ③ prev_tail(上一场结尾，保跨场连贯)
      ④ medium 媒介风格指令

    并明确要求模型：
      - 输出 JSON(配合 LLM.complete(json=True))。
      - 每个 action/dialogue 元素附 source_quote(逐字原文片段)，不报偏移。
      - 识别内心戏并外化，给出 adaptation。
      - 对白说话人用 bible 的 character id。
    """
    medium_style = _medium_style(medium)

    # system：角色设定 + 媒介风格(创新点①注入点) + 输出契约。
    system_parts: List[str] = []
    system_parts.append(
        "你是专业的小说改编编剧。任务：把'本场原文'改写成结构化的单场剧本，"
        "严格只依据给定原文与人物设定，不得虚构原文没有的情节主线。"
    )
    # 创新点① —— 媒介风格指令拼进 system。
    system_parts.append("【媒介风格要求】\n" + medium_style)
    # 输出契约：描述要返回的 JSON 形状与三大规则。
    system_parts.append(
        "【输出要求】只返回一个 JSON 对象，形如：\n"
        "{\n"
        '  "heading": {"int_ext":"INT|EXT|INT/EXT", "location_id":"<地点id>", '
        '"time_of_day":"<时段，用与本场原文相同的语言>", "time_ref": null},\n'
        '  "characters": ["<人物id>", ...],\n'
        '  "synopsis": "本场一句话梗概",\n'
        '  "elements": [\n'
        '    {"type":"action", "text":"动作或场景描述", '
        '"source_quote":"<本场原文里你依据的逐字片段，没有就给空字符串>", '
        '"adaptation": null 或 {"from":"interior_monologue","technique":"subtext|action|voiceover|visual"}},\n'
        '    {"type":"dialogue", "character":"<说话人的人物id>", "line":"台词", '
        '"parenthetical":"括号提示或null", '
        '"source_quote":"<逐字原文片段或空>", "adaptation": null 或 {...}},\n'
        '    {"type":"transition", "text":"CUT TO|FADE OUT|..."}\n'
        "  ]\n"
        "}\n"
        "三条硬规则：\n"
        "1. 行级溯源：每个 action/dialogue 都尽量给 source_quote，"
        "它必须是'本场原文'里出现过的逐字片段(用于代码定位原文偏移)；"
        "若该元素是你为改编新增、原文没有对应文字，则 source_quote 给空字符串。"
        "绝对不要自己报字符偏移数字。\n"
        "2. 内心戏外化：原文里不可拍的心理描写/内心独白(角色没说出口的想法)，"
        "不要直接当旁白照抄，要外化成可拍的动作、潜台词台词、画外音或视觉化处理，"
        "并在该元素上给 adaptation，from 固定为 interior_monologue，"
        "technique 从 subtext/action/voiceover/visual 中选最贴切的。\n"
        "3. 对白的 character 必须是给定人物设定里的 id，不要用名字。\n"
        "4. heading.time_of_day(时段)必须用与'本场原文'相同的语言来写："
        "原文是中文就用 日/夜/黄昏/清晨 等中文词；"
        "原文是英文就用 DAY/NIGHT/DUSK/DAWN 等英文词；"
        "不要把英文原文的时段翻成中文，也不要把中文原文的时段翻成英文。"
    )
    system = "\n\n".join(system_parts)

    # user：四样注入材料(②③①已在 system 体现媒介，这里给原文+bible+上场尾)。
    user_parts: List[str] = []
    # ② bible 切片。
    user_parts.append(bible_slice)
    # ③ 上一场结尾(保连贯)；为空就明示无。
    if prev_tail:
        user_parts.append("【上一场结尾(仅供衔接参考，不要重复写进本场)】\n" + prev_tail)
    else:
        user_parts.append("【上一场结尾】(无，本场为开场)")
    # 给 segment 阶段的提示(梗概/时段/地点线索)，帮助模型补 heading。
    hint_parts: List[str] = []
    if stub.summary:
        hint_parts.append("梗概提示=%s" % stub.summary)
    if stub.time_of_day:
        hint_parts.append("时段提示=%s" % stub.time_of_day)
    if stub.location_hint:
        hint_parts.append("地点线索=%s" % stub.location_hint)
    if hint_parts:
        user_parts.append("【场景提示】" + "；".join(hint_parts))
    # ① 本场原文(放最后，紧贴生成，最显著)。
    user_parts.append("【本场原文】\n" + scene_text)
    user = "\n\n".join(user_parts)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return messages


# ----------------------------------------------------------------------------
# 创新点② —— source_quote 定位成 SourceRef
# ----------------------------------------------------------------------------

def _locate_source_ref(
    source_quote: Optional[str],
    chapter: Chapter,
    chapter_index: int,
) -> Optional[SourceRef]:
    """
    用 chapter.text.find 把 LLM 给的逐字片段定位成 SourceRef。

    关键点(创新点②的代码侧落实)：
      - 偏移由代码定位，绝不信任 LLM 报的数字。
      - 偏移相对 chapter.text(章内自洽)。
      - 找不到(空片段 / 模型改了字 / 纯外化新增) 返回 None —— 允许溯源缺失。

    返回 SourceRef(chapter=chapter_index, spans=[Span(start, end)]) 或 None。
    """
    # 空或非字符串：无从定位。
    if not source_quote:
        return None
    quote = source_quote.strip()
    if not quote:
        return None

    # 在该章原文里找首次出现位置。
    pos = chapter.text.find(quote)
    if pos < 0:
        # 没命中：可能模型对原文做了改写。允许缺失，返回 None。
        return None

    start = pos
    end = pos + len(quote)
    span = Span(start=start, end=end)
    return SourceRef(chapter=chapter_index, spans=[span])


# ----------------------------------------------------------------------------
# 创新点③ —— adaptation 落字段
# ----------------------------------------------------------------------------

# 合法的外化技法集合，用于校验 LLM 给的 technique。
_VALID_TECHNIQUES = {"subtext", "action", "voiceover", "visual"}
# 合法的 from 来源集合。
_VALID_FROM = {"interior_monologue", "narration", "description"}


def _build_adaptation(raw: Optional[dict]) -> Optional[Adaptation]:
    """
    把 LLM 给的 adaptation dict 落成 Adaptation 模型。

    防御性处理：
      - raw 为 None / 非 dict：返回 None(该元素不是外化来的)。
      - from 缺失/非法：默认按 interior_monologue 兜底(本模块主打内心戏外化)。
      - technique 缺失/非法：默认按 subtext 兜底(最通用的外化手法)。

    注意 LLM JSON 里键是 "from"(无下划线)，Adaptation 用 alias="from" +
    populate_by_name，所以这里既可传 from_ 也可传 from；为清晰用 from_=。
    """
    if not isinstance(raw, dict):
        return None

    # 取来源；LLM 给的键是 "from"。
    from_val = raw.get("from")
    if from_val not in _VALID_FROM:
        # 兜底到内心戏外化(本场景化模块的核心来源)。
        from_val = "interior_monologue"

    tech_val = raw.get("technique")
    if tech_val not in _VALID_TECHNIQUES:
        tech_val = "subtext"

    return Adaptation(from_=from_val, technique=tech_val)


# ----------------------------------------------------------------------------
# elements 解析
# ----------------------------------------------------------------------------

def _parse_elements(
    raw_elements: list,
    chapter: Chapter,
    chapter_index: int,
    name_to_id: Dict[str, str],
) -> List:
    """
    把 LLM 返回的 elements 列表逐个解析成 ActionElement/DialogueElement/TransitionElement。

    - action：text + 代码定位的 source_ref + 可选 adaptation。
    - dialogue：character 经 name_to_id 兜底映射成 id；line/parenthetical；source_ref；adaptation。
    - transition：仅 text。
    未知 type 跳过(宁缺勿造非法元素)。保持原始顺序(elements 的有序性是 schema 核心)。
    """
    out: List = []
    for item in raw_elements:
        # 非 dict 的脏数据跳过。
        if not isinstance(item, dict):
            continue
        etype = item.get("type")

        if etype == "action":
            text = item.get("text")
            if not text:
                # 没正文的 action 无意义，跳过。
                continue
            source_ref = _locate_source_ref(item.get("source_quote"), chapter, chapter_index)
            adaptation = _build_adaptation(item.get("adaptation"))
            out.append(
                ActionElement(
                    type="action",
                    text=text,
                    source_ref=source_ref,
                    adaptation=adaptation,
                )
            )

        elif etype == "dialogue":
            line = item.get("line")
            if not line:
                continue
            # 说话人映射成 bible id：先看是不是已经就是合法 id，再按名字映射，最后原名兜底。
            raw_char = item.get("character")
            character = _resolve_character(raw_char, name_to_id)
            # parenthetical 允许 None。
            parenthetical = item.get("parenthetical")
            if parenthetical == "":
                parenthetical = None
            source_ref = _locate_source_ref(item.get("source_quote"), chapter, chapter_index)
            adaptation = _build_adaptation(item.get("adaptation"))
            out.append(
                DialogueElement(
                    type="dialogue",
                    character=character,
                    line=line,
                    parenthetical=parenthetical,
                    source_ref=source_ref,
                    adaptation=adaptation,
                )
            )

        elif etype == "transition":
            text = item.get("text")
            if not text:
                continue
            out.append(TransitionElement(type="transition", text=text))

        else:
            # 未知类型：跳过，不构造非法元素。
            continue

    return out


def _resolve_character(raw_char, name_to_id: Dict[str, str]) -> str:
    """
    把 LLM 给的说话人解析成 character id。

    优先级：
      1. raw_char 本就是某角色 id(出现在 name_to_id 的值集合里)：直接用。
      2. raw_char 是名字/别名(命中 name_to_id 的键)：映射成对应 id。
      3. 都不命中：用原文兜底(直接返回 raw_char)，绝不丢失说话人信息。
      4. raw_char 为空：返回空串(让 schema 仍能构造，避免 KeyError)。
    """
    if not raw_char:
        return ""
    # 已经是合法 id？
    if raw_char in name_to_id.values():
        return raw_char
    # 是名字/别名？
    if raw_char in name_to_id:
        return name_to_id[raw_char]
    # 兜底：原样返回。
    return raw_char


# ----------------------------------------------------------------------------
# 主函数：generate_scene
# ----------------------------------------------------------------------------

def generate_scene(
    stub: SceneStub,
    novel: Novel,
    bible: StoryBible,
    medium: str = "film",
    prev_tail: str = "",
    llm=None,
    language: Optional[str] = None,
) -> Scene:
    """
    逐场生成(Pass3)：把一个 SceneStub 展开成完整 Scene。

    流程：
      1. 找本场所在章，按 stub.source_ref.spans 切出本场原文 scene_text。
      2. 构造 bible 切片(本场相关人物/地点)与 prompt(注入四样：原文/bible/上场尾/媒介)。
      3. LLM json 模式返回结构化场景。
      4. 解析 heading / characters / elements；对每个元素用代码定位 source_ref(创新点②)，
         把内心戏外化落到 adaptation(创新点③)，对白说话人映射成 bible id。
      5. 用 pydantic 构造并校验 Scene(id 用 stub.id)返回。

    language(可选)：本场原文语言提示，仅用于 heading.time_of_day 的兜底默认值。
      默认 None(保持中文 "日" 兜底，既有中文行为不变)；
      调用方传 "en" 时，模型缺省时段会兜底成英文 "DAY" 而非中文。
      时段语言的首要保证仍在 prompt 里(要求模型用原文语言写时段)，
      这个参数只是兜底层的兜底，避免英文输入因模型漏填而回退出中文。
    """
    # llm 为空时取默认单例。
    if llm is None:
        llm = get_llm()

    # 1. 找章 + 切原文。
    chapter = _find_chapter(novel, stub.chapter_index)
    if chapter is None:
        # 找不到章是上游严重错误，明确报错而不是静默生成空场。
        raise ValueError("generate_scene: 找不到 chapter_index=%d" % stub.chapter_index)
    scene_text = _slice_scene_text(chapter, stub.source_ref)

    # 2. bible 切片 + prompt。
    bible_slice = _bible_slice(stub, bible)
    name_to_id = _name_to_id_map(bible)
    messages = _build_messages(stub, scene_text, bible_slice, prev_tail, medium)

    # 3. LLM json 模式。temperature 留默认(client 内默认 0.0，确定性 + 可缓存)。
    data = llm.complete(messages, json=True)
    if not isinstance(data, dict):
        # 极端情况下兜底成空结构，避免后续 KeyError。
        data = {}

    # 4a. heading。把 language 透传给兜底逻辑，决定缺省时段的语言。
    heading = _build_heading(data.get("heading"), stub, bible, language=language)

    # 4b. characters：优先用 LLM 给的，逐个 resolve 成 id；为空回退到 stub.characters。
    raw_chars = data.get("characters")
    characters: List[str] = []
    if isinstance(raw_chars, list):
        for c in raw_chars:
            cid = _resolve_character(c, name_to_id)
            if cid and cid not in characters:
                characters.append(cid)
    if not characters:
        characters = list(stub.characters)

    # 4c. elements。
    raw_elements = data.get("elements")
    if not isinstance(raw_elements, list):
        raw_elements = []
    elements = _parse_elements(raw_elements, chapter, stub.chapter_index, name_to_id)

    # synopsis。
    synopsis = data.get("synopsis")
    if not isinstance(synopsis, str):
        synopsis = stub.summary

    # 5. 构造并校验 Scene(场级 source_ref 复用 stub 的)。
    scene = Scene(
        id=stub.id,
        heading=heading,
        source_ref=stub.source_ref,
        characters=characters,
        synopsis=synopsis,
        elements=elements,
        continuity_flags=[],
    )
    return scene


# 中文时段词 -> 英文 slugline 时段词的映射(仅英文输入时用于归一)。
# 覆盖常见时段；键为可能出现在中文输出里的子串。
_ZH_TO_EN_TIME = {
    "清晨": "DAWN",
    "黎明": "DAWN",
    "拂晓": "DAWN",
    "早晨": "MORNING",
    "上午": "MORNING",
    "黄昏": "DUSK",
    "傍晚": "DUSK",
    "午后": "AFTERNOON",
    "下午": "AFTERNOON",
    "深夜": "NIGHT",
    "夜晚": "NIGHT",
    "夜": "NIGHT",
    "日": "DAY",
    "白天": "DAY",
}


def _has_cjk(text: str) -> bool:
    """判断字符串里是否含有 CJK 汉字(用于检测英文产出里混入的中文时段)。"""
    for ch in text:
        # 基本汉字区间 U+4E00..U+9FFF，足够覆盖时段词。
        if "一" <= ch <= "鿿":
            return True
    return False


def _normalize_time_of_day_en(value: str) -> str:
    """
    英文输入时把时段值归一成英文。

    规则：
      - value 不含中文：原样返回(已是英文，如 DAY/evening，不强行改大小写)。
      - value 含中文：按 _ZH_TO_EN_TIME 逐键匹配(子串命中即替换)，命中返回对应英文；
        都不命中(罕见生僻表达)兜底为 "DAY"，绝不把中文时段漏到英文 slugline 里。
    """
    if not value:
        return "DAY"
    if not _has_cjk(value):
        # 已是英文(或不含中文)，不动。
        return value
    # 含中文：按映射子串匹配。
    for zh_word in _ZH_TO_EN_TIME:
        if zh_word in value:
            return _ZH_TO_EN_TIME[zh_word]
    # 含中文但无法识别具体时段：兜底为 DAY。
    return "DAY"


def _build_heading(
    raw: Optional[dict],
    stub: SceneStub,
    bible: StoryBible,
    language: Optional[str] = None,
) -> Heading:
    """
    构造 Heading，对 LLM 给的脏/缺数据做兜底，保证一定能通过 schema 校验。

    - int_ext：非法值兜底为 INT(最常见)。
    - location_id：优先用 LLM 给的；非法/空时退化到 bible 第一个地点 id；
      bible 也没地点时给占位 "loc_unknown"。
    - time_of_day：优先 LLM > stub.time_of_day > 语言相关默认值。
      默认值随 language 走：language=="en" 用 "DAY"，其余(含 None)仍用中文 "日"，
      保证既有中文行为不变，英文输入时不再回退出中文时段。
    - time_ref：透传(可为 None)。
    """
    if not isinstance(raw, dict):
        raw = {}

    # int_ext 兜底。
    int_ext = raw.get("int_ext")
    if int_ext not in ("INT", "EXT", "INT/EXT"):
        int_ext = "INT"

    # location_id 兜底。
    valid_loc_ids = {loc.id for loc in bible.locations}
    location_id = raw.get("location_id")
    if not location_id or location_id not in valid_loc_ids:
        if bible.locations:
            location_id = bible.locations[0].id
        else:
            location_id = "loc_unknown"

    # time_of_day 兜底。
    time_of_day = raw.get("time_of_day")
    if not time_of_day:
        if stub.time_of_day:
            time_of_day = stub.time_of_day
        else:
            # 兜底默认值随语言走：英文输入回退到 "DAY"，否则保持中文 "日"。
            if language == "en":
                time_of_day = "DAY"
            else:
                time_of_day = "日"

    # 语言一致性兜底：英文输入时，把模型偶发回的中文时段词归一成英文。
    # prompt 已要求时段随原文语言，但 LLM 偶尔仍漏译个别场(实测 sc_001 会回 "日")，
    # 这里做最后一道代码侧防线，保证英文产物的 slugline 时段一定是英文。
    # 只在 language=="en" 时介入；中文/默认路径完全不动，既有中文行为零影响。
    if language == "en":
        time_of_day = _normalize_time_of_day_en(time_of_day)

    # time_ref 透传。
    time_ref = raw.get("time_ref")
    if time_ref == "":
        time_ref = None

    return Heading(
        int_ext=int_ext,
        location_id=location_id,
        time_of_day=time_of_day,
        time_ref=time_ref,
    )


# ----------------------------------------------------------------------------
# 任务二：驱动器 generate —— 按顺序逐场生成，传递 prev_tail 保跨场连贯
# ----------------------------------------------------------------------------

def _scene_tail(scene: Scene, max_elements: int = 2) -> str:
    """
    取一场最后 1~2 个元素的文本，作为下一场的 prev_tail。

    取 action.text / dialogue("说话人: 台词") / transition.text，按原顺序拼。
    用于让下一场生成时知道"上一场是怎么收的"，保跨场叙事连贯。
    """
    if not scene.elements:
        return ""
    tail_elems = scene.elements[-max_elements:]
    lines: List[str] = []
    for el in tail_elems:
        if isinstance(el, ActionElement):
            lines.append(el.text)
        elif isinstance(el, DialogueElement):
            lines.append("%s：%s" % (el.character, el.line))
        elif isinstance(el, TransitionElement):
            lines.append(el.text)
    return "\n".join(lines)


def generate(
    novel: Novel,
    bible: StoryBible,
    stubs: List[SceneStub],
    medium: str = "film",
    llm=None,
    language: Optional[str] = None,
) -> List[Scene]:
    """
    管线驱动器：按 stubs 顺序逐场生成 Scene。

    跨场连贯做法：把上一场最后 1~2 个元素的文本当作下一场的 prev_tail 注入，
    让相邻场之间衔接自然(创新点不直接，但是产品质量基础)。
    第一场 prev_tail 为空串。

    language(可选)：透传给 generate_scene，仅影响 heading.time_of_day 的兜底默认值
      (英文 "en" 兜底成 "DAY"，默认 None 保持中文 "日")。不传则维持既有中文行为。
    """
    if llm is None:
        llm = get_llm()

    scenes: List[Scene] = []
    prev_tail = ""
    for stub in stubs:
        scene = generate_scene(
            stub=stub,
            novel=novel,
            bible=bible,
            medium=medium,
            prev_tail=prev_tail,
            llm=llm,
            language=language,
        )
        scenes.append(scene)
        # 更新 prev_tail 供下一场。
        prev_tail = _scene_tail(scene)
    return scenes
