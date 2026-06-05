# ----------------------------------------------------------------------------
# pipeline/ingest.py —— Pass0：小说全文 -> 分章(Novel) -> 分块(Chunk)
#
# 第一棒。下游 bible/segment/generate 全靠它把原始 .txt 变成结构化的
# Novel(章列表) 与 Chunk(章内带重叠窗口)。
#
# 两条硬约束贯穿全文：
#   1. Chapter.text 必须是"该章原始文本"，不做破坏字符偏移的清洗 ——
#      因为所有溯源偏移都相对该章 text，清洗会让偏移对不上原文。
#   2. 每个 Chunk 必须满足 chapter.text[chunk.start:chunk.end] == chunk.text，
#      即"章内偏移自洽"。这是行级溯源能成立的地基(详见 types.py 注释)。
# ----------------------------------------------------------------------------

from __future__ import annotations

import re
from typing import List, Optional

from app.pipeline.types import Chapter, Chunk, Novel


# ----------------------------------------------------------------------------
# 章节标题正则策略
# ----------------------------------------------------------------------------
# 目标：稳健识别中英文多种章标题，且只匹配"独占一行"的标题，避免把正文里
# 偶然出现的"第一章"误当成新章开头。
#
# 用 re.MULTILINE，让 ^ $ 匹配每一行的行首/行尾，逐行扫描标题行。
#
# 中文分支 _CN_CHAPTER：
#   ^\s*               行首允许少量空白(有的排版会缩进)
#   第                  汉字"第"
#   [0-9零一二三四五六七八九十百千两]+   章号：阿拉伯数字 或 中文数字(含"两/百/千")
#   章                  汉字"章"
#   .*$                 标题后续("三年后的铃铛"等)直到行尾，全部算标题
#   兼容"第一章""第1章""第二十三章"等写法。
#
# 英文分支 _EN_CHAPTER：
#   ^\s*
#   (?:CHAPTER|Chapter) 关键词，re.IGNORECASE 再放宽大小写(CHAPTER II / Chapter 1)
#   \s+
#   (?:[0-9]+|[IVXLCDM]+)  章号：阿拉伯数字 或 罗马数字(I/II/III...)
#   两个分支用 | 合并，整体加 IGNORECASE + MULTILINE。
#
# 不去贪心匹配"Chapter One"(英文单词数字)，因为本项目素材用的是数字/罗马数字，
# 且单词数字易和正文误撞；保持精确优先。若日后需要可再扩。
_CHAPTER_PATTERN = re.compile(
    r"^\s*(?:"
    r"第[0-9零一二三四五六七八九十百千两]+章.*"          # 中文：第X章 ...
    r"|"
    r"(?:CHAPTER|Chapter)\s+(?:[0-9]+|[IVXLCDM]+)\.?.*"  # 英文：Chapter N / CHAPTER II
    r")\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _first_nonempty_line(text: str) -> str:
    """取全文第一条非空行(去掉首尾空白后非空)，作为书名兜底。无则返回空串。"""
    # 按行遍历，逐行 strip 判断是否非空。不用列表推导是为了能提前 return。
    for line in text.splitlines():
        stripped = line.strip()
        if stripped != "":
            return stripped
    return ""


def ingest(text: str, title: Optional[str] = None) -> Novel:
    """
    解析小说全文为 Novel。

    参数：
      text ：小说全文原始文本。
      title：书名；缺省时取全文第一非空行。

    行为：
      1. 用 _CHAPTER_PATTERN 找出所有"独占一行的章标题"，按出现顺序切章。
      2. 标题行本身作为 Chapter.title；该标题行之后、到下一标题行之前的内容
         作为该章 text(去掉标题行那一行，但保留正文原样)。
      3. 若全文没有任何章标记，整篇作为单章(index=1, title="正文")，不报错。
    """
    # 书名兜底：调用方没给就取第一非空行；仍为空则给"未命名"。
    if title is None:
        book_title = _first_nonempty_line(text)
    else:
        book_title = title
    if book_title == "":
        book_title = "未命名"

    # finditer 拿到所有标题行的匹配对象(含在全文中的位置 start()/end())。
    matches = list(_CHAPTER_PATTERN.finditer(text))

    # 情况A：没有任何章标记 -> 退化为单章。
    if len(matches) == 0:
        single = Chapter(index=1, title="正文", text=text)
        return Novel(title=book_title, raw=text, chapters=[single])

    # 情况B：有章标记 -> 逐个标题切片。
    chapters: List[Chapter] = []
    for i, m in enumerate(matches):
        # 标题行原文：用 strip 去掉行首尾空白，得到干净标题(如"第一章 三年后的铃铛")。
        # 注意 strip 只作用于 title 字段，不影响下面 text 的偏移。
        title_line = m.group(0).strip()

        # 该章正文的起点：从标题行末尾(m.end())开始。
        # m.end() 落在标题行的行尾位置，正文从这里往后。
        body_start = m.end()

        # 该章正文的终点：下一个标题的起点(m_next.start())；最后一章则到全文末尾。
        if i + 1 < len(matches):
            body_end = matches[i + 1].start()
        else:
            body_end = len(text)

        # 切出该章正文。这里只在两端做一次 strip("\n")，去掉紧贴标题/章末的换行，
        # 让 text 不以多余空行开头/结尾，便于后续阅读与生成。
        # 重要：strip 后 text 自成一套坐标系，章内偏移(0 起)就相对这个 text，
        # 自洽性由 chunk_chapter 保证、由测试校验，与"全文偏移"无关，故安全。
        body = text[body_start:body_end].strip("\n")

        chapters.append(Chapter(index=i + 1, title=title_line, text=body))

    return Novel(title=book_title, raw=text, chapters=chapters)


def chunk_chapter(chapter: Chapter, max_chars: int = 2000, overlap: int = 150) -> List[Chunk]:
    """
    把一章切成带重叠的上下文窗口。

    参数：
      max_chars：每个窗口最大字符数(应对 LLM 上下文限制)。
      overlap  ：相邻窗口的重叠字符数(让跨窗口的句子/线索不被切断丢失)。

    约定(被测试强制)：每个 Chunk 满足 chapter.text[start:end] == chunk.text。
    """
    chap_text = chapter.text
    n = len(chap_text)

    chunks: List[Chunk] = []

    # 空章直接返回空列表，避免后续死循环。
    if n == 0:
        return chunks

    # 短章(不超过一个窗口)：整章作为一个 chunk，start=0,end=n。
    if n <= max_chars:
        chunks.append(
            Chunk(chapter_index=chapter.index, start=0, end=n, text=chap_text)
        )
        return chunks

    # 步长 = 窗口大小 - 重叠。必须为正，否则窗口无法前进会死循环。
    # 若调用方传入的 overlap >= max_chars(非法)，这里兜底为前进 1 个字符的最小步长，
    # 防御性写法，宁可慢也不死循环。
    step = max_chars - overlap
    if step <= 0:
        step = 1

    start = 0
    while start < n:
        # 窗口右界：start + max_chars，但不超过章末 n。
        end = start + max_chars
        if end > n:
            end = n

        # 切片即窗口文本，天然满足 chap_text[start:end] == 该文本(自洽)。
        piece = chap_text[start:end]
        chunks.append(
            Chunk(chapter_index=chapter.index, start=start, end=end, text=piece)
        )

        # 已经覆盖到章末，结束。
        if end >= n:
            break

        # 向前推进一个步长，下一窗口与本窗口重叠 overlap 个字符。
        start += step

    return chunks


def chunk_novel(novel: Novel, max_chars: int = 2000, overlap: int = 150) -> List[Chunk]:
    """对全书每一章分别切窗，拼成一个总的 Chunk 列表(按章顺序)。"""
    all_chunks: List[Chunk] = []
    for chapter in novel.chapters:
        all_chunks.extend(chunk_chapter(chapter, max_chars=max_chars, overlap=overlap))
    return all_chunks
