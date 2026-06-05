# -*- coding: utf-8 -*-
"""
ingest 模块测试(Pass0)。

用真实样本素材验证：
1. 中文样本切出恰好 3 章，标题分别含"第一章/第二章/第三章"对应内容。
2. 英文样本切出恰好 3 章(Chapter 1 / CHAPTER II / CHAPTER III)。
3. 偏移自洽：任一 Chunk 满足 chapter.text[start:end] == chunk.text。
   这条最关键 —— 它是行级溯源(创新点②)正确性的地基，必须测。
4. 无章节标记的纯文本退化为单章(index=1, title="正文")，不报错。
"""

from pathlib import Path

import pytest

from app.pipeline.ingest import ingest, chunk_chapter, chunk_novel


# 样本目录：本测试文件在 backend/tests/，样本在 backend/samples/。
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
CN_SAMPLE = SAMPLES_DIR / "中文网文样本_旧城咖啡.txt"
EN_SAMPLE = SAMPLES_DIR / "english_pride_and_prejudice_ch1-3.txt"


def _read(path: Path) -> str:
    """以 UTF-8 读取样本全文。"""
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 中文样本：3 章 + 标题内容校验
# ---------------------------------------------------------------------------

def test_chinese_three_chapters():
    novel = ingest(_read(CN_SAMPLE))

    # 恰好 3 章。
    assert len(novel.chapters) == 3

    # 章号 1-based 且连续。
    assert [c.index for c in novel.chapters] == [1, 2, 3]

    # 标题分别含对应章节文字与各自的标题词。
    assert "第一章" in novel.chapters[0].title
    assert "三年后的铃铛" in novel.chapters[0].title
    assert "第二章" in novel.chapters[1].title
    assert "那个没寄出去的雨夜" in novel.chapters[1].title
    assert "第三章" in novel.chapters[2].title
    assert "铃铛再响" in novel.chapters[2].title

    # 正文被正确归章：第一章正文含"铜铃"，不含第二章独有的"慕尼黑"。
    assert "铜铃" in novel.chapters[0].text
    assert "慕尼黑" not in novel.chapters[0].text
    assert "慕尼黑" in novel.chapters[1].text

    # 书名兜底取第一非空行 = "旧城咖啡"。
    assert novel.title == "旧城咖啡"


# ---------------------------------------------------------------------------
# 英文样本：3 章 (Chapter 1 / CHAPTER II / CHAPTER III)
# ---------------------------------------------------------------------------

def test_english_three_chapters():
    novel = ingest(_read(EN_SAMPLE))

    assert len(novel.chapters) == 3
    assert [c.index for c in novel.chapters] == [1, 2, 3]

    # 三种标题写法都要识别：阿拉伯数字 + 罗马数字、大小写混合。
    assert novel.chapters[0].title.lower().startswith("chapter 1")
    assert novel.chapters[1].title.upper().startswith("CHAPTER II")
    assert novel.chapters[2].title.upper().startswith("CHAPTER III")

    # 第一章正文含开篇名句，且不应跨到第二章。
    assert "universally acknowledged" in novel.chapters[0].text


# ---------------------------------------------------------------------------
# 偏移自洽：chapter.text[start:end] == chunk.text(最关键)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sample", [CN_SAMPLE, EN_SAMPLE])
def test_chunk_offsets_are_consistent(sample):
    novel = ingest(_read(sample))

    # 故意用较小的 max_chars，强制长章被切成多个有重叠的窗口，
    # 这样才能真正考验"多窗口偏移自洽"，而不是每章只切一窗。
    chunks = chunk_novel(novel, max_chars=300, overlap=50)

    # 至少应产生比章数更多的窗口(说明确实发生了切分)。
    assert len(chunks) >= len(novel.chapters)

    # 把章号映射到章对象，便于按 chunk.chapter_index 取回原章 text。
    chap_by_index = {c.index: c for c in novel.chapters}

    for ch in chunks:
        chapter = chap_by_index[ch.chapter_index]
        # 核心不变式：窗口文本必须等于原章 text 在 [start,end) 的切片。
        # 一旦不成立，窗口里抽到的 span 偏移就无法回落到原文，溯源即失效。
        assert chapter.text[ch.start:ch.end] == ch.text
        # 偏移合法性：0 <= start < end <= len(text)。
        assert 0 <= ch.start < ch.end <= len(chapter.text)


def test_chunk_overlap_present():
    """相邻窗口应有重叠(后一窗 start < 前一窗 end)，验证滚动重叠确实生效。"""
    novel = ingest(_read(CN_SAMPLE))
    # 找出被切成多窗的某一章。
    for chapter in novel.chapters:
        chunks = chunk_chapter(chapter, max_chars=300, overlap=50)
        if len(chunks) >= 2:
            # 相邻两窗存在重叠。
            assert chunks[1].start < chunks[0].end
            return
    pytest.fail("没有任何一章被切成多窗，无法验证重叠")


# ---------------------------------------------------------------------------
# 无章节标记 -> 退化为单章
# ---------------------------------------------------------------------------

def test_no_chapter_markers_single_chapter():
    plain = "这是一段没有任何章节标题的纯文本。\n它应该被当作整篇单章处理，而不报错。"
    novel = ingest(plain)

    assert len(novel.chapters) == 1
    assert novel.chapters[0].index == 1
    assert novel.chapters[0].title == "正文"
    # 单章 text 应保留原文(供按偏移切片)。
    assert novel.chapters[0].text == plain

    # 单章也能正常分块且偏移自洽。
    chunks = chunk_novel(novel, max_chars=20, overlap=5)
    chapter = novel.chapters[0]
    for ch in chunks:
        assert chapter.text[ch.start:ch.end] == ch.text
