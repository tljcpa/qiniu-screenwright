# -*- coding: utf-8 -*-
"""
连贯性检查器测试。

覆盖：
1. 脏剧本(时间线倒错 + 未知人物 + 未知地点 + 未知对白说话人 + 重复 id + 空场景)：
   断言对应 flag 被检出且 level 正确。
2. 干净剧本：断言没有任何 error。
3. annotate：断言 flags 按 scene_ids 正确回填，且不污染入参、幂等。
"""

from app.pipeline.continuity import annotate, check_continuity
from app.schema.models import (
    ActionElement,
    Character,
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
)


# ----------------------------------------------------------------------------
# 小工具：快速造一个最小合法 SourceRef / Heading / Scene。
# ----------------------------------------------------------------------------

def _ref():
    """造一个最小 SourceRef(场级溯源是必填字段)。"""
    return SourceRef(chapter=1, spans=[Span(start=0, end=1)])


def _heading(location_id, time_ref=None):
    """造一个 Heading，允许指定地点 id 与时间引用。"""
    return Heading(
        int_ext="INT",
        location_id=location_id,
        time_of_day="日",
        time_ref=time_ref,
    )


def _action(text="走进房间"):
    """造一个最小 action element。"""
    return ActionElement(type="action", text=text)


def _clean_bible():
    """造一个含两个人物、一个地点、两个时间点的干净 bible。"""
    return StoryBible(
        characters=[
            Character(id="char_lin", name="林"),
            Character(id="char_wang", name="王"),
        ],
        locations=[Location(id="loc_room", name="房间")],
        timeline=[
            TimePoint(id="t1", label="第一天", order=1),
            TimePoint(id="t2", label="第二天", order=2),
        ],
    )


def _meta():
    """造一个最小 Meta。"""
    return Meta(title="测试", source=SourceMeta(type="novel", chapters=[1]))


# ----------------------------------------------------------------------------
# 测试 1：脏剧本，全检查项命中。
# ----------------------------------------------------------------------------

def test_dirty_screenplay_flags():
    """构造含时间线倒错 + 未知引用 + 重复 id + 空场景的剧本，逐项断言。"""
    bible = _clean_bible()

    # sc_001：order=2(第二天)，人物 ok。
    sc1 = Scene(
        id="sc_001",
        heading=_heading("loc_room", time_ref="t2"),
        source_ref=_ref(),
        characters=["char_lin"],
        elements=[_action()],
    )
    # sc_002：order=1(第一天)，早于前一场 sc_001 -> 时间线倒错 warn。
    #          同时引用未知人物 char_ghost -> error；
    #          对白说话人 char_ghost 也未知 -> error。
    sc2 = Scene(
        id="sc_002",
        heading=_heading("loc_room", time_ref="t1"),
        source_ref=_ref(),
        characters=["char_ghost"],
        elements=[
            DialogueElement(type="dialogue", character="char_ghost", line="你好"),
        ],
    )
    # sc_003：未知地点 loc_unknown -> error；空 elements -> info。
    sc3 = Scene(
        id="sc_003",
        heading=_heading("loc_unknown"),
        source_ref=_ref(),
        characters=["char_wang"],
        elements=[],
    )
    # 重复 id：再来一个 id 同为 sc_001 -> error。
    sc_dup = Scene(
        id="sc_001",
        heading=_heading("loc_room"),
        source_ref=_ref(),
        characters=["char_wang"],
        elements=[_action("重复场景")],
    )

    sp = Screenplay(
        meta=_meta(),
        story_bible=bible,
        scenes=[sc1, sc2, sc3, sc_dup],
    )

    flags = check_continuity(sp)

    # --- 重复 id：error，scene_ids 含 sc_001 ---
    dup = [f for f in flags if f.level == "error" and "重复" in f.msg]
    assert len(dup) == 1
    assert dup[0].scene_ids == ["sc_001"]

    # --- 未知人物(场景 characters)：error，命中 char_ghost ---
    unknown_char = [
        f for f in flags
        if f.level == "error" and "未知人物" in f.msg and "char_ghost" in f.msg
        and "对白" not in f.msg
    ]
    assert len(unknown_char) == 1
    assert "sc_002" in unknown_char[0].scene_ids

    # --- 未知对白说话人：error ---
    unknown_speaker = [
        f for f in flags
        if f.level == "error" and "对白说话人" in f.msg
    ]
    assert len(unknown_speaker) == 1
    assert "sc_002" in unknown_speaker[0].scene_ids

    # --- 未知地点：error ---
    unknown_loc = [
        f for f in flags
        if f.level == "error" and "未知地点" in f.msg and "loc_unknown" in f.msg
    ]
    assert len(unknown_loc) == 1
    assert "sc_003" in unknown_loc[0].scene_ids

    # --- 时间线倒错：warn，两场都列出 ---
    timeline = [f for f in flags if f.level == "warn" and "时间线倒错" in f.msg]
    assert len(timeline) == 1
    assert "sc_001" in timeline[0].scene_ids
    assert "sc_002" in timeline[0].scene_ids

    # --- 空场景：info ---
    empty = [f for f in flags if f.level == "info" and "空场景" in f.msg]
    assert len(empty) == 1
    assert "sc_003" in empty[0].scene_ids


# ----------------------------------------------------------------------------
# 测试 2：干净剧本，无 error。
# ----------------------------------------------------------------------------

def test_clean_screenplay_no_error():
    """构造完全合法、时间正序、引用齐全的剧本，断言没有 error 级 flag。"""
    bible = _clean_bible()

    sc1 = Scene(
        id="sc_001",
        heading=_heading("loc_room", time_ref="t1"),
        source_ref=_ref(),
        characters=["char_lin"],
        elements=[
            _action(),
            DialogueElement(type="dialogue", character="char_lin", line="到了"),
        ],
    )
    # 时间正序：t2 在 t1 之后。
    sc2 = Scene(
        id="sc_002",
        heading=_heading("loc_room", time_ref="t2"),
        source_ref=_ref(),
        characters=["char_wang"],
        elements=[_action("第二天")],
    )

    sp = Screenplay(meta=_meta(), story_bible=bible, scenes=[sc1, sc2])

    flags = check_continuity(sp)

    # 干净剧本不应有任何 error。
    errors = [f for f in flags if f.level == "error"]
    assert errors == []
    # 也不应有时间线倒错 warn。
    timeline = [f for f in flags if f.level == "warn" and "时间线倒错" in f.msg]
    assert timeline == []


# ----------------------------------------------------------------------------
# 测试 3：annotate 回填正确、不污染入参、幂等。
# ----------------------------------------------------------------------------

def test_annotate_backfills_flags():
    """annotate 把 flag 按 scene_ids 回填到对应场，且不改入参，重复调用幂等。"""
    bible = _clean_bible()

    # sc_002 引用未知人物 -> 会产生一条挂到 sc_002 的 error。
    sc1 = Scene(
        id="sc_001",
        heading=_heading("loc_room", time_ref="t1"),
        source_ref=_ref(),
        characters=["char_lin"],
        elements=[_action()],
    )
    sc2 = Scene(
        id="sc_002",
        heading=_heading("loc_room", time_ref="t2"),
        source_ref=_ref(),
        characters=["char_ghost"],
        elements=[_action()],
    )

    sp = Screenplay(meta=_meta(), story_bible=bible, scenes=[sc1, sc2])

    annotated = annotate(sp)

    # 入参不被污染：原对象各场仍无 flag。
    assert sp.scenes[0].continuity_flags == []
    assert sp.scenes[1].continuity_flags == []

    # 回填结果：sc_001 无 flag，sc_002 有一条未知人物 error。
    a_sc1 = [s for s in annotated.scenes if s.id == "sc_001"][0]
    a_sc2 = [s for s in annotated.scenes if s.id == "sc_002"][0]
    assert a_sc1.continuity_flags == []
    assert len(a_sc2.continuity_flags) == 1
    assert a_sc2.continuity_flags[0].level == "error"
    assert "char_ghost" in a_sc2.continuity_flags[0].msg

    # 幂等：对 annotated 再 annotate 一次，结果一致(不累积)。
    again = annotate(annotated)
    again_sc2 = [s for s in again.scenes if s.id == "sc_002"][0]
    assert len(again_sc2.continuity_flags) == 1
