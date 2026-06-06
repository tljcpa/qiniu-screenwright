# -*- coding: utf-8 -*-
"""
metrics 模块测试。

构造一份"刻意安排过的" Screenplay，使各项指标的期望值可手算确定，然后断言：
  - 各类计数(scene/character/location/element/dialogue/action/transition)精确;
  - externalized_count(创新③)精确;
  - traceability_coverage(创新②)数值精确(注意分母排除 transition);
  - continuity_error_count / continuity_warn_count(创新④)精确;
  - schema_valid 为 True(往返自检);
  - 传 novel 时 source_coverage 落在 (0,1];
  - format_report 产出非空中文文本，且能反映关键数字。

测试数据布局(便于核对期望值)：
  场 sc_001：
    - action  (带 source_ref)            -> 内容行，溯源命中
    - action  (带 adaptation，无 source_ref) -> 内容行，外化，溯源未命中
    - dialogue(带 source_ref)            -> 内容行，溯源命中
    - dialogue(带 adaptation，无 source_ref) -> 内容行，外化，溯源未命中
    - transition                          -> 非内容行(不入溯源分母)
    continuity_flags: 1 error + 1 warn + 1 info
  场 sc_002：
    - dialogue(带 source_ref)            -> 内容行，溯源命中
    continuity_flags: 1 warn

  合计：
    element_count    = 6
    action_count     = 2
    dialogue_count   = 3
    transition_count = 1
    externalized_count = 2
    内容行(action+dialogue)总数 = 5；其中带 source_ref = 3
    traceability_coverage = 3/5 = 0.6
    continuity_error_count = 1
    continuity_warn_count  = 2  (一个 warn 在 sc_001，一个在 sc_002)
"""

import pytest

from app.schema.models import (
    Adaptation,
    ActionElement,
    Character,
    ContinuityFlag,
    DialogueElement,
    Heading,
    Location,
    Meta,
    Scene,
    Screenplay,
    SourceMeta,
    SourceRef,
    Span,
    StoryBible,
    TimePoint,
    TransitionElement,
)

from app.pipeline.metrics import compute_metrics, format_report
from app.pipeline.types import Chapter, Novel


def _build_screenplay() -> Screenplay:
    """构造上面描述的测试剧本。"""
    # story_bible：2 个人物、1 个地点、1 个时间点。
    bible = StoryBible(
        characters=[
            Character(id="char_lin", name="林"),
            Character(id="char_su", name="苏"),
        ],
        locations=[Location(id="loc_room", name="书房")],
        timeline=[TimePoint(id="t1", label="三年后", order=1)],
    )

    # 场 sc_001 的元素序列(覆盖溯源/外化/转场所有情形)。
    scene1 = Scene(
        id="sc_001",
        heading=Heading(int_ext="INT", location_id="loc_room", time_of_day="夜"),
        source_ref=SourceRef(chapter=1, spans=[Span(start=0, end=20)]),
        characters=["char_lin", "char_su"],
        synopsis="林回到书房。",
        elements=[
            # 动作行，带 source_ref -> 溯源命中。
            ActionElement(
                type="action",
                text="林推开门。",
                source_ref=SourceRef(chapter=1, spans=[Span(start=0, end=5)]),
            ),
            # 动作行，带 adaptation(外化)，无 source_ref -> 外化 + 溯源未命中。
            ActionElement(
                type="action",
                text="他眼神闪过一丝痛苦。",
                adaptation=Adaptation(from_="interior_monologue", technique="action"),
            ),
            # 对白，带 source_ref -> 溯源命中。
            DialogueElement(
                type="dialogue",
                character="char_lin",
                line="你还在等我？",
                source_ref=SourceRef(chapter=1, spans=[Span(start=6, end=12)]),
            ),
            # 对白，带 adaptation(外化为画外音)，无 source_ref -> 外化 + 溯源未命中。
            DialogueElement(
                type="dialogue",
                character="char_su",
                line="(画外)我从未离开。",
                adaptation=Adaptation(from_="narration", technique="voiceover"),
            ),
            # 转场，不入溯源分母、不计外化。
            TransitionElement(type="transition", text="CUT TO"),
        ],
        continuity_flags=[
            ContinuityFlag(level="error", msg="时间线冲突", scene_ids=["sc_001"]),
            ContinuityFlag(level="warn", msg="人物认知不一致", scene_ids=["sc_001"]),
            ContinuityFlag(level="info", msg="仅供参考", scene_ids=["sc_001"]),
        ],
    )

    # 场 sc_002：单对白行(带 source_ref)，外加一个 warn flag。
    scene2 = Scene(
        id="sc_002",
        heading=Heading(int_ext="EXT", location_id="loc_room", time_of_day="日"),
        source_ref=SourceRef(chapter=1, spans=[Span(start=12, end=30)]),
        characters=["char_su"],
        synopsis="苏独白。",
        elements=[
            DialogueElement(
                type="dialogue",
                character="char_su",
                line="终于回来了。",
                source_ref=SourceRef(chapter=1, spans=[Span(start=12, end=18)]),
            ),
        ],
        continuity_flags=[
            ContinuityFlag(level="warn", msg="天气连续性", scene_ids=["sc_002"]),
        ],
    )

    return Screenplay(
        meta=Meta(title="测试剧本", source=SourceMeta(type="novel", chapters=[1])),
        story_bible=bible,
        scenes=[scene1, scene2],
    )


def test_counts_and_traceability():
    """各类计数与溯源覆盖率精确。"""
    sp = _build_screenplay()
    m = compute_metrics(sp)

    # 规模类。
    assert m["scene_count"] == 2
    assert m["character_count"] == 2
    assert m["location_count"] == 1

    # 元素类。
    assert m["element_count"] == 6
    assert m["action_count"] == 2
    assert m["dialogue_count"] == 3
    assert m["transition_count"] == 1

    # 创新③：两个带 adaptation 的元素。
    assert m["externalized_count"] == 2

    # 创新②：内容行 5 个，命中 3 个 -> 0.6。
    assert m["traceability_coverage"] == pytest.approx(0.6)


def test_continuity_counts():
    """连贯性 error/warn 计数精确，info 不计入。"""
    sp = _build_screenplay()
    m = compute_metrics(sp)
    # sc_001 有 1 error；sc_001 + sc_002 各 1 warn = 2；info 不计。
    assert m["continuity_error_count"] == 1
    assert m["continuity_warn_count"] == 2


def test_schema_valid_true():
    """合法剧本往返自检为 True。"""
    sp = _build_screenplay()
    m = compute_metrics(sp)
    assert m["schema_valid"] is True


def test_no_novel_skips_source_coverage():
    """不传 novel 时不应有 source_coverage 字段。"""
    sp = _build_screenplay()
    m = compute_metrics(sp)
    assert "source_coverage" not in m


def test_source_coverage_in_range():
    """传 novel 时 source_coverage 落在 (0,1]。"""
    sp = _build_screenplay()
    # 构造一章原文，长度足够覆盖上面用到的 span 偏移(0..30)。
    chapter_text = "字" * 40  # 40 字，spans 最大 end=30 在范围内。
    novel = Novel(
        title="测试小说",
        raw=chapter_text,
        chapters=[Chapter(index=1, title="第一章", text=chapter_text)],
    )
    m = compute_metrics(sp, novel=novel)

    assert "source_coverage" in m
    cov = m["source_coverage"]
    # 必须严格大于 0(有 span 覆盖)且不超过 1(合并去重后不虚高)。
    assert cov > 0.0
    assert cov <= 1.0

    # 进一步核对精确值：两场 spans 在章内偏移合并去重后覆盖 [0,30)=30 字，
    # 分母 40 字 -> 0.75。(sc_001 场级 [0,20) 与 sc_002 场级 [12,30) 合并为 [0,30)。)
    assert cov == pytest.approx(30 / 40)


def test_source_coverage_empty_novel():
    """空小说(总字符 0)时 source_coverage 为 0.0，不除零崩溃。"""
    sp = _build_screenplay()
    novel = Novel(
        title="空",
        raw="",
        chapters=[Chapter(index=1, title="第一章", text="")],
    )
    m = compute_metrics(sp, novel=novel)
    assert m["source_coverage"] == 0.0


def test_format_report_text():
    """简报为非空中文文本且包含关键数字。"""
    sp = _build_screenplay()
    m = compute_metrics(sp)
    report = format_report(m)

    # 非空字符串。
    assert isinstance(report, str)
    assert len(report) > 0
    # 含看板标题与若干关键数字/卖点。
    assert "质量看板" in report
    assert "60.0%" in report          # 溯源覆盖率 0.6 -> 60.0%
    assert "合法" in report           # schema 自检
    assert "外化" in report           # 创新③ 文案


def test_format_report_language_en():
    """language='en' 时输出英文简报，关键数字仍正确，且不含中文模板词。"""
    sp = _build_screenplay()
    m = compute_metrics(sp)
    report = format_report(m, language="en")

    assert isinstance(report, str)
    assert len(report) > 0
    # 英文模板标志词。
    assert "Screenwright Quality Dashboard" in report
    assert "60.0%" in report           # 溯源覆盖率 0.6 -> 60.0%
    assert "valid" in report           # schema 自检英文文案
    assert "externaliz" in report      # 创新③ 英文文案(externalization)
    # 反证：不应混入中文模板。
    assert "质量看板" not in report
    assert "合法" not in report


def test_format_report_default_is_chinese():
    """不传 language 时默认仍是中文(向后兼容，既有调用不受影响)。"""
    sp = _build_screenplay()
    m = compute_metrics(sp)
    # 默认调用与显式 zh 调用应完全一致。
    assert format_report(m) == format_report(m, language="zh")
    assert "质量看板" in format_report(m)


def test_format_report_with_source_coverage():
    """带 source_coverage 时简报应输出原文覆盖率行。"""
    sp = _build_screenplay()
    chapter_text = "字" * 40
    novel = Novel(
        title="测试小说",
        raw=chapter_text,
        chapters=[Chapter(index=1, title="第一章", text=chapter_text)],
    )
    m = compute_metrics(sp, novel=novel)
    report = format_report(m)
    assert "原文覆盖率" in report
    assert "75.0%" in report          # 30/40 -> 75.0%
