// 转换流程入口（空态）：粘贴/上传小说 -> 点"生成" -> 进度区(mock 进度)
import { useState, useRef } from 'react'

interface Props {
  // 提交生成：把原文文本交给父组件去调 convert
  onGenerate: (text: string) => void
  // 加载样例
  onLoadSample: () => void
  // 当前是否在生成中(父组件控制)
  busy: boolean
  // mock 进度步骤文案(父组件推进)，为空表示无进度
  progressStep: string | null
  // 进度百分比 0..100
  progressPct: number
}

export default function Intake(props: Props) {
  const { onGenerate, onLoadSample, busy, progressStep, progressPct } = props
  const [text, setText] = useState('')
  const fileRef = useRef<HTMLInputElement | null>(null)

  // 读取上传的 txt 文件到文本框
  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0]
    if (!f) return
    const reader = new FileReader()
    reader.onload = () => {
      setText(String(reader.result ?? ''))
    }
    reader.readAsText(f)
  }

  return (
    <div className="intake">
      <h2>把小说，写成剧本。</h2>
      <p>粘贴或上传至少三章小说原文，选择目标媒介，生成可溯源的结构化剧本。</p>
      <textarea
        placeholder="在此粘贴小说原文……"
        value={text}
        onChange={(e) => setText(e.target.value)}
        disabled={busy}
      />
      <div className="intake-actions">
        <button
          className="btn primary"
          onClick={() => onGenerate(text)}
          disabled={busy || text.trim().length === 0}
        >
          生成剧本
        </button>
        <button className="btn" onClick={() => fileRef.current?.click()} disabled={busy}>
          上传文件
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".txt,text/plain"
          style={{ display: 'none' }}
          onChange={onFile}
        />
        <button className="btn" onClick={onLoadSample} disabled={busy}>
          加载样例
        </button>
      </div>

      {progressStep ? (
        <div className="progress">
          <div className="bar">
            <i style={{ width: progressPct + '%' }} />
          </div>
          <div className="step">{progressStep}</div>
        </div>
      ) : null}
    </div>
  )
}
