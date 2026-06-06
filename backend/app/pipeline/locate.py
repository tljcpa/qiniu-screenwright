# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# pipeline/locate.py —— 共享行级溯源定位器(三级回退)
#
# 背景与动机：
#   segment 的 marker 定位、generate 的 source_quote 定位，原本都用
#   chapter.text.find(quote) 做精确子串匹配。中文命中率高(70%+)，因为中文
#   逐字、标点稳定，LLM 复制原文片段时几乎一字不差。但英文命中率只有 ~10%：
#     - 英文长引语 LLM 容易"顺手改写"(同义替换、调整语序)；
#     - 弯引号(' " ' ")与直引号、连字符、空白(换行/制表/多空格)在 LLM 输出里
#       与原文常不一致；
#     - 大小写偶有出入。
#   只要有一处不一致，精确 find 就整体落空，溯源覆盖率断崖。
#
# 解决：一个共享 locate(text, quote) -> (start, end) | None，三级回退：
#   1) 精确 find —— 中文路径必走这一级且零回退，既有中文行为完全不变；
#   2) 归一化匹配 —— 把 text 与 quote 都做"空白折叠 + 引号/破折号归一 + 大小写
#      归一"，在归一串上 find，再用"归一位置->原位置映射"把命中区间映射回原文
#      的真实偏移。关键不变式：返回的 (start, end) 一定是相对【原始 text】的，
#      调用方 text[start:end] 仍能取回原文片段(可能与 quote 不完全逐字相等，但
#      指向的是原文里语义对应的那一段)；
#   3) 模糊匹配 —— 用 difflib.SequenceMatcher 在原文上滑动找与 quote 最相似的
#      窗口，相似度 > 阈值(默认 0.6)才采纳，否则返回 None。宁缺毋滥：阈值不达
#      标就当作"溯源缺失"，绝不为凑命中率而误配。
#
# 设计纪律：
#   - 中文零回退：归一化对纯中文几乎是恒等变换(中文标点也归一，但中文 LLM 本来
#     就精确命中，走第一级就返回了)，所以中文行为与既有测试不受任何影响。
#   - 偏移永远相对原始 text：第二级用映射表把归一偏移翻译回原偏移，绝不返回
#     归一串上的偏移(那会破坏 chapter.text[start:end] 自洽这条全局不变式)。
# ----------------------------------------------------------------------------

from __future__ import annotations

import difflib
from typing import List, Optional, Tuple


# ----------------------------------------------------------------------------
# 字符归一映射表
# ----------------------------------------------------------------------------
# 把"语义相同、字形不同"的标点统一成一个规范字符。归一只为提高匹配率，不改原文。
# 注意：这里只归一不影响中文精确命中的字符(弯引号、各种破折号、不间断空格等)，
# 中文常用全角标点(，。！？)不在表里，避免对中文做无谓改动。
_CHAR_NORMALIZE_MAP = {
    # 弯单引号(左/右) + 重音符 -> 直单引号
    "‘": "'",
    "’": "'",
    "ʼ": "'",
    "`": "'",
    # 弯双引号(左/右) -> 直双引号
    "“": '"',
    "”": '"',
    # 各种破折号(en/em/figure/horizontal bar) -> 普通连字符减号
    "–": "-",
    "—": "-",
    "‒": "-",
    "―": "-",
    # 省略号 -> 三个点(LLM 常把 ... 与 … 混用)
    "…": "...",
    # 不间断空格 / 窄空格 -> 普通空格(交给空白折叠继续处理)
    " ": " ",
    " ": " ",
    " ": " ",
}

# 被视为"空白"的字符集合(用于空白折叠)。
_WHITESPACE_CHARS = set(" \t\r\n\f\v   ")

# 模糊匹配默认相似度阈值。SequenceMatcher.ratio() 在 [0,1]，0.6 是经验上"明显
# 是同一段被改写"与"碰巧有点像"的分界。调高更稳(漏配)，调低更激进(易误配)。
_DEFAULT_FUZZY_THRESHOLD = 0.6


def _normalize_with_map(text: str) -> Tuple[str, List[int]]:
    """
    把 text 归一化，同时维护"归一串每个字符 -> 原串起始下标"的映射。

    归一规则(逐字符扫描)：
      1) 先按 _CHAR_NORMALIZE_MAP 替换(一个原字符可能展开成多个归一字符，如 … -> ...)；
      2) 连续空白折叠成单个空格；折叠掉的空白不产生归一字符(但其原下标被前一个或
         后一个有效字符的映射覆盖，见下)；
      3) 大小写归一(casefold，比 lower 更稳，能处理德语 ß 等，对中文是恒等)。

    返回 (norm, index_map)：
      - norm：归一后的字符串。
      - index_map：长度等于 len(norm)，index_map[i] 是 norm[i] 对应到【原始 text】
        里的起始字符下标。用它把"归一串上的命中区间"映射回原文真实偏移。

    映射的关键约定(保证 text[start:end] 自洽)：
      - 一个原字符展开成多个归一字符时(… -> ...)，这几个归一字符的 index_map 都
        指向该原字符的下标；
      - 一段连续空白折叠成一个空格时，这个空格的 index_map 指向这段空白的第一个
        原字符下标。映射回原文时再用"下一个非空白归一字符的原下标"修正 end 边界，
        避免把尾随空白错算进区间(见 _map_norm_span_to_original)。
    """
    norm_chars: List[str] = []
    index_map: List[int] = []

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]

        # ---- 空白折叠 ----
        if ch in _WHITESPACE_CHARS:
            # 记录这段空白的起点下标。
            ws_start = i
            # 吃掉整段连续空白。
            while i < n and text[i] in _WHITESPACE_CHARS:
                i = i + 1
            # 折叠成单个空格(归一)。空格的映射指向空白段起点。
            # 优化：串首的空白不产生归一空格(避免归一串以空格开头干扰对齐)；
            # 但为了让 quote 的归一与 text 的归一规则一致，这里仍统一产出一个空格，
            # 由调用方对 quote 做 strip 处理首尾空格(见 locate)。
            norm_chars.append(" ")
            index_map.append(ws_start)
            continue

        # ---- 字符级归一替换 ----
        if ch in _CHAR_NORMALIZE_MAP:
            replacement = _CHAR_NORMALIZE_MAP[ch]
        else:
            replacement = ch
        # 大小写归一。casefold 对中文/数字是恒等。
        replacement = replacement.casefold()

        # 替换可能是多字符(如 … -> ...)，逐个落入归一串，映射全部指向原下标 i。
        for c in replacement:
            norm_chars.append(c)
            index_map.append(i)
        i = i + 1

    return "".join(norm_chars), index_map


def _normalize_query(quote: str) -> str:
    """
    对 quote 只做归一(不需要映射，因为我们只在 text 的归一串上找 quote 的归一串)。

    复用 _normalize_with_map 的归一规则，丢掉映射，再 strip 掉首尾空格——
    首尾空格在折叠后只会是单个空格，strip 掉能让"原文片段两端是否带空白"不影响匹配。
    """
    norm, _ = _normalize_with_map(quote)
    return norm.strip()


def _map_norm_span_to_original(
    norm_start: int,
    norm_end: int,
    index_map: List[int],
    text_len: int,
) -> Tuple[int, int]:
    """
    把归一串上的区间 [norm_start, norm_end) 映射回原始 text 上的区间 [start, end)。

    - start：直接取归一起点字符对应的原下标 index_map[norm_start]。
    - end：取"归一终点(不含)位置"对应的原下标。若 norm_end 落在归一串末尾之后，
      end = text_len；否则 end = index_map[norm_end](即下一个归一字符的原起点)，
      这样自然排除了被折叠的尾随空白，区间右边界落在正确的原字符前。

    返回相对原始 text 的 (start, end)，保证 0 <= start < end <= text_len。
    """
    start = index_map[norm_start]
    if norm_end >= len(index_map):
        end = text_len
    else:
        end = index_map[norm_end]
    # 防御：极端情况下 end 不大于 start 时，至少给一个字符宽度(理论上不会发生，
    # 因为 norm_end > norm_start 且映射单调非减)。
    if end <= start:
        end = start + 1
        if end > text_len:
            end = text_len
    return start, end


def _exact(text: str, quote: str) -> Optional[Tuple[int, int]]:
    """第一级：精确 find。命中返回原文偏移，否则 None。中文主路径走这里。"""
    pos = text.find(quote)
    if pos < 0:
        return None
    return pos, pos + len(quote)


def _normalized(text: str, quote: str) -> Optional[Tuple[int, int]]:
    """
    第二级：归一化匹配。

    把 text 与 quote 都归一，在 text 的归一串上 find quote 的归一串，命中后用
    index_map 把归一区间映射回原文真实偏移。未命中返回 None。
    """
    norm_quote = _normalize_query(quote)
    if not norm_quote:
        return None
    norm_text, index_map = _normalize_with_map(text)
    pos = norm_text.find(norm_quote)
    if pos < 0:
        return None
    norm_start = pos
    norm_end = pos + len(norm_quote)
    return _map_norm_span_to_original(norm_start, norm_end, index_map, len(text))


def _fuzzy(
    text: str,
    quote: str,
    threshold: float,
) -> Optional[Tuple[int, int]]:
    """
    第三级：模糊匹配。在 text 上滑动找与 quote 最相似的窗口。

    做法(在归一空间里比较，命中后映射回原文)：
      - 把 text 与 quote 归一(降低标点/空白/大小写噪声对相似度的干扰)。
      - 用 difflib.SequenceMatcher 找出归一 quote 与归一 text 之间最长的匹配块，
        以它为锚点，取归一 text 上长度约等于归一 quote 的一段窗口作为候选。
      - 计算候选窗口与归一 quote 的 ratio，达阈值才采纳。
      - 用 index_map 把归一窗口区间映射回原文偏移。

    宁缺毋滥：ratio 不达 threshold 一律返回 None(当作溯源缺失)，绝不误配。
    """
    norm_quote = _normalize_query(quote)
    if not norm_quote:
        return None
    norm_text, index_map = _normalize_with_map(text)
    if not norm_text:
        return None

    qlen = len(norm_quote)

    # 用 SequenceMatcher 找归一 quote 在归一 text 里的最长公共子块，作为对齐锚点。
    # autojunk=False 关闭"高频字符当垃圾"的启发式，长文本上更可靠。
    matcher = difflib.SequenceMatcher(None, norm_text, norm_quote, autojunk=False)
    block = matcher.find_longest_match(0, len(norm_text), 0, qlen)
    if block.size == 0:
        # 连一个公共字符都没有，肯定不是同一段。
        return None

    # 锚点：归一 quote 的 block.b 对应到归一 text 的 block.a。
    # 候选窗口起点 = 锚点在 text 的位置 - 锚点在 quote 里的偏移(把 quote 对齐到 text)。
    anchor_text = block.a
    anchor_quote = block.b
    win_start = anchor_text - anchor_quote
    if win_start < 0:
        win_start = 0
    win_end = win_start + qlen
    if win_end > len(norm_text):
        win_end = len(norm_text)
        win_start = win_end - qlen
        if win_start < 0:
            win_start = 0

    # 在锚点附近做小范围微调：既滑动窗口起点(抵消"锚点不在 quote 正中"的对齐偏差)，
    # 也尝试若干窗口长度。后者很关键——原文里这段可能比 quote 略长(原文标点更多、
    # 被归一/折叠的字符更多)，固定 qlen 会把结尾切短，丢掉原句末尾。允许窗口长度
    # 在 qlen 附近浮动，取 ratio 最高者。
    best_ratio = -1.0
    best_span = None
    # 起点滑动半径与窗口长度浮动范围。二者共同决定 SequenceMatcher 调用次数
    # (约 (2*radius+1)*(2*len_margin+1) 次)，对长 quote 收紧上限防止变慢：
    # 短片段(marker)给足浮动空间；长片段(整句 source_quote)适度收窄，长度差通常
    # 只来自标点/空白，十几个字符的浮动足够吸收。
    if qlen <= 40:
        radius = min(qlen // 2 + 1, 20)
        len_margin = min(qlen // 2 + 1, 20)
    else:
        radius = 16
        len_margin = 16
    for delta in range(-radius, radius + 1):
        s = win_start + delta
        if s < 0:
            continue
        for wlen in range(qlen - len_margin, qlen + len_margin + 1):
            if wlen <= 0:
                continue
            e = s + wlen
            if e > len(norm_text):
                continue
            window = norm_text[s:e]
            ratio = difflib.SequenceMatcher(
                None, window, norm_quote, autojunk=False
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_span = (s, e)

    if best_span is None:
        return None
    if best_ratio < threshold:
        # 不达阈值：宁缺毋滥，当作溯源缺失。
        return None

    norm_start, norm_end = best_span
    return _map_norm_span_to_original(norm_start, norm_end, index_map, len(text))


def locate(
    text: str,
    quote: Optional[str],
    fuzzy_threshold: float = _DEFAULT_FUZZY_THRESHOLD,
) -> Optional[Tuple[int, int]]:
    """
    在 text 里定位 quote，返回相对原始 text 的 (start, end)，找不到返回 None。

    三级回退：精确 find -> 归一化匹配 -> 模糊匹配(达阈值才采纳)。
    返回的区间永远满足 text[start:end] 自洽(指向原文里那段)。

    参数：
      text            : 被搜索的原文(如 chapter.text)。
      quote           : 要定位的片段(LLM 复制的 marker / source_quote)；空则 None。
      fuzzy_threshold : 第三级模糊匹配的相似度阈值，默认 0.6。

    中文路径保证：纯中文且 LLM 精确复制时，第一级 find 命中即返回，行为与
    原 chapter.text.find 完全一致，零回退、零行为变化。
    """
    if quote is None:
        return None
    q = quote.strip()
    if not q:
        return None

    # 第一级：精确。中文主路径走这里返回，绝不进入下面两级。
    hit = _exact(text, q)
    if hit is not None:
        return hit

    # 第二级：归一化。
    hit = _normalized(text, q)
    if hit is not None:
        return hit

    # 第三级：模糊。
    return _fuzzy(text, q, fuzzy_threshold)
