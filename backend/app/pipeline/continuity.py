# -*- coding: utf-8 -*-
"""
连贯性检查器(创新点④) —— 纯规则、确定性、不联网、不调 LLM。

设计立场：
- 这是 Pass4 validate 的一部分，但它独立成模块，只做"基于 schema 已有结构的静态规则检查"。
- 不依赖任何外部状态：同一个 Screenplay 进去，永远得到同一组 ContinuityFlag(确定性)。
- 不修改输入(check_continuity 只读)；annotate 才负责把结果回填到场景上。

检查项(对应 BRIEF 第6节验收要求)：
1. 时间线倒错：场景按出现顺序，若某场 heading.time_ref 对应的 timeline.order
   比"前一个有 time_ref 的场"更早，且无合理理由 -> level="warn"。
2. 未知引用：
   - 场景 characters 里的 id 不在 bible.characters     -> "error"
   - heading.location_id 不在 bible.locations          -> "error"
   - DialogueElement.character 不在 bible.characters    -> "error"
3. 重复场景 id                                          -> "error"
4. 空场景(elements 为空)                                -> "info"
"""

from __future__ import annotations

from typing import Dict, List

from app.schema.models import ContinuityFlag, Scene, Screenplay


# ----------------------------------------------------------------------------
# 主入口：check_continuity
# ----------------------------------------------------------------------------

def check_continuity(sp: Screenplay) -> List[ContinuityFlag]:
    """
    对整部剧本做连贯性检查，返回一组 ContinuityFlag(只读，不改 sp)。

    顺序固定，便于测试做确定性断言：
      重复 id -> 未知引用 -> 时间线倒错 -> 空场景。
    """
    # 收集所有 flag 的容器。
    flags: List[ContinuityFlag] = []

    # 预先把 bible 的三类 id 拍平成集合(O(1) 查找)，避免每场重复遍历。
    bible = sp.story_bible
    # 人物 id 集合。
    char_ids = set()
    # 遍历每个人物，把其稳定 id 放入集合。
    for ch in bible.characters:
        char_ids.add(ch.id)
    # 地点 id 集合。
    loc_ids = set()
    for loc in bible.locations:
        loc_ids.add(loc.id)
    # 时间线 id -> order 的映射(时间线倒错检查要用 order 比大小)。
    time_order: Dict[str, int] = {}
    for tp in bible.timeline:
        time_order[tp.id] = tp.order

    # ---- 检查项 3：重复场景 id ----
    flags.extend(_check_duplicate_ids(sp.scenes))

    # ---- 检查项 2：未知引用(人物/地点/对白说话人) ----
    flags.extend(_check_unknown_refs(sp.scenes, char_ids, loc_ids))

    # ---- 检查项 1：时间线倒错 ----
    flags.extend(_check_timeline(sp.scenes, time_order))

    # ---- 检查项 4：空场景 ----
    flags.extend(_check_empty_scenes(sp.scenes))

    return flags


# ----------------------------------------------------------------------------
# 检查项 3：重复场景 id
# ----------------------------------------------------------------------------

def _check_duplicate_ids(scenes: List[Scene]) -> List[ContinuityFlag]:
    """场景 id 必须唯一；出现重复即 error。"""
    flags: List[ContinuityFlag] = []
    # 统计每个 id 出现的次数。
    seen: Dict[str, int] = {}
    for sc in scenes:
        if sc.id in seen:
            seen[sc.id] = seen[sc.id] + 1
        else:
            seen[sc.id] = 1
    # 对出现次数 >1 的 id 各产出一条 error。
    for scene_id in seen:
        if seen[scene_id] > 1:
            flags.append(
                ContinuityFlag(
                    level="error",
                    msg="场景 id 重复: " + scene_id + " 出现 " + str(seen[scene_id]) + " 次",
                    scene_ids=[scene_id],
                )
            )
    return flags


# ----------------------------------------------------------------------------
# 检查项 2：未知引用
# ----------------------------------------------------------------------------

def _check_unknown_refs(
    scenes: List[Scene],
    char_ids: set,
    loc_ids: set,
) -> List[ContinuityFlag]:
    """
    三种未知引用都判 error：
    - 场景出场人物 id 不在 bible。
    - 场景 heading.location_id 不在 bible。
    - 对白说话人 character 不在 bible。
    """
    flags: List[ContinuityFlag] = []
    for sc in scenes:
        # 场景出场人物 id 校验。
        for cid in sc.characters:
            if cid not in char_ids:
                flags.append(
                    ContinuityFlag(
                        level="error",
                        msg="场景 " + sc.id + " 引用了未知人物 id: " + cid,
                        scene_ids=[sc.id],
                    )
                )
        # 地点 id 校验。
        if sc.heading.location_id not in loc_ids:
            flags.append(
                ContinuityFlag(
                    level="error",
                    msg="场景 " + sc.id + " 引用了未知地点 id: " + sc.heading.location_id,
                    scene_ids=[sc.id],
                )
            )
        # 对白说话人校验：只有 DialogueElement 才有 character 字段。
        for el in sc.elements:
            if getattr(el, "type", None) == "dialogue":
                speaker = el.character
                if speaker not in char_ids:
                    flags.append(
                        ContinuityFlag(
                            level="error",
                            msg="场景 " + sc.id + " 的对白说话人是未知人物 id: " + speaker,
                            scene_ids=[sc.id],
                        )
                    )
    return flags


# ----------------------------------------------------------------------------
# 检查项 1：时间线倒错
# ----------------------------------------------------------------------------

def _check_timeline(
    scenes: List[Scene],
    time_order: Dict[str, int],
) -> List[ContinuityFlag]:
    """
    按场景出现顺序扫描。维护"上一个有有效 time_ref 的场"的 order 与 id。
    若当前场 order 严格小于上一场 order，则判 warn(时间线倒错)。

    "合理理由"的处理：
    - 没有 time_ref 的场直接跳过(无从判断时间)。
    - time_ref 指向 bible 里不存在的 TimePoint：这属于"未知时间引用"，
      不在本函数判(避免和未知引用检查重叠/误报)，这里只跳过。
    - order 相等不算倒错(同一时间点，合理)。
    """
    flags: List[ContinuityFlag] = []
    # 上一场的 order(用 None 表示"还没遇到有效 time_ref 的场")。
    prev_order = None
    # 上一场的 id，用于在 flag 里把两场都列出。
    prev_scene_id = None

    for sc in scenes:
        ref = sc.heading.time_ref
        # 没有 time_ref：跳过，不更新基准。
        if ref is None:
            continue
        # time_ref 指向未知 TimePoint：跳过，不更新基准。
        if ref not in time_order:
            continue
        # 当前场的时间序号。
        cur_order = time_order[ref]
        # 只有已经有一个基准场时，才能比较倒错。
        if prev_order is not None:
            if cur_order < prev_order:
                flags.append(
                    ContinuityFlag(
                        level="warn",
                        msg=(
                            "时间线倒错: 场景 " + sc.id
                            + "(order=" + str(cur_order) + ") 早于前一场 "
                            + prev_scene_id + "(order=" + str(prev_order) + ")"
                        ),
                        scene_ids=[prev_scene_id, sc.id],
                    )
                )
        # 更新基准为当前场(无论是否倒错，按出现顺序推进)。
        prev_order = cur_order
        prev_scene_id = sc.id

    return flags


# ----------------------------------------------------------------------------
# 检查项 4：空场景
# ----------------------------------------------------------------------------

def _check_empty_scenes(scenes: List[Scene]) -> List[ContinuityFlag]:
    """elements 为空的场景给 info 级提示(不算错误，但值得注意)。"""
    flags: List[ContinuityFlag] = []
    for sc in scenes:
        if len(sc.elements) == 0:
            flags.append(
                ContinuityFlag(
                    level="info",
                    msg="场景 " + sc.id + " 没有任何 element(空场景)",
                    scene_ids=[sc.id],
                )
            )
    return flags


# ----------------------------------------------------------------------------
# annotate：把 flags 回填到对应 Scene.continuity_flags
# ----------------------------------------------------------------------------

def annotate(sp: Screenplay) -> Screenplay:
    """
    跑一遍检查，并把每条 flag 按其 scene_ids 回填到对应 Scene.continuity_flags。

    返回一个"新对象"(深拷贝后填充)，不就地修改入参，保证调用方拿到的原对象不变。
    注意：
    - 一条 flag 可能涉及多个 scene_ids(如时间线倒错涉及两场)，则两场都追加该 flag。
    - 重复 id 场景：scene_ids 里的 id 在剧本中对应多场，按 id 匹配会都填上(可接受，
      因为重复 id 本身就是 error，回填到所有同 id 场不丢信息)。
    - 回填前先清空各场原有 continuity_flags，保证 annotate 幂等(重复调用结果一致)。
    """
    # model_copy(deep=True) 得到独立深拷贝，避免污染入参。
    new_sp = sp.model_copy(deep=True)

    # 基于"新对象"重新跑检查(等价于基于入参，因为是深拷贝且检查只读)。
    flags = check_continuity(new_sp)

    # 先清空每场已有 flags，保证幂等。
    for sc in new_sp.scenes:
        sc.continuity_flags = []

    # 把每条 flag 追加到它涉及的每个场景上(按 id 匹配)。
    for flag in flags:
        for sc in new_sp.scenes:
            if sc.id in flag.scene_ids:
                sc.continuity_flags.append(flag)

    return new_sp
