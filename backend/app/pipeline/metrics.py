# -*- coding: utf-8 -*-
"""
pipeline/metrics.py —— 量化质量看板(metrics)。

这是 BRIEF 第2节"加分项：量化质量看板"的实现。它把一份已生成的剧本
(Screenplay)做纯统计扫描，产出一组可量化指标，用于：
  - 路演时一句话甩数据(schema 合法率/溯源覆盖率/外化数/冲突数)；
  - README / 前端质量面板展示；
  - 朴素基线对比(同一小说不同管线版本，看指标涨没涨)。

设计原则：
  1. 纯统计、不联网、不调用 LLM —— 看板必须秒出、可在测试里确定性断言。
  2. 每个指标都对应一个创新点或卖点，注释里点明"这个数字证明了什么"。
  3. novel(可选)只用来算 source_coverage(溯源覆盖了原文多少字符)，
     不传也能算出其余全部指标，所以做成可选入参。

每个指标对应的卖点：
  - element/dialogue/action/transition_count：剧本规模与结构(完整度)。
  - externalized_count           -> 创新点③ 内心戏外化(有多少行是外化产物)。
  - traceability_coverage        -> 创新点② 行级双向溯源(多少行能点回原文)。
  - continuity_error/warn_count  -> 创新点④ 连贯性检查器(查出多少冲突)。
  - schema_valid                 -> 工程规范自检(产物是否严格往返合法)。
  - source_coverage              -> 创新点② 的另一面(剧本覆盖了原文多大比例)。
"""

from __future__ import annotations

from typing import Optional

# 复用已定稿的数据契约，绝不在这里另起一套类型。
from app.schema.models import Screenplay


def compute_metrics(sp: Screenplay, novel: Optional[object] = None) -> dict:
    """
    对一份 Screenplay 做纯统计，返回量化指标 dict。

    参数：
      sp    : 已生成的剧本对象(顶层数据契约)。
      novel : 可选的 Novel(app.pipeline.types.Novel)。只在需要算
              source_coverage(原文覆盖率)时才用到；不传则跳过该指标。
              这里类型标注写成 object 而非 Novel，是为了避免 metrics 模块
              在 import 期就硬依赖 pipeline.types，保持本模块尽量独立、好测。

    返回的 dict 字段见模块 docstring。
    """
    # ---- 1. 规模类计数(完整度) ----------------------------------------
    # 场景数：直接数 scenes 列表长度。
    scene_count = len(sp.scenes)
    # 人物数 / 地点数：来自单一事实源 story_bible，证明跨章一致性抽取的产出。
    character_count = len(sp.story_bible.characters)
    location_count = len(sp.story_bible.locations)

    # ---- 2. 元素类计数(结构) ------------------------------------------
    # 这些计数器在遍历所有场的所有 element 时累加。
    element_count = 0       # 所有元素总数。
    dialogue_count = 0      # 对白行数。
    action_count = 0        # 动作/描述行数。
    transition_count = 0    # 转场行数。

    # ---- 3. 创新点专项计数 --------------------------------------------
    # externalized_count：带 adaptation 标记的元素数 = 内心戏外化产物数(创新点③)。
    externalized_count = 0
    # 溯源覆盖率的分子/分母(创新点②)：
    #   只统计"语义内容行"(action + dialogue)，transition(转场)是导演记号、
    #   本就没有对应原文，不该拉低溯源覆盖率，所以分母排除 transition。
    traceable_total = 0     # action + dialogue 总数(分母)。
    traceable_hit = 0       # 其中带 source_ref 的数(分子)。

    # 遍历每一场的每一个元素。
    for scene in sp.scenes:
        for el in scene.elements:
            # 每个元素都计入总数。
            element_count += 1

            # el.type 是判别字段，按它分流统计。
            if el.type == "dialogue":
                dialogue_count += 1
                # dialogue 计入溯源分母。
                traceable_total += 1
                # 带 source_ref 才算溯源命中。
                if el.source_ref is not None:
                    traceable_hit += 1
                # 带 adaptation 说明这行是外化产物。
                if el.adaptation is not None:
                    externalized_count += 1
            elif el.type == "action":
                action_count += 1
                traceable_total += 1
                if el.source_ref is not None:
                    traceable_hit += 1
                if el.adaptation is not None:
                    externalized_count += 1
            elif el.type == "transition":
                transition_count += 1
                # transition 既不计溯源、也不计外化(它没有这两个字段)。
            else:
                # 防御：未来若新增 element 类型，至少不静默吞掉。
                # 不抛异常，避免看板因一个新类型整体崩掉。
                pass

    # traceability_coverage：有 source_ref 的内容行 / 内容行总数，范围 [0,1]。
    # 分母为 0(没有任何内容行)时定义为 0.0，避免除零。
    if traceable_total > 0:
        traceability_coverage = traceable_hit / traceable_total
    else:
        traceability_coverage = 0.0

    # ---- 4. 连贯性冲突计数(创新点④) -----------------------------------
    # 扫描每一场的 continuity_flags，按 level 分桶统计。
    continuity_error_count = 0
    continuity_warn_count = 0
    for scene in sp.scenes:
        for flag in scene.continuity_flags:
            if flag.level == "error":
                continuity_error_count += 1
            elif flag.level == "warn":
                continuity_warn_count += 1
            # level == "info" 不计入 error/warn(它只是提示，不算冲突)。

    # ---- 5. schema 自检(工程规范) -------------------------------------
    # 把剧本 to_yaml 再 from_yaml 往返一遍，能成功且结构合法 -> 产物自洽。
    # 这是"我们的产物严格遵守自己定义的 schema"的硬证据。
    schema_valid = _check_round_trip(sp)

    # 组装结果。先放不依赖 novel 的字段。
    metrics = {
        "scene_count": scene_count,
        "character_count": character_count,
        "location_count": location_count,
        "element_count": element_count,
        "dialogue_count": dialogue_count,
        "action_count": action_count,
        "transition_count": transition_count,
        "externalized_count": externalized_count,
        "traceability_coverage": traceability_coverage,
        "continuity_error_count": continuity_error_count,
        "continuity_warn_count": continuity_warn_count,
        "schema_valid": schema_valid,
    }

    # ---- 6. source_coverage(仅 novel 传入时，创新点②的另一面) ----------
    # 含义：所有场的 source_ref.spans 覆盖了原文多少字符 / 原文总字符数。
    # 它回答"这份剧本用到了原著多大比例"，太低说明漏改了大段原文。
    if novel is not None:
        metrics["source_coverage"] = _compute_source_coverage(sp, novel)

    return metrics


def _check_round_trip(sp: Screenplay) -> bool:
    """
    schema 自检：to_yaml -> from_yaml 往返是否成功。

    成功判据：往返不抛异常，且往返后再 to_yaml 的文本与原文本一致。
    第二个判据比"仅不抛异常"更强 —— 它能抓出"能解析但语义漂移"的隐患。
    任何异常都视为不合法(返回 False)，看板不因自检而崩。
    """
    try:
        # 第一次序列化为 YAML 文本。
        yaml_text = sp.to_yaml()
        # 反序列化回对象(会触发 pydantic 全量校验 + 判别联合)。
        sp2 = Screenplay.from_yaml(yaml_text)
        # 再序列化一次，比对两次文本是否完全一致(稳定往返)。
        yaml_text2 = sp2.to_yaml()
        return yaml_text == yaml_text2
    except Exception:
        # 解析失败 / 校验失败 / 任意异常 -> 不合法。
        return False


def _compute_source_coverage(sp: Screenplay, novel: object) -> float:
    """
    计算 source_coverage：场级 spans 覆盖的字符数 / 各章 text 总字符数。

    实现要点：
      1. spans 偏移是"章内偏移"(见 pipeline/types.py 的设计说明)，
         所以要按 chapter 分别累计覆盖区间，不能跨章混算。
      2. 同一章里不同场的 spans 可能重叠，重叠部分只能算一次，
         否则覆盖率会虚高甚至 >1。这里用"区间合并"去重。
      3. 分母是 novel 各章 text 长度之和；为 0 时返回 0.0 防除零。

    返回范围 [0,1]。
    """
    # 建立 章号 -> 该章文本长度 的映射(分母用)。
    chapter_len = {}
    total_chars = 0
    for ch in novel.chapters:
        # ch.index 是 1-based 章号，ch.text 是该章原文。
        chapter_len[ch.index] = len(ch.text)
        total_chars += len(ch.text)

    # 分母为 0(空小说)直接返回 0.0。
    if total_chars == 0:
        return 0.0

    # 按章收集所有 span 区间(start, end)。
    spans_by_chapter = {}
    for scene in sp.scenes:
        ref = scene.source_ref
        for span in ref.spans:
            # 把偏移夹到该章合法范围内，防止越界 span 把覆盖率算虚高。
            ch_len = chapter_len.get(ref.chapter, 0)
            start = max(0, span.start)
            end = min(span.end, ch_len)
            # 非法/空区间(start >= end)跳过。
            if start >= end:
                continue
            spans_by_chapter.setdefault(ref.chapter, []).append((start, end))

    # 对每章的区间做合并去重，累加被覆盖的字符数。
    covered_chars = 0
    for ch_index, intervals in spans_by_chapter.items():
        covered_chars += _merged_length(intervals)

    # 覆盖字符 / 原文总字符。
    return covered_chars / total_chars


def _merged_length(intervals: list) -> int:
    """
    合并一组 [start,end) 区间并返回去重后的总长度。

    经典区间合并：按起点排序，逐个吞并重叠/相邻区间。
    用 if/else 不用三元(遵守代码风格)。
    """
    # 空集合长度为 0。
    if not intervals:
        return 0
    # 按起点升序排序，保证可以一次扫描合并。
    ordered = sorted(intervals, key=lambda iv: iv[0])
    total = 0
    # cur_start/cur_end 维护"当前正在合并的区间"。
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= cur_end:
            # 有重叠或相接：扩展当前区间右端。
            if end > cur_end:
                cur_end = end
            else:
                # 完全被包含，右端不动。
                pass
        else:
            # 出现断档：结算上一个区间长度，开启新区间。
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    # 结算最后一个区间。
    total += cur_end - cur_start
    return total


def format_report(metrics: dict) -> str:
    """
    把指标 dict 排成一段可读中文简报。

    用途：路演口播稿 / README 段落 / 前端质量面板文案。
    设计：
      - 百分比类指标(覆盖率)转成带一位小数的百分号，人读友好。
      - source_coverage 可能不存在(没传 novel)，缺失时不输出该行。
      - 纯文本，无 emoji(遵守代码风格)。
    """
    # 把 [0,1] 的覆盖率转成 "xx.x%" 文本。
    trace_pct = _to_percent(metrics.get("traceability_coverage", 0.0))

    # schema 自检结果转成中文"合法/不合法"。
    if metrics.get("schema_valid", False):
        schema_text = "合法"
    else:
        schema_text = "不合法"

    # 逐行拼装简报。每行点明数字背后的卖点。
    lines = []
    lines.append("Screenwright 质量看板")
    lines.append(
        "规模：{0} 个场景，{1} 个人物，{2} 个地点，共 {3} 个剧本元素。".format(
            metrics.get("scene_count", 0),
            metrics.get("character_count", 0),
            metrics.get("location_count", 0),
            metrics.get("element_count", 0),
        )
    )
    lines.append(
        "结构：对白 {0} 行，动作 {1} 行，转场 {2} 处。".format(
            metrics.get("dialogue_count", 0),
            metrics.get("action_count", 0),
            metrics.get("transition_count", 0),
        )
    )
    lines.append(
        "内心戏外化(创新③)：{0} 行由内心独白/旁白外化而来，已打 adaptation 标记，可审计可回退。".format(
            metrics.get("externalized_count", 0)
        )
    )
    lines.append(
        "行级溯源覆盖率(创新②)：{0}，即这一比例的内容行可点回原文精确高亮。".format(trace_pct)
    )
    lines.append(
        "连贯性检查(创新④)：发现 {0} 处错误、{1} 处警告。".format(
            metrics.get("continuity_error_count", 0),
            metrics.get("continuity_warn_count", 0),
        )
    )
    lines.append("Schema 自检：往返{0}(产物严格遵守自定义契约)。".format(schema_text))

    # source_coverage 仅在算过时输出。
    if "source_coverage" in metrics:
        src_pct = _to_percent(metrics["source_coverage"])
        lines.append("原文覆盖率(创新②)：剧本场级溯源覆盖了原著 {0} 的字符。".format(src_pct))

    # 用换行连成一段。
    return "\n".join(lines)


def _to_percent(value: float) -> str:
    """把 [0,1] 浮点转成保留一位小数的百分号字符串，如 0.756 -> '75.6%'。"""
    # value * 100 后格式化为一位小数，拼上百分号。
    return "{0:.1f}%".format(value * 100)
