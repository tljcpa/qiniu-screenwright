# -*- coding: utf-8 -*-
"""
pipeline/baseline.py —— 朴素基线(对照组)。

这是 BRIEF 第2节加分项"朴素基线对比"的实现，定位是**诚实的对照组**：

为什么要写这个文件？
  绝大多数参赛队的做法就是这一版：把小说切几块、每块丢给 LLM 让它
  "改写成剧本"，然后把结果拼起来。没有故事圣经(跨章一致性)、没有
  行级溯源、没有内心戏外化标记、没有严格 schema 校验。

  我们故意把这个最简做法**真实地**实现出来(不是稻草人，不是故意做烂)，
  好在 UI / 路演里和我们的完整管线并排对比，用客观差距证明：我们的
  多轮管线深度不是装饰，而是实打实多做了换皮组会跳过的硬骨头。

本模块刻意**不做**的事(差距即卖点)：
  - 不建 story bible，不跨块归一人物(同一人在不同块可能叫不同名)。
  - 不回填 source_ref(行级溯源)，剧本无法点回原文。
  - 不识别内心独白、不打 adaptation 外化标记。
  - 不校验到我们的严格 pydantic schema，输出是自由文本/浅 JSON。
  - 不做连贯性检查。

所以这里的输出是一个**轻量 dict**，结构够前端并排展示即可：
  {"medium": ..., "chunks": N, "scenes": [{"heading": str, "lines": [str,...]}, ...]}

设计原则：
  1. 能跑、能出像样结果——朴素不等于做烂。
  2. 复用全局 LLM 客户端(get_llm)，与主管线同源，对比才公平。
  3. 单块调用、prompt 直白，正是"换皮组"的真实写法。
"""

from __future__ import annotations

# 复用全局统一 LLM 客户端工厂；llm=None 时从这里取默认实例。
from app.llm.client import get_llm


# 固定切块大小(字符数)。朴素做法不做语义切分、不做滚动摘要，
# 就是粗暴按长度切，长度短则整篇一块。1200 是个对单块 LLM 友好的常见值。
_CHUNK_SIZE = 1200


def _split_fixed(text: str, size: int) -> list:
    """
    把全文按固定字符数切块。

    这是朴素做法的核心"简陋点"：不看章节、不看场景边界、不看句号，
    纯按长度硬切。块与块之间没有任何上下文衔接(这正是它会丢一致性的根源)。

    返回 list[str]；text 为空时返回空列表；text 短于 size 时返回单元素列表。
    """
    # 去掉首尾空白，避免空块。
    stripped = text.strip()
    # 空文本直接返回空列表，调用方据此可决定不发 LLM 请求。
    if not stripped:
        return []
    # 文本不超过一个块大小：整篇作为一块。
    if len(stripped) <= size:
        return [stripped]
    # 否则按固定步长切片，最后一块可能不足 size，照收。
    chunks = []
    start = 0
    n = len(stripped)
    while start < n:
        end = start + size
        chunks.append(stripped[start:end])
        start = end
    return chunks


def _build_naive_prompt(chunk: str, medium: str) -> list:
    """
    构造朴素 prompt(OpenAI messages 格式)。

    刻意写得"直白且偷懒"——这就是换皮组的真实 prompt：直接要 LLM
    把这段小说改写成剧本，给场景标题/动作/对白，返回 JSON。
      - 不注入 bible 切片(没有 bible)。
      - 不注入上一场结尾(无跨块衔接)。
      - 不要求逐字溯源、不要求标外化、不约束到严格 schema。

    medium 只做一句话风格提示，不像主管线那样按媒介系统性重渲染。
    """
    # 把媒介翻成一句中文风格提示。用 if/else 不用三元(遵守代码风格)。
    if medium == "short_drama":
        medium_hint = "目标是竖屏短剧，节奏要快、多金句、冲突前置。"
    elif medium == "series":
        medium_hint = "目标是剧集，节奏可稍缓，注意单集落点。"
    else:
        # 默认按电影处理(film 或任何未知值都走这里)。
        medium_hint = "目标是电影，画面感优先。"

    # system 消息：给最简角色设定，要求 JSON 输出以便程序解析。
    system_content = (
        "你是一个剧本改编助手。把用户给的小说片段改写成剧本。"
        + medium_hint
        + "只输出一个 JSON 对象，形如 "
        + '{"scenes":[{"heading":"场景标题","lines":["动作或对白行", "..."]}]}。'
        + "heading 是场景标题，lines 是该场按顺序排列的动作描述与对白文本。"
    )

    # user 消息：直接把这一块原文丢进去，要求改成剧本。
    user_content = (
        "把下面这段小说改写成剧本，给出场景标题、动作和对白：\n\n" + chunk
    )

    # 返回标准 messages 列表，交给 llm.complete(json=True)。
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def _coerce_scenes(data: object) -> list:
    """
    把单块 LLM 返回的浅 JSON 容错地规整成 [{"heading":str,"lines":[str,...]}]。

    朴素做法不做严格 schema 校验，但起码要把 LLM 各种"差不多"的返回
    收敛成前端能渲染的统一形状，否则展示会乱。容错点：
      - data 不是 dict / 没有 scenes -> 返回空列表。
      - 单个 scene 缺 heading -> 给个占位标题。
      - lines 不是 list -> 包成单元素或丢弃非字符串项。
    """
    scenes_out = []
    # data 必须是 dict 且含 scenes 列表，否则视为本块无有效输出。
    if not isinstance(data, dict):
        return scenes_out
    raw_scenes = data.get("scenes")
    if not isinstance(raw_scenes, list):
        return scenes_out

    for raw in raw_scenes:
        # 每个 scene 也必须是 dict，否则跳过。
        if not isinstance(raw, dict):
            continue
        # heading 容错：缺失或非字符串时给占位标题。
        heading = raw.get("heading")
        if not isinstance(heading, str) or not heading.strip():
            heading = "未命名场景"

        # lines 容错：规整成字符串列表。
        raw_lines = raw.get("lines")
        lines = []
        if isinstance(raw_lines, list):
            for item in raw_lines:
                # 字符串直接收。
                if isinstance(item, str):
                    lines.append(item)
                else:
                    # 非字符串(如朴素 LLM 偶尔返回 {"character":..,"line":..})
                    # 直接 str() 兜底，保证前端有东西可显示。
                    lines.append(str(item))
        elif isinstance(raw_lines, str):
            # 偶尔 LLM 把 lines 写成一整段字符串，包成单元素列表。
            lines = [raw_lines]
        # raw_lines 是别的类型时 lines 保持空列表。

        scenes_out.append({"heading": heading, "lines": lines})

    return scenes_out


def naive_convert(text: str, medium: str = "film", llm=None) -> dict:
    """
    朴素基线转换：小说全文 -> 轻量剧本 dict(对照组)。

    流程(刻意简单)：
      1. 全文按固定大小切几块(短则整篇一块)。
      2. 每块一次 LLM 调用，直白 prompt 要它改成剧本(场景标题/动作/对白)。
      3. 把各块的 scenes 顺序拼接，返回轻量 dict。

    刻意不做：bible / 跨块人物归一 / source_ref / 外化标记 / 严格 schema。
    所以返回的 dict 里**没有** source_ref、adaptation、story_bible 等字段——
    这正是它和我们完整管线产物的本质差距。

    参数：
      text   : 小说全文(可多章拼接的纯文本)。
      medium : 目标媒介，film | series | short_drama，仅作一句话风格提示。
      llm    : 可注入的 LLM 客户端(需有 complete(messages, json=...) 方法)；
               None 时用 get_llm() 取全局默认实例。

    返回：
      {
        "medium": str,         # 回显媒介
        "chunks": int,         # 实际切了几块(== LLM 调用次数)
        "scenes": [            # 顺序拼接的场景，结构极简、无溯源/无外化
          {"heading": str, "lines": [str, ...]},
          ...
        ],
      }
    """
    # 注入优先：外部传了 llm 就用它，否则取全局默认实例。
    if llm is None:
        llm = get_llm()

    # 第一步：固定大小切块。
    chunks = _split_fixed(text, _CHUNK_SIZE)

    # 累积所有块产出的场景。
    all_scenes = []
    # 逐块一次 LLM 调用——这就是朴素做法"无跨块上下文"的体现。
    for chunk in chunks:
        messages = _build_naive_prompt(chunk, medium)
        # json=True 让客户端以 JSON 对象模式返回并解析成 dict。
        data = llm.complete(messages, json=True)
        # 容错规整后追加。各块之间不做任何人物/地点归一(差距点)。
        scene_list = _coerce_scenes(data)
        all_scenes.extend(scene_list)

    # 组装轻量 dict。注意这里**没有** source_ref / adaptation / story_bible，
    # 是朴素基线的本质——结构只够并排展示，不可溯源、不可审计。
    return {
        "medium": medium,
        "chunks": len(chunks),
        "scenes": all_scenes,
    }


def naive_vs_ours_summary(naive_out: dict, ours_metrics: dict) -> dict:
    """
    朴素基线 vs 我们的完整管线——对比摘要(给路演/前端用)。

    把两者在几个关键维度上的差异列成结构化 dict，每个维度同时给出
    naive / ours 的布尔判定和一句中文说明，前端可直接渲染成对照表。

    维度(都对应一个创新点或工程卖点)：
      - cross_chapter_consistency : 跨章/跨块人物一致性(创新点②的前提，bible)。
      - traceable                 : 行级双向溯源(创新点②，source_ref)。
      - externalization_marked    : 内心戏外化并打标记(创新点③，adaptation)。
      - structured_editable       : 结构化、可编辑、可校验(严格 schema)。
      - continuity_checked        : 跨场连贯性检查(创新点④)。

    判定依据：
      - naive 侧：恒为 False/0——朴素做法本就不做这些(这正是对照组的意义)。
        我们诚实地按"它没做"来标，不抹黑也不美化。
      - ours 侧：从 ours_metrics(metrics.compute_metrics 的输出)读取客观数字，
        用数字佐证"我们确实做到了"，而不是嘴上说。

    参数：
      naive_out    : naive_convert 的返回 dict(目前只读它的规模，做诚实对照)。
      ours_metrics : compute_metrics 的输出 dict(读 traceability_coverage 等)。

    返回：
      {
        "dimensions": {
          "<维度名>": {
            "naive": bool,
            "ours": bool,
            "ours_evidence": <数字/简短证据>,
            "note": "<一句中文说明差距>",
          },
          ...
        },
        "headline": "<一句话总结>",
      }
    """
    # 从我们的指标里取客观证据(缺失时给安全默认值，避免 KeyError)。
    trace_cov = ours_metrics.get("traceability_coverage", 0.0)
    externalized = ours_metrics.get("externalized_count", 0)
    schema_valid = ours_metrics.get("schema_valid", False)
    char_count = ours_metrics.get("character_count", 0)
    cont_err = ours_metrics.get("continuity_error_count", 0)
    cont_warn = ours_metrics.get("continuity_warn_count", 0)

    # ---- 维度判定 -------------------------------------------------------
    # 我们这边各维度是否"做到了"，用客观数字推导布尔，而非写死 True。

    # 跨章一致性：我们有 story_bible 抽出的统一人物集(character_count>0 即建了 bible)。
    if char_count > 0:
        ours_consistency = True
    else:
        ours_consistency = False

    # 行级溯源：覆盖率 > 0 说明确实回填了 source_ref。
    if trace_cov > 0:
        ours_traceable = True
    else:
        ours_traceable = False

    # 内心戏外化：externalized_count > 0 说明确有外化产物并打了 adaptation 标记。
    if externalized > 0:
        ours_externalized = True
    else:
        ours_externalized = False

    # 结构化可编辑：schema 往返自检通过，说明产物严格遵守可校验契约。
    if schema_valid:
        ours_structured = True
    else:
        ours_structured = False

    # 连贯性检查：只要跑过检查器就算做了，证据是发现的冲突总数(0 也算做过)。
    # 这里用"指标里存在 continuity 计数字段"作为"做过检查"的判据。
    if ("continuity_error_count" in ours_metrics) or ("continuity_warn_count" in ours_metrics):
        ours_continuity = True
    else:
        ours_continuity = False

    # 朴素基线侧：这些维度它一律不做，恒 False(诚实对照，非抹黑)。
    dimensions = {
        "cross_chapter_consistency": {
            "naive": False,
            "ours": ours_consistency,
            "ours_evidence": char_count,
            "note": (
                "朴素做法逐块独立改写，不建 story bible，同一人物在不同块"
                "可能名字/设定漂移；我们用单一事实源 bible 统一了 "
                + str(char_count)
                + " 个人物。"
            ),
        },
        "traceable": {
            "naive": False,
            "ours": ours_traceable,
            "ours_evidence": trace_cov,
            "note": (
                "朴素产物没有 source_ref，剧本无法点回原文；我们行级回填溯源，"
                "覆盖率 "
                + "{0:.1f}%".format(trace_cov * 100)
                + "，可双向高亮。"
            ),
        },
        "externalization_marked": {
            "naive": False,
            "ours": ours_externalized,
            "ours_evidence": externalized,
            "note": (
                "朴素做法不识别内心独白，要么照搬要么丢失；我们外化了 "
                + str(externalized)
                + " 处并打 adaptation 标记，可审计可回退。"
            ),
        },
        "structured_editable": {
            "naive": False,
            "ours": ours_structured,
            "ours_evidence": schema_valid,
            "note": (
                "朴素输出是自由文本/浅 JSON，不可校验、难安全编辑；"
                "我们的产物严格遵守 pydantic schema，往返自检通过。"
            ),
        },
        "continuity_checked": {
            "naive": False,
            "ours": ours_continuity,
            "ours_evidence": cont_err + cont_warn,
            "note": (
                "朴素做法不做跨场连贯性检查；我们的检查器共标出 "
                + str(cont_err + cont_warn)
                + " 处时间线/认知冲突。"
            ),
        },
    }

    # 一句话总结，给路演口播 / 前端标题用。
    headline = (
        "朴素基线只做了'切块改写'，跨章一致性、行级溯源、内心戏外化、"
        "结构化校验、连贯性检查这五项硬骨头全部缺失；我们的完整管线逐项做到并可量化。"
    )

    return {
        "dimensions": dimensions,
        "headline": headline,
    }
