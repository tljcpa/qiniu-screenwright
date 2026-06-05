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
  computeMetrics,
  type WorkbenchData,
  type TargetMedium,
  type ExportFormat,
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

// mock 进度步骤(对应后端 Pass0-5)，演示转换流程
const PROGRESS_STEPS = [
  '分章分块（Pass0 ingest）',
  '构建故事圣经（Pass1 bible）',
  '场景切分与溯源（Pass2 segment）',
  '逐场生成与外化（Pass3 generate）',
  '校验与连贯性检查（Pass4 validate）',
  '完成',
]

export default function App() {
  const [data, setData] = useState<WorkbenchData | null>(null)
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

  // 播放一段 mock 进度动画，然后 resolve
  async function runProgress(): Promise<void> {
    for (let i = 0; i < PROGRESS_STEPS.length; i++) {
      setProgressStep(PROGRESS_STEPS[i])
      setProgressPct(Math.round(((i + 1) / PROGRESS_STEPS.length) * 100))
      await new Promise((r) => setTimeout(r, 350))
    }
  }

  // 生成：跑进度 + 调 convert
  async function handleGenerate(text: string) {
    setBusy(true)
    setActiveKeys(new Set())
    await runProgress()
    const d = await convert(text, medium)
    setData(d)
    setProgressStep(null)
    setProgressPct(0)
    setBusy(false)
  }

  // 加载样例
  async function handleLoadSample() {
    setBusy(true)
    setActiveKeys(new Set())
    const d = await getSample()
    // 样例也应反映当前选择的媒介，统一走 convert 以套用媒介渲染
    const dm = await convert('', medium)
    void d
    setData(dm)
    setBusy(false)
  }

  // 切换媒介：若已有数据，按新媒介重渲染(同一 bible+scenes 重渲染，体现①)
  async function handleSwitchMedium(m: TargetMedium) {
    setMedium(m)
    if (data) {
      setBusy(true)
      setActiveKeys(new Set())
      const d = await convert('', m)
      setData(d)
      setBusy(false)
    }
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
                onRegenerate={(id) => {
                  // 重生成本轮仅 mock，提示用户(真实接口已在 api.ts 留好)
                  // eslint-disable-next-line no-alert
                  window.alert('重生成（mock）：场 ' + id + ' 将按指令增量重生成。真实接口 /api/regenerate_scene 已就绪。')
                }}
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
