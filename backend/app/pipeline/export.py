# -*- coding: utf-8 -*-
"""
导出器(export pipeline) —— 把 Screenplay 渲染成三种交付格式。

提供三个入口：
1. to_yaml(sp)        : 主交付格式。直接委托 Screenplay.to_yaml()，做统一入口。
2. to_fountain(sp)    : 业界标准的 Fountain 纯文本剧本格式(给可信度加分)。
3. to_pdf(sp, path)   : 用 fpdf2 渲染 PDF，支持中文(CJK)。

设计要点：
- id -> name 解析：Screenplay 内部一律用稳定 id 引用(location_id / character id)，
  但人类可读的剧本要显示 name。所以导出时必须把 id 经 story_bible 反查成 name。
  为此先把 locations / characters 各建一张 {id: name} 字典(只建一次，O(1) 查)。
- Fountain 映射严格遵循 BRIEF 第6节的 Schema 和 Fountain 业界约定(见 to_fountain)。
"""

from __future__ import annotations

from typing import Dict

from app.schema.models import Screenplay


# ----------------------------------------------------------------------------
# id -> name 解析辅助
# ----------------------------------------------------------------------------

def _build_location_name_map(sp: Screenplay) -> Dict[str, str]:
    """
    建立 {location_id: location_name} 字典。

    Heading.location_id 存的是稳定 id(如 loc_office)，
    导出剧本时要显示人类可读的地点名(如 "总经理办公室")，
    所以预先把 story_bible.locations 摊平成查表用的字典。
    """
    # 用字典推导：遍历 bible 里的每个 Location，键是 id，值是 name。
    name_map: Dict[str, str] = {}
    for loc in sp.story_bible.locations:
        name_map[loc.id] = loc.name
    return name_map


def _build_character_name_map(sp: Screenplay) -> Dict[str, str]:
    """
    建立 {character_id: character_name} 字典。

    DialogueElement.character 存的是 character id(如 char_lin)，
    Fountain 的说话人行要显示 name(如 "林川")，所以同样预建查表字典。
    """
    name_map: Dict[str, str] = {}
    for ch in sp.story_bible.characters:
        name_map[ch.id] = ch.name
    return name_map


def _resolve(name_map: Dict[str, str], key: str) -> str:
    """
    通用解析：在 name_map 里查 key 对应的 name。

    契约要求"找不到就用 id"降级，所以用 dict.get(key, key)：
    - 命中：返回 name。
    - 未命中：返回 key 本身(即原始 id)，保证不丢信息也不抛异常。
    """
    return name_map.get(key, key)


# ----------------------------------------------------------------------------
# 1. YAML —— 主交付格式
# ----------------------------------------------------------------------------

def to_yaml(sp: Screenplay) -> str:
    """
    导出 YAML(主交付格式)。

    直接委托 Screenplay.to_yaml()。保留此函数是为了让 export 模块成为
    所有导出格式的统一入口(调用方只 import 这一个模块即可)。
    """
    # 委托给模型自带的序列化(by_alias / exclude_none / allow_unicode 都已在模型内处理)。
    return sp.to_yaml()


# ----------------------------------------------------------------------------
# 2. Fountain —— 业界标准纯文本剧本格式
# ----------------------------------------------------------------------------

def to_fountain(sp: Screenplay) -> str:
    """
    导出 Fountain 格式。

    Fountain 是剧本写作的事实标准纯文本语法(Final Draft / Highland 等都支持)。
    本函数按 BRIEF 第6节 Schema 做如下映射：

    - 标题页(Title Page)：用 "Title:" / "Medium:" 等键值对，Fountain 规范的标题页区块。
    - 场景标题(slugline)：格式 "{INT/EXT}. {地点名} - {time_of_day}"，整行大写。
        地点名由 heading.location_id 经 location_name_map 解析得到(找不到用 id)。
        Fountain 里行首是 INT./EXT. 会被识别为 slugline。
    - action 元素：作为普通动作行，整段独占一行，前后用空行隔开(Fountain 默认段落即 action)。
    - dialogue 元素：
        第一行  说话人名(大写)        —— character id 经 character_name_map 解析成 name。
        若有 parenthetical：单独一行，用圆括号包裹(如 "(冷笑)")。
        再下一行 台词文本(line)。
        Fountain 规则：大写人名行 + 紧接非空行 = 对白块。
    - transition 元素：大写 + 结尾冒号(如 "CUT TO:")。Fountain 里以冒号结尾的大写行识别为转场。
    - 场与场之间用空行分隔。
    """
    # 预建两张 id->name 查表(只建一次，循环里反复用)。
    loc_names = _build_location_name_map(sp)
    char_names = _build_character_name_map(sp)

    # 用列表累积每一行/每一块，最后 join。比反复字符串拼接清晰且高效。
    parts = []

    # --- 标题页区块 ---
    # Fountain 标题页是文件最顶部的 "Key: Value" 列表，与正文之间空一行。
    parts.append("Title: " + sp.meta.title)
    # target_medium 是 film/series/short_drama，作为附加元信息写进标题页。
    parts.append("Medium: " + sp.meta.target_medium)
    # 标题页与正文之间必须有一个空行作为分隔。
    parts.append("")

    # --- 逐场渲染 ---
    for scene in sp.scenes:
        heading = scene.heading
        # 解析地点 id -> 地点名(找不到回退为 id)。
        location_name = _resolve(loc_names, heading.location_id)
        # 组装 slugline：INT/EXT. 地点名 - 时间。整体转大写符合剧本惯例。
        slugline = (heading.int_ext + ". " + location_name + " - " + heading.time_of_day).upper()
        parts.append(slugline)
        # slugline 之后空一行再开始正文(Fountain 段落以空行分隔)。
        parts.append("")

        # --- 逐元素渲染 ---
        for el in scene.elements:
            if el.type == "action":
                # 动作行：整段文本独占一段。
                parts.append(el.text)
                # 段后空行，与下一个块隔开。
                parts.append("")
            elif el.type == "dialogue":
                # 说话人名：character id 解析成 name，再整体大写(剧本惯例)。
                speaker = _resolve(char_names, el.character).upper()
                parts.append(speaker)
                # parenthetical 若存在，单独一行并用圆括号包裹。
                if el.parenthetical:
                    # 去掉用户可能自带的括号，统一补一对，避免出现双层括号。
                    inner = el.parenthetical.strip()
                    if inner.startswith("(") and inner.endswith(")"):
                        inner = inner[1:-1].strip()
                    parts.append("(" + inner + ")")
                # 台词正文行(紧跟人名/括注，构成对白块，中间不能有空行)。
                parts.append(el.line)
                # 对白块结束后空一行。
                parts.append("")
            elif el.type == "transition":
                # 转场：大写 + 结尾冒号。先去掉用户可能自带的尾冒号再统一补一个。
                text = el.text.strip().upper()
                if text.endswith(":"):
                    text = text[:-1].rstrip()
                parts.append(text + ":")
                parts.append("")
            else:
                # 防御性分支：未知 type 不应出现(判别联合已限定)，但若出现就跳过而非崩溃。
                continue

        # 场与场之间再补一个空行做强分隔(配合上面段后空行，形成稳定间隔)。
        parts.append("")

    # 用换行连接所有片段；末尾保证一个换行收尾。
    text = "\n".join(parts)
    if not text.endswith("\n"):
        text = text + "\n"
    return text


# ----------------------------------------------------------------------------
# 3. PDF —— 用 fpdf2 渲染，支持中文
# ----------------------------------------------------------------------------

# 系统常见 CJK 字体候选路径(按优先级排列)。
# 探测策略：逐个看文件是否存在，命中第一个就用它，让 PDF 能显示中文。
# wqy-zenhei.ttc(文泉驿正黑)在 Debian/Ubuntu 上最常见，放第一位。
_CJK_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSans.ttc",
]


def _find_cjk_font() -> str:
    """
    探测系统里可用的 CJK 字体文件路径。

    返回第一个真实存在的候选路径；都不存在则返回空字符串，
    调用方据此降级为内置拉丁字体(只保证英文不报错)。
    """
    import os

    for path in _CJK_FONT_CANDIDATES:
        # os.path.isfile 确认是真实文件(不是目录、不是悬空链接)。
        if os.path.isfile(path):
            return path
    return ""


def to_pdf(sp: Screenplay, path: str) -> str:
    """
    把剧本渲染成 PDF 写到 path，返回 path。

    实现策略：
    - 内容直接复用 to_fountain(sp) 的纯文本(Fountain 风格)，逐行写入 PDF。
      PDF 不求精美排版，稳健不崩比好看重要(契约明确)。
    - 中文支持：探测系统 CJK 字体(_find_cjk_font)。
        * 找到 -> add_font 注册该字体，全文用它，中文正常显示。
        * 找不到 -> 降级为 fpdf2 内置 Helvetica(只保证英文/ASCII 不报错)；
          此时若文本含中文，内置字体无对应字形会渲染为缺字方块或被忽略，
          但绝不抛异常(满足"中文字体缺失时也不应抛异常"的验收要求)。

    已知限制(降级路径)：
    - 内置 Helvetica 是 Latin-1 编码字体，无法表示 CJK 字符。
      为避免 fpdf2 在降级路径对非 latin-1 字符抛 UnicodeEncodeError，
      降级时会把每行用 latin-1 做一次 "replace" 容错编码(不可表示字符替换为 '?')。
      正常环境(有 wqy-zenhei.ttc)走的是中文字体路径，不受此限制。
    """
    from fpdf import FPDF

    # 先拿到 Fountain 纯文本，按行切分逐行写。
    content = to_fountain(sp)
    lines = content.split("\n")

    # 探测 CJK 字体。
    cjk_font_path = _find_cjk_font()

    pdf = FPDF()
    # 自动分页：内容超过一页时 fpdf2 自动开新页，避免溢出丢内容。
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # 选定要使用的字体族名。
    if cjk_font_path:
        # 注册 CJK 字体(uni=True 默认开启，支持 Unicode)。族名自定义为 "cjk"。
        # 注意：.ttc 是字体集合，fpdf2 默认取其中第一个 face，足够显示中文。
        try:
            pdf.add_font("cjk", "", cjk_font_path)
            font_family = "cjk"
        except Exception:
            # 极端情况下字体文件存在但 fpdf2 解析失败：兜底降级为内置字体，不让导出崩。
            font_family = "helvetica"
            cjk_font_path = ""
    else:
        # 无 CJK 字体：降级为内置拉丁字体。
        font_family = "helvetica"

    # 设定正文字号。
    pdf.set_font(font_family, size=11)

    # 页面可写宽度 = 总宽 - 左右边距，留给每行 multi_cell 用(自动按宽换行)。
    usable_width = pdf.w - pdf.l_margin - pdf.r_margin

    for line in lines:
        # 空行在 PDF 里渲染成一个空段落(给一点垂直间距)。
        if line == "":
            pdf.ln(5)
            continue

        # 降级路径(无 CJK 字体)：内置 Helvetica 只认 latin-1，
        # 用 latin-1 + replace 容错编码，避免非 ASCII 字符触发 UnicodeEncodeError。
        text_to_write = line
        if not cjk_font_path:
            text_to_write = line.encode("latin-1", "replace").decode("latin-1")

        # multi_cell 会在超出 usable_width 时自动换行，防止长行溢出页面。
        pdf.multi_cell(usable_width, 6, text_to_write)

    # 写出 PDF 到目标路径。fpdf2 的 output(path) 直接落盘。
    pdf.output(path)
    return path
