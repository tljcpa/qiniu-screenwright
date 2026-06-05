// ============================================================================
// API 客户端层
// ----------------------------------------------------------------------------
// 本文件做两件事：
//   1. 定义与后端 SCHEMA 契约(见 BRIEF 第6节)严格对齐的 TypeScript 类型。
//   2. 封装将来要调用的真实 /api/* 函数：convert / regenerateScene / getSample
//      / exportAs。本轮内部先返回 mock 数据，但签名按真实接口设计，
//      切到真后端只需把每个函数体里的 mock 分支换成 fetch(见 USE_MOCK 开关)。
// ============================================================================

// ---------------------------------------------------------------------------
// 类型定义（对应后端 pydantic 模型）
// ---------------------------------------------------------------------------

// 目标媒介：电影 / 剧集 / 短剧（创新点①）
export type TargetMedium = 'film' | 'series' | 'short_drama'

// 导出格式
export type ExportFormat = 'yaml' | 'fountain' | 'pdf'

// 原文字符区间(基于该章 text 的字符偏移)
export interface Span {
  start: number
  end: number
}

// 溯源引用：定位到第几章 + 一组字符区间（创新点②的数据基石）
export interface SourceRef {
  chapter: number
  spans: Span[]
}

// 内心戏外化标记（创新点③）。
// 关键：后端用 pydantic 的 alias="from" + model_dump(by_alias=True) 序列化，
//       真实 API 的 JSON 键是 "from"(不是 "from_")。
//       所以前端类型以 from 为准；同时保留可选的 from_ 仅为兼容历史 mock 数据。
export interface Adaptation {
  from?: 'interior_monologue' | 'narration' | 'description'
  // 兼容历史 mock：极少数旧数据可能用 from_，渲染时以 from 优先、from_ 兜底。
  from_?: 'interior_monologue' | 'narration' | 'description'
  technique: 'subtext' | 'action' | 'voiceover' | 'visual'
}

// 动作元素
export interface ActionElement {
  type: 'action'
  text: string
  source_ref?: SourceRef | null
  adaptation?: Adaptation | null
}

// 对白元素
export interface DialogueElement {
  type: 'dialogue'
  character: string // character id
  line: string
  parenthetical?: string | null
  source_ref?: SourceRef | null
  adaptation?: Adaptation | null
}

// 转场元素
export interface TransitionElement {
  type: 'transition'
  text: string
}

// 元素：有序带类型序列(剧本的核心设计)。用 type 判别联合。
export type Element = ActionElement | DialogueElement | TransitionElement

// 场标题
export interface Heading {
  int_ext: 'INT' | 'EXT' | 'INT/EXT'
  location_id: string
  time_of_day: string
  time_ref?: string | null
}

// 连贯性冲突标记（创新点④）
export interface ContinuityFlag {
  level: 'info' | 'warn' | 'error'
  msg: string
  scene_ids: string[]
}

// 场
export interface Scene {
  id: string
  heading: Heading
  source_ref: SourceRef
  characters: string[]
  synopsis: string
  elements: Element[]
  continuity_flags: ContinuityFlag[]
}

// 人物关系
export interface Relationship {
  to: string
  type: string
}

// 人物
export interface Character {
  id: string
  name: string
  aliases: string[]
  traits: string[]
  arc: string
  relationships: Relationship[]
}

// 地点
export interface Location {
  id: string
  name: string
}

// 时间点
export interface TimePoint {
  id: string
  label: string
  order: number
}

// 故事圣经：跨章一致性的单一事实源
export interface StoryBible {
  characters: Character[]
  locations: Location[]
  timeline: TimePoint[]
}

// 来源元数据
export interface SourceMeta {
  type: 'novel'
  chapters: number[]
}

// 元信息
export interface Meta {
  title: string
  source: SourceMeta
  target_medium: TargetMedium
  schema_version: string
  language: string
}

// 顶层剧本
export interface Screenplay {
  meta: Meta
  story_bible: StoryBible
  scenes: Scene[]
}

// 原文章节(前端展示左侧原文 + 做溯源定位用)。
// 注：后端契约里溯源是 chapter 编号 + 字符偏移，前端需要拿到每章原文 text 才能高亮，
// 所以约定 sample/convert 返回里附带 chapters。
export interface Chapter {
  index: number
  title: string
  text: string
}

// getSample / convert 返回的完整工作台数据包
export interface WorkbenchData {
  screenplay: Screenplay
  chapters: Chapter[]
}

// 质量看板指标
export interface QualityMetrics {
  scene_count: number
  dialogue_count: number
  externalization_count: number // 内心戏外化条数
  trace_coverage: number // 溯源覆盖率 0..1
  continuity_conflicts: number
}

// ---------------------------------------------------------------------------
// 真假数据开关：将来后端就绪，把 USE_MOCK 置 false 即可整体切换。
// ---------------------------------------------------------------------------
const USE_MOCK = false

// 模拟网络延迟，让 mock 下的 loading / 进度也能演示
function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// 后端基址。Vite dev 下用代理或同源；这里用相对路径，部署时由反代统一接管。
const API_BASE = ''

// 转换进度事件(对应后端 SSE 的 stage 事件 payload)。
export interface ConvertProgress {
  stage: string
  detail?: string
  pct?: number
}

// 后端 done 事件原始结构(screenplay + metrics + chapters)。
interface DoneEvent {
  stage: 'done'
  screenplay: Screenplay
  metrics: Record<string, unknown>
  chapters: Chapter[]
}

// ---------------------------------------------------------------------------
// 对外 API 函数（签名即将来真实接口）
// ---------------------------------------------------------------------------

// 转换：小说原文 + 目标媒介 -> 结构化剧本
// 真实对接点：POST /api/convert (后端为 SSE 进度流)。
// onProgress 可选：每收到一个 stage 事件就回调一次，用于驱动前端进度条。
// 实现要点：后端用 text/event-stream 返回，逐事件是一段 "data: {json}\n\n"。
// 浏览器原生 EventSource 只支持 GET，这里是 POST，所以用 fetch + ReadableStream
// 手动读字节流、按 "\n\n" 切分事件块、再逐块解析 JSON。
export async function convert(
  text: string,
  medium: TargetMedium,
  onProgress?: (p: ConvertProgress) => void,
): Promise<WorkbenchData> {
  if (USE_MOCK) {
    // mock 下也模拟几个 stage 进度，让 onProgress 能演示
    if (onProgress) {
      const steps: ConvertProgress[] = [
        { stage: 'ingest', detail: '分章分块', pct: 5 },
        { stage: 'bible', detail: '构建故事圣经', pct: 25 },
        { stage: 'segment', detail: '场景切分', pct: 45 },
        { stage: 'generate', detail: '逐场生成', pct: 60 },
        { stage: 'annotate', detail: '连贯性检查', pct: 85 },
        { stage: 'metrics', detail: '计算指标', pct: 95 },
      ]
      for (const s of steps) {
        await delay(120)
        onProgress(s)
      }
    } else {
      await delay(600)
    }
    const data = buildMockWorkbench(medium)
    // mock 下忽略传入 text，仅按媒介渲染样例；真实后端会用 text
    void text
    return data
  }

  const res = await fetch(API_BASE + '/api/convert', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, medium }),
  })
  if (!res.ok || !res.body) {
    throw new Error('convert failed: ' + res.status)
  }

  // 读字节流并增量解码成文本。
  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let done: DoneEvent | null = null

  // 解析一个 SSE 事件块(可能含多行 data:)，返回拼接后的 JSON 字符串或 null。
  function extractData(block: string): string | null {
    const dataLines: string[] = []
    for (const line of block.split('\n')) {
      if (line.startsWith('data:')) {
        dataLines.push(line.slice('data:'.length).trimStart())
      }
    }
    if (dataLines.length === 0) {
      return null
    }
    return dataLines.join('\n')
  }

  // 处理一个完整事件块。
  function handleBlock(block: string): void {
    const payload = extractData(block)
    if (payload === null || payload === '') {
      return
    }
    let evt: { stage?: string } & Record<string, unknown>
    try {
      evt = JSON.parse(payload)
    } catch {
      // 半截/非 JSON 块跳过(理论上不会发生，因为只在 \n\n 边界处理)。
      return
    }
    if (evt.stage === 'error') {
      const detail = typeof evt.detail === 'string' ? evt.detail : '转换失败'
      throw new Error(detail)
    }
    if (evt.stage === 'done') {
      done = evt as unknown as DoneEvent
      return
    }
    // 其余视为 stage 进度事件。
    if (onProgress) {
      onProgress({
        stage: String(evt.stage || ''),
        detail: typeof evt.detail === 'string' ? evt.detail : undefined,
        pct: typeof evt.pct === 'number' ? evt.pct : undefined,
      })
    }
  }

  // 流式读取：每读到一段就追加到 buffer，按 "\n\n" 切出完整事件块处理。
  // 关键：sse-starlette 用 CRLF("\r\n\r\n")分隔事件，先归一化成 "\n" 再按 "\n\n" 切，
  // 否则永远切不出事件块(实测踩过这个坑)。
  for (;;) {
    const { value, done: streamDone } = await reader.read()
    if (value) {
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
      let sep = buffer.indexOf('\n\n')
      while (sep !== -1) {
        const block = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        handleBlock(block)
        sep = buffer.indexOf('\n\n')
      }
    }
    if (streamDone) {
      break
    }
  }
  // 处理可能残留在 buffer 里的最后一个块(无尾随 \n\n 的情况)。
  if (buffer.trim()) {
    handleBlock(buffer)
  }

  if (!done) {
    throw new Error('convert failed: 未收到 done 事件')
  }
  // 组装前端工作台数据包：screenplay + chapters(metrics 前端自行从剧本派生)。
  const d: DoneEvent = done
  return { screenplay: d.screenplay, chapters: d.chapters }
}

// 增量重生成某一场（编辑安全：只动这一场）
// 真实对接点：POST /api/regenerate_scene
// 后端需要整份 screenplay 来重建上下文，并只返回更新后的这一场。
// medium/text 可选：text 给上则用原文重建 Novel，溯源更准。
export async function regenerateScene(
  screenplay: Screenplay,
  sceneId: string,
  instruction: string,
  medium?: TargetMedium,
  text?: string,
): Promise<Scene> {
  if (USE_MOCK) {
    await delay(400)
    const found = screenplay.scenes.find((s) => s.id === sceneId)
    if (!found) throw new Error('scene not found: ' + sceneId)
    // mock 下把指令附到 synopsis 末尾，演示"指令生效"
    return { ...found, synopsis: found.synopsis + `（已按指令重生成：${instruction}）` }
  }
  const res = await fetch(API_BASE + '/api/regenerate_scene', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      screenplay,
      scene_id: sceneId,
      instruction,
      medium,
      text,
    }),
  })
  if (!res.ok) throw new Error('regenerate failed: ' + res.status)
  return (await res.json()) as Scene
}

// 加载样例
// 真实对接点：GET /api/sample。
// 注：后端 /api/sample 返回的是样本列表 [{id,title,text}]，不是 WorkbenchData。
// 前端约定取第一条样本原文，再走 convert 得到完整工作台数据。
export async function getSample(
  medium: TargetMedium = 'film',
  onProgress?: (p: ConvertProgress) => void,
): Promise<WorkbenchData> {
  if (USE_MOCK) {
    await delay(300)
    return buildMockWorkbench(medium)
  }
  const res = await fetch(API_BASE + '/api/sample')
  if (!res.ok) throw new Error('sample failed: ' + res.status)
  const samples = (await res.json()) as { id: string; title: string; text: string }[]
  if (!samples.length || !samples[0].text) {
    throw new Error('sample failed: 无可用样本')
  }
  // 用第一条样本(中文《旧城咖啡》)的原文真跑 convert。
  return convert(samples[0].text, medium, onProgress)
}

// 导出为指定格式，返回一个可下载的文本/二进制 Blob
// 真实对接点：POST /api/export
export async function exportAs(
  format: ExportFormat,
  screenplay: Screenplay,
): Promise<Blob> {
  if (USE_MOCK) {
    await delay(200)
    const content = mockExportContent(format, screenplay)
    return new Blob([content], { type: 'text/plain;charset=utf-8' })
  }
  const res = await fetch(API_BASE + '/api/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ format, screenplay }),
  })
  if (!res.ok) throw new Error('export failed: ' + res.status)
  return await res.blob()
}

// ---------------------------------------------------------------------------
// 质量看板：从剧本派生指标（本轮真算 + 部分 mock）
// ---------------------------------------------------------------------------
export function computeMetrics(data: WorkbenchData): QualityMetrics {
  const scenes = data.screenplay.scenes
  let dialogue = 0
  let externalization = 0
  let elemsTotal = 0
  let elemsTraced = 0
  for (const sc of scenes) {
    for (const el of sc.elements) {
      elemsTotal += 1
      if (el.type === 'dialogue') dialogue += 1
      if (el.type !== 'transition' && el.source_ref) elemsTraced += 1
      if ((el.type === 'action' || el.type === 'dialogue') && el.adaptation) {
        externalization += 1
      }
    }
  }
  const conflicts = scenes.reduce(
    (n, s) => n + s.continuity_flags.filter((f) => f.level !== 'info').length,
    0,
  )
  let coverage = 0
  if (elemsTotal > 0) {
    coverage = elemsTraced / elemsTotal
  }
  return {
    scene_count: scenes.length,
    dialogue_count: dialogue,
    externalization_count: externalization,
    trace_coverage: coverage,
    continuity_conflicts: conflicts,
  }
}

// ===========================================================================
// 以下为 MOCK 数据区（真后端就绪后整块删除即可）
// 造一份《旧城咖啡》风格的中文样例：3 章原文 + 3 场剧本，含 source_ref 与外化元素。
// 关键：source_ref 的 spans 必须能在对应 chapter 的 text 上精确切片，
//       否则左侧高亮会错位。下方文字与偏移是手工对齐过的。
// ===========================================================================

// 第一章原文
const CH1 = '林晚推开旧城咖啡的木门，铜铃轻响。她已经三年没回这条街了。柜台后的男人抬起头，是周屿。两人都没有说话，空气里只有咖啡机低低的嗡鸣。她想，他大概早就把我忘了吧，可他的手却停在了半空。'

// 第二章原文
const CH2 = '周屿给她端来一杯没有加糖的拿铁，正是她当年的习惯。林晚的指尖碰到杯壁，温热。窗外开始下雨，旧城的青石板被打湿，反着灰白的光。他终于开口：还以为你不会回来了。她笑了笑，没有回答，心里却翻江倒海。'

// 第三章原文
const CH3 = '雨停的时候，林晚起身告辞。周屿送她到门口，欲言又止。铜铃又响了一声，像是某种告别，也像是重新开始。街角的梧桐落下一片叶子，恰好落在她的肩上。'

// 工具：在长文本里找子串的字符区间，便于手工对齐 spans
function span(text: string, sub: string): Span {
  const start = text.indexOf(sub)
  return { start, end: start + sub.length }
}

// 按媒介构造一份工作台数据。短剧节奏更快、金句化；电影/剧集偏写实。
// 为聚焦演示，三种媒介共用同一 bible 与场骨架，仅个别对白/转场措辞随媒介微调（体现①）。
function buildMockWorkbench(medium: TargetMedium): WorkbenchData {
  const chapters: Chapter[] = [
    { index: 1, title: '第一章 重逢', text: CH1 },
    { index: 2, title: '第二章 旧习惯', text: CH2 },
    { index: 3, title: '第三章 雨停', text: CH3 },
  ]

  // 短剧媒介下，给一句更"钩子化"的台词，演示同源不同渲染
  let openLine = '（沉默片刻）……好久不见。'
  if (medium === 'short_drama') {
    openLine = '三年了，你怎么敢回来？'
  }

  const screenplay: Screenplay = {
    meta: {
      title: '旧城咖啡',
      source: { type: 'novel', chapters: [1, 2, 3] },
      target_medium: medium,
      schema_version: '1.0',
      language: 'zh',
    },
    story_bible: {
      characters: [
        {
          id: 'char_lin',
          name: '林晚',
          aliases: ['晚'],
          traits: ['克制', '念旧', '外柔内刚'],
          arc: '从逃避过去到直面重逢',
          relationships: [{ to: 'char_zhou', type: '旧情人' }],
        },
        {
          id: 'char_zhou',
          name: '周屿',
          aliases: ['屿'],
          traits: ['沉默', '深情', '隐忍'],
          arc: '从守店等待到主动开口',
          relationships: [{ to: 'char_lin', type: '旧情人' }],
        },
      ],
      locations: [
        { id: 'loc_cafe', name: '旧城咖啡馆' },
        { id: 'loc_door', name: '咖啡馆门口' },
      ],
      timeline: [
        { id: 'tp_1', label: '重逢当日 下午', order: 1 },
        { id: 'tp_2', label: '重逢当日 雨中', order: 2 },
        { id: 'tp_3', label: '重逢当日 雨停', order: 3 },
      ],
    },
    scenes: [
      // 第 1 场：对应第一章
      {
        id: 'sc_001',
        heading: {
          int_ext: 'INT',
          location_id: 'loc_cafe',
          time_of_day: '日',
          time_ref: 'tp_1',
        },
        source_ref: { chapter: 1, spans: [{ start: 0, end: CH1.length }] },
        characters: ['char_lin', 'char_zhou'],
        synopsis: '林晚三年后重回旧城咖啡，与周屿无言相对。',
        elements: [
          {
            type: 'action',
            text: '林晚推开旧城咖啡的木门，铜铃轻响。柜台后的周屿抬起头，两人沉默相对，唯有咖啡机低鸣。',
            source_ref: {
              chapter: 1,
              spans: [span(CH1, '林晚推开旧城咖啡的木门，铜铃轻响。'), span(CH1, '柜台后的男人抬起头，是周屿。')],
            },
            adaptation: null,
          },
          {
            // 内心戏外化：原文"她想，他大概早就把我忘了吧" -> 用动作/潜台词表现（创新点③）
            type: 'action',
            text: '林晚的目光闪躲，下意识攥紧了包带；周屿停在半空的手，泄露了他并未忘记。',
            source_ref: {
              chapter: 1,
              spans: [span(CH1, '她想，他大概早就把我忘了吧，可他的手却停在了半空。')],
            },
            adaptation: { from_: 'interior_monologue', technique: 'subtext' },
          },
          {
            type: 'dialogue',
            character: 'char_zhou',
            line: openLine,
            parenthetical: '声音很轻',
            source_ref: {
              chapter: 1,
              spans: [span(CH1, '两人都没有说话')],
            },
            adaptation: null,
          },
        ],
        continuity_flags: [],
      },
      // 第 2 场：对应第二章
      {
        id: 'sc_002',
        heading: {
          int_ext: 'INT',
          location_id: 'loc_cafe',
          time_of_day: '日',
          time_ref: 'tp_2',
        },
        source_ref: { chapter: 2, spans: [{ start: 0, end: CH2.length }] },
        characters: ['char_lin', 'char_zhou'],
        synopsis: '周屿端上她当年习惯的无糖拿铁，雨落旧城，两人各怀心事。',
        elements: [
          {
            type: 'action',
            text: '周屿端来一杯无糖拿铁——正是她当年的习惯。林晚指尖触到温热的杯壁。窗外，雨打青石板。',
            source_ref: {
              chapter: 2,
              spans: [span(CH2, '周屿给她端来一杯没有加糖的拿铁，正是她当年的习惯。'), span(CH2, '窗外开始下雨')],
            },
            adaptation: null,
          },
          {
            type: 'dialogue',
            character: 'char_zhou',
            line: '还以为你不会回来了。',
            parenthetical: null,
            source_ref: {
              chapter: 2,
              spans: [span(CH2, '他终于开口：还以为你不会回来了。')],
            },
            adaptation: null,
          },
          {
            // 内心戏外化：原文"心里却翻江倒海" -> 用画面/表情外化
            type: 'action',
            text: '林晚只是笑了笑，可她端杯的手微微发颤，茶面荡开一圈圈涟漪。',
            source_ref: {
              chapter: 2,
              spans: [span(CH2, '她笑了笑，没有回答，心里却翻江倒海。')],
            },
            adaptation: { from_: 'interior_monologue', technique: 'visual' },
          },
        ],
        continuity_flags: [
          {
            level: 'info',
            msg: '林晚的"无糖"习惯与第1场克制性格一致，无冲突。',
            scene_ids: ['sc_001', 'sc_002'],
          },
        ],
      },
      // 第 3 场：对应第三章
      {
        id: 'sc_003',
        heading: {
          int_ext: 'EXT',
          location_id: 'loc_door',
          time_of_day: '日',
          time_ref: 'tp_3',
        },
        source_ref: { chapter: 3, spans: [{ start: 0, end: CH3.length }] },
        characters: ['char_lin', 'char_zhou'],
        synopsis: '雨停，林晚告辞，周屿欲言又止，铜铃再响，落叶落肩。',
        elements: [
          {
            type: 'action',
            text: '雨停。林晚起身告辞，周屿送她到门口，欲言又止。铜铃又响一声。一片梧桐叶落在她肩上。',
            source_ref: {
              chapter: 3,
              spans: [span(CH3, '雨停的时候，林晚起身告辞。'), span(CH3, '街角的梧桐落下一片叶子，恰好落在她的肩上。')],
            },
            adaptation: null,
          },
          {
            type: 'transition',
            text: 'FADE OUT.',
          },
        ],
        continuity_flags: [],
      },
    ],
  }

  return { screenplay, chapters }
}

// 导出内容的 mock 生成（真实由后端 export.py 产出）
function mockExportContent(format: ExportFormat, sp: Screenplay): string {
  if (format === 'fountain') {
    const lines: string[] = []
    lines.push('Title: ' + sp.meta.title)
    lines.push('')
    for (const sc of sp.scenes) {
      const loc = sp.story_bible.locations.find((l) => l.id === sc.heading.location_id)
      lines.push(`${sc.heading.int_ext}. ${loc ? loc.name : sc.heading.location_id} - ${sc.heading.time_of_day}`)
      lines.push('')
      for (const el of sc.elements) {
        if (el.type === 'action') {
          lines.push(el.text)
        } else if (el.type === 'dialogue') {
          const ch = sp.story_bible.characters.find((c) => c.id === el.character)
          lines.push((ch ? ch.name : el.character).toUpperCase())
          if (el.parenthetical) lines.push('(' + el.parenthetical + ')')
          lines.push(el.line)
        } else {
          lines.push('> ' + el.text)
        }
        lines.push('')
      }
    }
    return lines.join('\n')
  }
  if (format === 'pdf') {
    // 占位：真 PDF 由后端生成。这里返回提示文本，避免前端伪造二进制。
    return '[PDF 导出由后端 export.py 生成，此处为 mock 占位]\n\n' + sp.meta.title
  }
  // 默认 yaml：简化序列化（真 YAML 由后端 to_yaml 产出，含 from 别名）
  const y: string[] = []
  y.push('meta:')
  y.push('  title: ' + sp.meta.title)
  y.push('  target_medium: ' + sp.meta.target_medium)
  y.push('  schema_version: ' + sp.meta.schema_version)
  y.push('scenes:')
  for (const sc of sp.scenes) {
    y.push('  - id: ' + sc.id)
    y.push('    synopsis: ' + sc.synopsis)
    y.push('    elements:')
    for (const el of sc.elements) {
      y.push('      - type: ' + el.type)
      if (el.type === 'action') y.push('        text: ' + el.text)
      if (el.type === 'dialogue') {
        y.push('        character: ' + el.character)
        y.push('        line: ' + el.line)
      }
      if (el.type === 'transition') y.push('        text: ' + el.text)
      if ((el.type === 'action' || el.type === 'dialogue') && el.adaptation) {
        y.push('        adaptation:')
        y.push('          from: ' + el.adaptation.from_)
        y.push('          technique: ' + el.adaptation.technique)
      }
    }
  }
  return y.join('\n')
}
