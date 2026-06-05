// ============================================================================
// App：编剧工作台主组件，编排顶部栏 + 分屏工作台 + 侧栏 + 转换入口。
// 核心状态：
//   - data: 当前工作台数据(剧本+原文)，为 null 时显示转换入口空态。
//   - medium: 目标媒介(创新点①)，切换会按媒介重新 convert/getSample。
//   - activeKeys: 当前高亮的 element key 集合，是双向溯源(创新点②)的单一事实源。
//       右->左：点右侧元素 => activeKeys = {该 key} => 左侧命中分片高亮。
//       左->右：点左侧分片 => activeKeys = 该分片的 owners => 右侧对应元素高亮。
//     两个方向写同一个 activeKeys，左右两个面板都只读它，因此天然双向。
// ============================================================================
import { useMemo, useState } from 'react'
import {
  convert,
  getSample,
  exportAs,
  regenerateScene,
  computeMetrics,
  type WorkbenchData,
  type TargetMedium,
  type ExportFormat,
  type ConvertProgress,
} from './api'
import { buildChapterSegments } from './trace'
import NovelPane from './components/NovelPane'
import ScriptPane from './components/ScriptPane'
import SideBar from './components/SideBar'
import Intake from './components/Intake'

// 媒介选项与中文标签
const MEDIA: { id: TargetMedium; label: string }[] = [
  { id: 'film', label: '电影' },
  { id: 'series', label: '剧集' },
  { id: 'short_drama', label: '短剧' },
]

// 后端 stage 标识 -> 中文展示标签(对应后端 Pass0-5)。
// 真实进度由后端 SSE 的 stage 事件驱动，这张表只负责把英文 stage 翻成中文。
const STAGE_LABELS: Record<string, string> = {
  ingest: '分章分块（Pass0 ingest）',
  bible: '构建故事圣经（Pass1 bible）',
  segment: '场景切分与溯源（Pass2 segment）',
  generate: '逐场生成与外化（Pass3 generate）',
  annotate: '校验与连贯性检查（Pass4 annotate）',
  metrics: '计算质量指标（Pass5 metrics）',
  done: '完成',
}

export default function App() {
  const [data, setData] = useState<WorkbenchData | null>(null)
  // 缓存最近一次提交的小说原文：切媒介/重生成需回传后端，保证真实重渲染与精确溯源。
  const [sourceText, setSourceText] = useState<string>('')
  const [medium, setMedium] = useState<TargetMedium>('film')
  const [activeKeys, setActiveKeys] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [progressStep, setProgressStep] = useState<string | null>(null)
  const [progressPct, setProgressPct] = useState(0)

  // 由 source_ref 投影出的各章分片(只在 data 变化时重算)
  const segmentsByChapter = useMemo(() => {
    if (!data) return new Map()
    return buildChapterSegments(data)
  }, [data])

  // 质量看板指标
  const metrics = useMemo(() => {
    if (!data) {
      return {
        scene_count: 0,
        dialogue_count: 0,
        externalization_count: 0,
        trace_coverage: 0,
        continuity_conflicts: 0,
      }
    }
    return computeMetrics(data)
  }, [data])

  // 真实进度回调：把后端 SSE 的 stage 事件映射成进度条状态。
  function handleProgress(p: ConvertProgress) {
    const label = STAGE_LABELS[p.stage] || p.stage
    setProgressStep(label)
    if (typeof p.pct === 'number') {
      setProgressPct(p.pct)
    }
  }

  // 生成：调真实 convert(SSE)，进度由后端事件驱动。
  // 关键：必须缓存提交的原文，重生成/切媒介时回传后端以保证溯源精确。
  async function handleGenerate(text: string) {
    setBusy(true)
    setActiveKeys(new Set())
    setProgressStep(null)
    setProgressPct(0)
    setSourceText(text)
    const d = await convert(text, medium, handleProgress)
    setData(d)
    setProgressStep(null)
    setProgressPct(0)
    setBusy(false)
  }

  // 加载样例：走真实 /api/sample 取样本原文后 convert。
  async function handleLoadSample() {
    setBusy(true)
    setActiveKeys(new Set())
    setProgressStep(null)
    setProgressPct(0)
    const d = await getSample(medium, handleProgress)
    // 缓存样例原文(取自第一章拼接近似不可靠，这里直接用 chapters 拼回完整原文)。
    setSourceText(d.chapters.map((c) => c.text).join('\n\n'))
    setData(d)
    setProgressStep(null)
    setProgressPct(0)
    setBusy(false)
  }

  // 切换媒介：若已有数据，用缓存的原文按新媒介重新 convert(真实重渲染，体现①)。
  async function handleSwitchMedium(m: TargetMedium) {
    setMedium(m)
    if (data && sourceText) {
      setBusy(true)
      setActiveKeys(new Set())
      setProgressStep(null)
      setProgressPct(0)
      const d = await convert(sourceText, m, handleProgress)
      setData(d)
      setProgressStep(null)
      setProgressPct(0)
      setBusy(false)
    }
  }

  // 增量重生成某一场（编辑安全：只动这一场）。
  // 关键：拿到后端返回的单个新 Scene 后，只替换 state.screenplay.scenes 里同 id 的那一场，
  //       其他场对象引用原样不变(编辑安全)，并触发 segmentsByChapter/metrics 重算以刷新溯源数据。
  // 失败时把错误抛回 ScriptPane，由该卡片自行展示并允许重试，绝不污染全局 data。
  async function handleRegenerate(sceneId: string, instruction: string): Promise<void> {
    if (!data) {
      return
    }
    // 用当前完整 screenplay 调后端；medium 与缓存的 sourceText 一并回传，溯源更准。
    const newScene = await regenerateScene(
      data.screenplay,
      sceneId,
      instruction,
      medium,
      sourceText,
    )
    // 函数式更新：基于最新 state 重建，避免闭包里 data 过期。
    setData((prev) => {
      if (!prev) {
        return prev
      }
      // 只替换目标 id 的那一场，其余场原样保留(引用不变)。
      const nextScenes = prev.screenplay.scenes.map((s) => {
        if (s.id === sceneId) {
          return newScene
        }
        return s
      })
      return {
        ...prev,
        screenplay: { ...prev.screenplay, scenes: nextScenes },
      }
    })
    // 重生成后旧的高亮 key 可能失效，清掉，避免指向已被替换的元素。
    setActiveKeys(new Set())
  }

  // 右侧点击元素 -> 高亮该元素，左侧联动
  function handleElementClick(key: string) {
    setActiveKeys(new Set([key]))
  }

  // 左侧点击分片 -> 高亮该分片关联的所有元素，右侧联动
  function handleSegmentClick(ownerKeys: string[]) {
    setActiveKeys(new Set(ownerKeys))
  }

  // 导出：调 exportAs 拿 Blob 并触发浏览器下载
  async function handleExport(format: ExportFormat) {
    if (!data) return
    const blob = await exportAs(format, data.screenplay)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const ext = format === 'fountain' ? 'fountain' : format
    a.download = data.screenplay.meta.title + '.' + ext
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  return (
    <div className="app">
      {/* 顶部栏 */}
      <header className="topbar">
        <div className="brand">
          <span className="mark">Screen</span>wright
          <span className="sub">编剧工作台</span>
        </div>

        {/* 媒介切换器(创新点①) */}
        <div className="medium-switch">
          {MEDIA.map((m) => (
            <button
              key={m.id}
              className={m.id === medium ? 'active' : ''}
              onClick={() => handleSwitchMedium(m.id)}
              disabled={busy}
            >
              {m.label}
            </button>
          ))}
        </div>

        <div className="spacer" />

        {/* 导出 */}
        <div className="btn-group">
          <button className="btn" onClick={() => handleExport('yaml')} disabled={!data}>
            导出 YAML
          </button>
          <button className="btn" onClick={() => handleExport('fountain')} disabled={!data}>
            Fountain
          </button>
          <button className="btn" onClick={() => handleExport('pdf')} disabled={!data}>
            PDF
          </button>
        </div>

        <button className="btn primary" onClick={handleLoadSample} disabled={busy}>
          加载样例
        </button>
      </header>

      {/* 主体 */}
      <div className="main">
        {data ? (
          <>
            <div className="split">
              <NovelPane
                chapters={data.chapters}
                segmentsByChapter={segmentsByChapter}
                activeKeys={activeKeys}
                onSegmentClick={handleSegmentClick}
              />
              <ScriptPane
                screenplay={data.screenplay}
                activeKeys={activeKeys}
                onElementClick={handleElementClick}
                onRegenerate={handleRegenerate}
              />
            </div>
            <SideBar screenplay={data.screenplay} metrics={metrics} />
          </>
        ) : (
          <Intake
            onGenerate={handleGenerate}
            onLoadSample={handleLoadSample}
            busy={busy}
            progressStep={progressStep}
            progressPct={progressPct}
          />
        )}
      </div>
    </div>
  )
}
