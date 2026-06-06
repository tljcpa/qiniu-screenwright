# -*- coding: utf-8 -*-
"""
locate(三级回退定位器)单元测试。

覆盖：
  1. 第一级精确：命中返回精确偏移；中文路径精确命中(零回退证明)。
  2. 第二级归一化：弯引号/空白折叠/大小写/破折号不一致时仍命中，且偏移映射回原文正确
     (chapter.text[start:end] 取回的是原文真实片段)。
  3. 第三级模糊：LLM 改写过的长引语在阈值内命中；噪声过大时返回 None(宁缺毋滥)。
  4. 边界：空 quote / None / 完全不存在 -> None。

关键不变式贯穿全文：locate 返回的 (start, end) 永远相对【原始 text】，
text[start:end] 必须落在原文上(不破坏 chapter.text[start:end] 自洽)。
"""

from app.pipeline.locate import locate


# ---------------------------------------------------------------------------
# 第一级：精确
# ---------------------------------------------------------------------------

def test_exact_hit_offsets():
    text = "abcdefg hello world"
    hit = locate(text, "hello")
    assert hit is not None
    start, end = hit
    assert text[start:end] == "hello"
    # 精确命中应等于 str.find 的结果。
    assert start == text.find("hello")


def test_exact_chinese_zero_fallback():
    """中文 LLM 精确复制 marker：第一级 find 命中，偏移与 str.find 完全一致。"""
    text = "林深推开木门，铜铃轻响，他看见了她。"
    quote = "铜铃轻响"
    hit = locate(text, quote)
    assert hit is not None
    start, end = hit
    assert (start, end) == (text.find(quote), text.find(quote) + len(quote))
    assert text[start:end] == quote


# ---------------------------------------------------------------------------
# 第二级：归一化(偏移映射回原文正确)
# ---------------------------------------------------------------------------

def test_normalized_curly_quotes():
    """原文用弯引号，quote 用直引号：归一后命中，偏移映射回原文是带弯引号的片段。"""
    # 原文是弯引号。
    text = 'She said “I will not” firmly.'
    # LLM 复制时用了直引号。
    quote = 'She said "I will not"'
    hit = locate(text, quote)
    assert hit is not None
    start, end = hit
    # 关键：映射回原文，取回的是原文(弯引号)那一段，而非归一串。
    fragment = text[start:end]
    assert fragment.startswith("She said")
    assert "“" in fragment and "”" in fragment


def test_normalized_whitespace_collapse():
    """原文里换行/多空格，quote 是单空格紧凑写法：折叠归一后命中，且偏移正确。"""
    text = "He   walked\n  into the\troom slowly."
    quote = "He walked into the room"
    hit = locate(text, quote)
    assert hit is not None
    start, end = hit
    fragment = text[start:end]
    # 原文片段应从 He 开始、覆盖到 room(含其间的换行/制表)。
    assert fragment.startswith("He")
    assert fragment.rstrip().endswith("room")
    # start 是 "He" 在原文的真实下标。
    assert text[start:start + 2] == "He"


def test_normalized_case_and_dash():
    """大小写 + 破折号(em-dash vs hyphen)不一致：归一命中。"""
    text = "It was a TRUTH—universally acknowledged."
    quote = "it was a truth-universally"
    hit = locate(text, quote)
    assert hit is not None
    start, end = hit
    fragment = text[start:end]
    assert fragment.startswith("It was a TRUTH")
    # 含原文的 em-dash。
    assert "—" in fragment


# ---------------------------------------------------------------------------
# 第三级：模糊
# ---------------------------------------------------------------------------

def test_fuzzy_paraphrase_hit():
    """LLM 轻度改写长引语：精确/归一都落空，模糊匹配在阈值内命中并定位到原句区间。"""
    text = (
        "It is a truth universally acknowledged, that a single man "
        "in possession of a good fortune, must be in want of a wife."
    )
    # 改了几个词(单数->复数、调整措辞)，但主体高度相似。
    quote = (
        "It is a truth universally acknowledged that a single man "
        "in possession of a good fortune must be in want of a wife"
    )
    hit = locate(text, quote)
    assert hit is not None
    start, end = hit
    fragment = text[start:end]
    # 命中片段应覆盖原句主体。
    assert "truth universally acknowledged" in fragment
    assert "want of a wife" in fragment


def test_fuzzy_below_threshold_returns_none():
    """完全不相干的 quote：相似度远低于阈值，宁缺毋滥返回 None。"""
    text = "It is a truth universally acknowledged, that a single man."
    quote = "The quick brown fox jumps over the lazy dog repeatedly today."
    hit = locate(text, quote)
    assert hit is None


def test_fuzzy_high_threshold_rejects():
    """阈值可控、宁缺毋滥：把阈值调到极高，明显改写的 quote 被拒。"""
    text = (
        "It is a truth universally acknowledged, that a single man "
        "in possession of a good fortune, must be in want of a wife."
    )
    # 改动较大(替换多个词)，相似但不至于 >0.95。
    quote = (
        "It is a known fact that a wealthy bachelor "
        "is generally assumed to be seeking a spouse."
    )
    hit = locate(text, quote, fuzzy_threshold=0.95)
    assert hit is None


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------

def test_none_and_empty():
    text = "anything"
    assert locate(text, None) is None
    assert locate(text, "") is None
    assert locate(text, "   ") is None


def test_not_found_exact_no_overlap():
    """无任何公共内容时返回 None(模糊也救不回)。"""
    text = "完全是中文内容，没有任何英文。"
    assert locate(text, "zzzzz qqqqq xxxxx wwwww vvvvv") is None


def test_offset_invariant_roundtrip():
    """通用不变式：所有命中区间都满足 0<=start<end<=len(text)。"""
    text = "Para one.\n\nMr. Bennet’s reply was “yes, indeed” to her."
    for quote in ["Mr. Bennet's reply", 'yes, indeed', "para one"]:
        hit = locate(text, quote)
        assert hit is not None, quote
        start, end = hit
        assert 0 <= start < end <= len(text)
        assert len(text[start:end]) > 0
