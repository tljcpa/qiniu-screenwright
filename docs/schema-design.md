# Screenwright YAML Schema 设计文档

> 本文档描述 Screenwright 把小说转化为结构化剧本时所采用的数据契约（Schema）。
> 权威实现为 `backend/app/schema/models.py`（pydantic v2）。本文档与该文件严格一致；
> 如有出入，以代码为准。

---

## 1. 引言：剧本为什么需要一套结构化 Schema

把一部小说改写成剧本，最朴素的做法是让大模型直接吐出一段“看起来像剧本”的纯文本。
这种做法在演示层面够用，但无法支撑一个真正的编剧工作台，因为它丢失了结构。
Screenwright 的核心主张是：**剧本应当是一份可机读、可手改、可溯源、可重渲染的结构化数据，纯文本只是它的一种投影。**

围绕这一主张，Schema 设计有四个明确目标：

- **可机读（machine-readable）**：管线各阶段（抽取设定、场景切分、逐场生成、校验、导出）都围绕同一组类型流转；
  校验器能逐字段判定合法性，前端能按确定结构渲染分屏工作台。
- **可手改（human-editable）**：作者要能直接打开文件改一句对白、调一个转场、补一条人物关系，
  且改完仍然合法。这要求载体噪点低、结构靠缩进而非括号、允许注释。
- **可溯源（traceable）**：剧本里的每一个动作行、每一句对白，都能反查到它来自原文的哪一章、哪一段字符区间，
  从而支撑“点剧本→原文高亮”的双向溯源。
- **可重渲染（re-renderable）**：同一套人物/场景设定，应当能按不同目标媒介（电影 / 剧集 / 短剧）
  重新生成出风格不同的剧本，而不需要重新理解原著。

下文先给出顶层结构与一段完整示例，再逐模块讲解字段，然后展开核心设计决策的理由，
最后说明校验修复机制与可扩展性。

---

## 2. 顶层结构总览

一部剧本（`Screenplay`）由三部分构成：

```
Screenplay
├── meta          剧本元信息（标题、来源、目标媒介、语言、schema 版本）
├── story_bible   跨章一致性的单一事实源（人物 / 地点 / 时间线）
└── scenes        有序场景列表，每场内部是有序带类型的元素序列
```

`meta` 回答“这是什么、改给谁看”；`story_bible` 回答“故事世界里有谁、有哪些地方、时间怎么排”；
`scenes` 才是剧本正文。三者解耦的意义在第 4 节展开。

下面是一段可被 `Screenplay.from_yaml` 直接解析的完整示例（字段名与代码完全一致）：

```yaml
meta:
  title: 雪夜归人
  source:
    type: novel
    chapters: [1, 2, 3]
  target_medium: short_drama
  schema_version: "1.0"
  language: zh

story_bible:
  characters:
    - id: char_lin
      name: 林青
      aliases: [青儿]
      traits: [隐忍, 决绝]
      arc: 从沉默承受到主动反击
      relationships:
        - to: char_zhao
          type: 宿敌
    - id: char_zhao
      name: 赵承
      aliases: []
      traits: [傲慢]
      arc: ""
      relationships: []
  locations:
    - id: loc_courtyard
      name: 赵府后院
    - id: loc_street
      name: 长街
  timeline:
    - id: tp_night1
      label: 第一夜·大雪
      order: 1
    - id: tp_dawn1
      label: 翌日·破晓
      order: 2

scenes:
  - id: sc_001
    heading:
      int_ext: EXT
      location_id: loc_courtyard
      time_of_day: 夜
      time_ref: tp_night1
    source_ref:
      chapter: 1
      spans:
        - start: 0
          end: 420
    characters: [char_lin, char_zhao]
    synopsis: 雪夜对峙，林青第一次直面赵承。
    elements:
      - type: action
        text: 大雪压枝。林青立在廊下，指节因攥紧而泛白。
        source_ref:
          chapter: 1
          spans:
            - start: 12
              end: 58
      - type: dialogue
        character: char_zhao
        line: 这么晚，你还不死心？
        parenthetical: 冷笑
        source_ref:
          chapter: 1
          spans:
            - start: 60
              end: 92
      - type: action
        text: 林青垂眸，缓缓将袖中短刀握紧——她知道，退一步便再无退路。
        source_ref:
          chapter: 1
          spans:
            - start: 95
              end: 160
        adaptation:
          from: interior_monologue
          technique: action
      - type: transition
        text: CUT TO
    continuity_flags:
      - level: info
        msg: 本场时间点 tp_night1 与第 2 场衔接正常。
        scene_ids: [sc_001, sc_002]
```

这段示例同时包含了三种 element（`action` / `dialogue` / `transition`）、一处 `adaptation`（把原文的内心独白外化为动作）、
元素级 `source_ref`（精确到字符区间），以及一条 `continuity_flag`。

---

## 3. 逐模块字段说明

字段类型、默认值、取值域均以 `models.py` 为准。下文取值域中的 `Literal[...]` 表示该字段只能取列举的值。

### 3.1 Meta / SourceMeta

`Meta`（`Screenplay.meta`）：

| 字段 | 类型 | 默认 | 含义 |
|------|------|------|------|
| `title` | `str` | 必填 | 剧本标题。 |
| `source` | `SourceMeta` | 必填 | 来源元信息。 |
| `target_medium` | `Literal["film","series","short_drama"]` | `"film"` | 目标媒介，支撑可控媒介改编。 |
| `schema_version` | `str` | `"1.0"` | Schema 版本号，便于未来演进与兼容判定。 |
| `language` | `str` | `"zh"` | 输出语言，默认中文（随输入语言）。 |

`SourceMeta`（`meta.source`）：

| 字段 | 类型 | 默认 | 含义 |
|------|------|------|------|
| `type` | `Literal["novel"]` | `"novel"` | 来源类型，当前只支持小说。 |
| `chapters` | `list[int]` | 必填 | 本剧本覆盖的章号列表。 |

### 3.2 StoryBible 及其条目

`StoryBible`（`Screenplay.story_bible`）是跨章一致性的单一事实源，由三个列表组成：

| 字段 | 类型 | 含义 |
|------|------|------|
| `characters` | `list[Character]` | 全部人物。 |
| `locations` | `list[Location]` | 全部地点。 |
| `timeline` | `list[TimePoint]` | 时间线节点。 |

`Character`：

| 字段 | 类型 | 默认 | 含义 |
|------|------|------|------|
| `id` | `str` | 必填 | 稳定标识（如 `char_lin`）。全篇引用人物一律用 id，不用 name。 |
| `name` | `str` | 必填 | 人物显示名。 |
| `aliases` | `list[str]` | `[]` | 别名/称呼。 |
| `traits` | `list[str]` | `[]` | 性格标签。 |
| `arc` | `str` | `""` | 人物弧线描述。 |
| `relationships` | `list[Relationship]` | `[]` | 人物关系边。 |

`Relationship`：`to: str`（指向另一个 character id）、`type: str`（关系描述，如“宿敌”“父女”）。

`Location`：`id: str`、`name: str`。`Heading.location_id` 引用其 `id`。

`TimePoint`：`id: str`、`label: str`、`order: int`。`order` 给连贯性检查排序用；`Heading.time_ref` 可关联其 `id`。

> 设计要点：人物、地点、时间点全部以 **id 引用** 而非内联展开。
> 这避免了同一人物在不同场景被重复描述、进而出现自相矛盾的设定漂移。

### 3.3 Scene / Heading / SourceRef / Span

`Scene`：

| 字段 | 类型 | 默认 | 含义 |
|------|------|------|------|
| `id` | `str` | 必填 | 场景标识（如 `sc_001`）。 |
| `heading` | `Heading` | 必填 | 场景标题行（slugline）。 |
| `source_ref` | `SourceRef` | 必填 | 场级溯源（本场整体对应的原文区间）。 |
| `characters` | `list[str]` | 必填 | 本场出场人物的 character id 列表。 |
| `synopsis` | `str` | `""` | 本场梗概。 |
| `elements` | `list[Element]` | 必填 | 有序带类型的元素序列（剧本正文核心载体）。 |
| `continuity_flags` | `list[ContinuityFlag]` | `[]` | 连贯性检查标记。 |

`Heading`：

| 字段 | 类型 | 默认 | 含义 |
|------|------|------|------|
| `int_ext` | `Literal["INT","EXT","INT/EXT"]` | 必填 | 内景/外景/内外景。 |
| `location_id` | `str` | 必填 | 引用 `Location.id`。 |
| `time_of_day` | `str` | 必填 | 日/夜/黄昏等自由文本。 |
| `time_ref` | `str \| None` | `None` | 可选关联 `TimePoint.id`，供连贯性检查。 |

`SourceRef`：`chapter: int`（章号，1-based）、`spans: list[Span]`。
允许一个对象对应原文多段（`spans` 是列表），以覆盖跨段落改写的情况。

`Span`：`start: int`、`end: int`，是原文中的字符偏移区间，前端据此精确高亮。
Span 是整套溯源机制的最小单位。

### 3.4 三种 Element（判别联合）

`elements` 列表里的每一项都是一个 `Element`，由 `type` 字段判别为以下三种之一：

`ActionElement`（动作/场景描述行）：

| 字段 | 类型 | 默认 | 含义 |
|------|------|------|------|
| `type` | `Literal["action"]` | 必填 | 判别字段。 |
| `text` | `str` | 必填 | 动作描述正文。 |
| `source_ref` | `SourceRef \| None` | `None` | 元素级溯源。 |
| `adaptation` | `Adaptation \| None` | `None` | 非空表示此行由内心戏外化而来。 |

`DialogueElement`（对白行）：

| 字段 | 类型 | 默认 | 含义 |
|------|------|------|------|
| `type` | `Literal["dialogue"]` | 必填 | 判别字段。 |
| `character` | `str` | 必填 | 说话人的 character id。 |
| `line` | `str` | 必填 | 台词正文。 |
| `parenthetical` | `str \| None` | `None` | 括号提示（如“冷笑”）。 |
| `source_ref` | `SourceRef \| None` | `None` | 元素级溯源。 |
| `adaptation` | `Adaptation \| None` | `None` | 非空表示此对白由内心戏外化而来。 |

`TransitionElement`（转场行）：

| 字段 | 类型 | 默认 | 含义 |
|------|------|------|------|
| `type` | `Literal["transition"]` | 必填 | 判别字段。 |
| `text` | `str` | 必填 | 转场文本（如 `CUT TO` / `FADE OUT`）。 |

> 注意：`TransitionElement` 刻意不带 `source_ref` 与 `adaptation`。
> 转场是结构性标记，不对应原文具体字句，也不存在“外化”概念，因此不给这两个字段，避免无意义的空位。

### 3.5 Adaptation（外化透明标记）

`Adaptation` 记录一行内容是“从原文的什么、外化成什么手法”得来的：

| 字段 | 类型 | 取值域 | 含义 |
|------|------|--------|------|
| `from`（Python 端字段名 `from_`） | `Literal[...]` | `interior_monologue` / `narration` / `description` | 原文是什么：内心独白 / 旁白叙述 / 描写。 |
| `technique` | `Literal[...]` | `subtext` / `action` / `voiceover` / `visual` | 外化成什么手法：潜台词 / 动作 / 画外音 / 视觉化。 |

`from` 在 YAML 中是键名，在 Python 端字段名为 `from_`（因为 `from` 是 Python 关键字），别名机制见第 5 节。

### 3.6 ContinuityFlag（连贯性标记）

`ContinuityFlag`：`level: Literal["info","warn","error"]`、`msg: str`、`scene_ids: list[str] = []`。
`level` 决定前端展示级别（提示/标黄/标红），`scene_ids` 标出涉及的场景，便于跨场冲突定位。

---

## 4. 核心设计决策与理由

这一节是本文档的重点。以下七条决策共同决定了 Schema 为什么长这样。

### 4.1 用 YAML 而非 JSON

剧本是要给**人**改的文件，不是纯粹的机器间传输报文。YAML 相比 JSON 有三处对“可手改”至关重要的优势：

- **缩进即结构**：层级靠缩进表达，没有成片的花括号和逗号，作者改一句对白时视觉噪点远小于 JSON。
- **可注释**：YAML 支持 `#` 注释，作者可以就地写下“这里我改了赵承的语气”这类批注，JSON 不行。
- **字符串友好**：大段中文对白、多行动作描述在 YAML 里几乎不需要转义，JSON 则要处理大量引号与换行转义。

题目本身也指定交付 YAML Schema。因此 YAML 是产品定位（手改优先）与赛题要求的共同结论。
JSON 仍作为内部容错入口存在（见第 5 节 `validate.py`），但**对外主格式是 YAML**。

### 4.2 elements 用“有序带类型序列”而非按类型分桶（最重要的决策）

一个直觉但错误的设计是把每场拆成 `actions: list[...]`、`dialogues: list[...]`、`transitions: list[...]` 三个桶。
我们明确拒绝这种结构，理由是：

**剧本的本质是时间序列，顺序本身承载叙事。** 一段“动作—对白—动作—转场”的节奏，
其中元素的先后顺序就是导演调度和观众体验的一部分。一旦按类型分桶，元素间的相对顺序就被抹掉了——
你再也无法表达“先这句台词，再这个动作，然后这句反问”。要在分桶结构里恢复顺序，
就得给每个元素再加一个全局序号字段，这既冗余又脆弱（手改插入一行就要重排所有序号）。

因此 `elements` 是一个**单一的有序列表**，列表里混排三种类型，每个元素自带 `type` 判别字段。
pydantic 端用 `Annotated[Union[...], Field(discriminator="type")]` 实现判别联合（discriminated union）：
解析时直接读 `type` 选对应子模型校验，而不是逐个 try。这带来两个好处——
错误信息精确（不会糊成“三个子类型都不匹配”），解析性能也更好。

这是整套 Schema 里最关键的一处取舍：它让“顺序”成为一等公民，直接服务于“可手改”和“忠实表达叙事节奏”。

### 4.3 source_ref 下沉到 element 级

仅有场级溯源（`Scene.source_ref`）只能做到“这一整场来自第几章大概哪一段”，
而双向溯源的产品价值在于**精确到行**：点击剧本里的某一句对白，原文里对应的那几十个字立刻高亮。

为此 `ActionElement` 和 `DialogueElement` 各自带一个可选的 `source_ref`，
其中 `spans` 是字符偏移区间列表。字符偏移（而非行号或段落号）让前端可以做到字符级精确高亮，
不受原文排版换行的影响。场级 `source_ref` 仍然保留，作为元素级溯源缺失时的兜底。

### 4.4 story_bible 作为单一事实源

跨章一致性是本项目刻意啃的硬骨头。如果每个场景各自描述人物、地点，三章下来必然出现设定漂移
（同一个人前后性格矛盾、地名不一致）。解决办法是把人物/地点/时间线抽到顶层 `story_bible`，
作为**单一事实源（single source of truth）**：

- 场景内部只用 **id 引用**（`characters: [char_lin]`、`location_id: loc_courtyard`、`time_ref: tp_night1`），不内联展开。
- 逐场生成时，管线把 bible 的相关切片注入提示词，约束模型不要凭空发明或改写已确立的设定。

这样一致性问题从“事后逐场比对”变成了“结构上无法分叉”。

### 4.5 adaptation 标记：内心戏外化的透明与可回退

小说大量依赖内心独白，而剧本无法直接拍“心里想什么”，必须外化为可见可听的手法
（潜台词、动作、画外音、视觉化）。这一步是创造性改写，也最容易引起“这是不是篡改原著”的疑虑。

`Adaptation` 标记的意义就是把这步改写**显式记录下来**：哪一行是外化来的（`from`）、用了什么手法（`technique`）。
由此带来两层价值——**透明**：评审与作者能一眼看出哪些是忠实转写、哪些是主动改编；
**可回退**：因为改写被结构化标注，工具可以筛出所有 `adaptation` 非空的元素，支持审计甚至一键还原对照。
它既是创新点，也是对“改编可信度”的工程化交代。

### 4.6 target_medium 放在 meta

电影、剧集、短剧的节奏、时长、台词密度差异巨大。我们的设计是：
**人物与场景设定（bible + scenes 的骨架）与媒介解耦，媒介风格只在生成时套用。**

`target_medium` 因此放在顶层 `meta`，而不是散落在每个场景里。这样同一套 `story_bible` 和场景切分，
可以按不同 `target_medium` 重渲染出风格不同的剧本——短剧版强调金句与强冲突，电影版更舒缓。
把它放 meta 是“一处声明、全局生效”的自然结果，直接支撑可控媒介改编这一创新点。

### 4.7 time_ref / continuity_flags：为连贯性检查预留结构位

连贯性检查（跨场时间线、人物认知冲突）是有余力时的进阶能力。即便检查器尚未跑满，
Schema 也提前留好了结构位，避免日后改动契约：

- `Heading.time_ref` 让每场可关联到 `timeline` 上的某个 `TimePoint`，配合 `TimePoint.order` 就能判断场序与时序是否矛盾。
- `Scene.continuity_flags` 用 `ContinuityFlag` 承载检查结果，`level` 分级、`scene_ids` 定位涉及场景。

这两处都是**可选/默认空**字段，不影响基础流程的合法性，却为连贯性检查器留好了落点。

---

## 5. 校验与修复

实现见 `backend/app/schema/models.py` 与 `backend/app/schema/validate.py`。

### 5.1 判别联合保证类型安全

`Element` 定义为：

```python
Element = Annotated[
    Union[ActionElement, DialogueElement, TransitionElement],
    Field(discriminator="type"),
]
```

`discriminator="type"` 让 pydantic 在校验每个元素时直接按 `type` 的值选定唯一子模型，
随后做该子模型的全字段校验。好处是：类型选择是确定的而非试错的，
非法元素（如 `type: dialogue` 却缺 `character`）会得到精确指向该子模型的错误信息，而不是含糊的联合匹配失败。
整个 `Screenplay.model_validate` 会自上而下递归触发所有子模型校验，任何字段越界（如 `int_ext` 取了非法值）都会被拦下。

### 5.2 from 别名保证 round-trip

`Adaptation.from_` 是序列化往返一致性的关键设计：

```python
class Adaptation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: Literal[...] = Field(alias="from")
    technique: Literal[...]
```

- Python 端字段名必须是 `from_`（`from` 是关键字，不能直接作字段名）。
- `alias="from"` 让它在 YAML/JSON 里的键名是 `from`。
- `populate_by_name=True` 允许同时用字段名 `from_` 或别名 `from` 来构造对象，
  因此单元测试里写 `Adaptation(from_=..., technique=...)`、以及从 YAML 的 `from` 读入，两条路都成立。

往返由 `Screenplay` 的两个方法保证：

- `to_yaml()` 用 `model_dump(by_alias=True, exclude_none=True)`，
  `by_alias=True` 让 `from_` 输出成 `from`，`exclude_none=True` 去掉值为 None 的可选字段（如未填的 `source_ref`），YAML 更干净；
  再 `yaml.safe_dump(allow_unicode=True, sort_keys=False)`，中文不转义、字段保持定义顺序，便于人工 diff 与手改。
- `from_yaml()` 用 `yaml.safe_load` + `model_validate`，别名自动回读，判别联合按 `type` 选子模型。

`to_yaml` → `from_yaml` 因此构成稳定往返，这是“可手改”落地的前提：作者改完导出的文件能被原样读回。

### 5.3 validate_and_repair 的容错

`validate_and_repair(raw, llm, max_retries=2)` 是 Pass4 校验修复的核心：

1. **统一入口**：`_coerce_to_dict` 把输入归一成 dict。字符串优先尝试 `json.loads`（报错更精确），失败再退回 `yaml.safe_load`（YAML 是 JSON 超集）；解析结果若不是映射则判为非法结构。
2. **尝试构造**：`_try_build` 调 `Screenplay.model_validate` 触发完整校验，成功即返回。
3. **失败回喂**：捕获 `ValidationError / ValueError / TypeError`，把**校验错误文本 + 原始内容**拼成修复对话喂回 LLM，要求其只输出修复后的完整 YAML（不带解释、不带代码围栏）。用 LLM 的新产出作为下一轮待校验内容。
4. **重试上限**：总尝试数为 `max_retries + 1`；用尽仍失败则抛出最后一次异常。

`llm` 在此处是鸭子类型——任何提供 `complete(messages, json=False) -> str | dict` 的对象都可注入，
本模块不依赖任何具体 client，也不主动发请求（是否真的调用外部 API 由传入的 `llm` 决定）。
这一设计让“LLM 产出不一定合法”这一现实，被收敛成“非法即回喂修复”的闭环，而不是直接报错中断管线。

---

## 6. 可扩展性

Schema 在三个方向上为演进留了空间，且都能做到向后兼容：

- **新增 element 类型**：剧本未来可能需要 `parenthetical` 独立行、`shot`（镜头）、`dual_dialogue`（双人对白）等。
  做法是新增一个带唯一 `type` 字面量的子模型，并把它加入 `Element` 的 `Union`。
  因为判别靠 `type`，旧数据完全不受影响，旧的三种元素照常解析。这正是判别联合相比分桶结构在扩展性上的又一优势。
- **新增目标媒介**：`target_medium` 是 `Literal`，新增如 `animation`、`stage_play` 只需扩展字面量取值，
  生成层补上对应的媒介风格策略即可，结构无须改动。同理 `SourceMeta.type` 未来要支持小说以外的来源（如剧本互转）也是扩展 `Literal`。
- **新增语言**：`language` 是自由 `str` 且默认随输入，本身已支持任意语言；
  多语言输出只是生成层与提示词的事，Schema 不构成约束。
- **版本演进**：`schema_version` 显式存在（默认 `"1.0"`），未来若发生破坏性变更，
  可据此做版本判定与迁移，老文件不会被静默误读。

整体原则是：**新增走扩展（加子模型、加 Literal 取值），尽量不动既有字段语义**，从而保护已生成剧本的可读回性。

---

## 附：与代码的对应关系

本文档所有字段名、类型、默认值、取值域均直接对应 `backend/app/schema/models.py` 中的同名定义，
校验与修复行为对应 `backend/app/schema/validate.py`。第 2 节的 YAML 示例可被 `Screenplay.from_yaml` 解析。
若代码后续演进，请同步更新本文档并提升 `schema_version`。
