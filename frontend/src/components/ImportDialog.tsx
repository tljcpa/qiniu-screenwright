// ============================================================================
// 导入剧本续编(闭合"导出 -> 手改 -> 导回续编"可编辑环)
// ----------------------------------------------------------------------------
// 职责：模态浮层，让用户粘贴或上传 .yaml/.json 剧本文本，可选附原著原文，
//       提交后调 /api/import，把返回结果载入工作台(与 convert 结果同样渲染)。
//   - 处理 400 等错误：把后端 detail 友好展示在浮层内，可改后重试，不污染全局。
// 视觉沿用纸感墨色风格，复用 .btn 等既有类。
// ============================================================================
import { useState, useRef } from 'react'
import { importScreenplay, type WorkbenchData } from '../api'

interface Props {
  // 导入成功：把工作台数据交回父组件载入
  onImported: (data: WorkbenchData) => void
  // 关闭浮层
  onClose: () => void
}

export default function ImportDialog(props: Props) {
  const { onImported, onClose } = props
  // 剧本文本(YAML/JSON)
  const [content, setContent] = useState('')
  // 可选原著原文(用于溯源重建)
  const [text, setText] = useState('')
  // 提交中
  const [busy, setBusy] = useState(false)
  // 错误信息(null 表示无错)
  const [err, setErr] = useState<string | null>(null)
  const scriptFileRef = useRef<HTMLInputElement | null>(null)
  const novelFileRef = useRef<HTMLInputElement | null>(null)

  // 读取上传的剧本文件(.yaml/.yml/.json)到剧本文本框
  function onScriptFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0]
    if (!f) {
      return
    }
    const reader = new FileReader()
    reader.onload = () => {
      setContent(String(reader.result ?? ''))
    }
    reader.readAsText(f)
  }

  // 读取上传的原著原文(.txt)到原文框
  function onNovelFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0]
    if (!f) {
      return
    }
    const reader = new FileReader()
    reader.onload = () => {
      setText(String(reader.result ?? ''))
    }
    reader.readAsText(f)
  }

  // 提交导入：成功则回调载入并关闭；失败把错误留在浮层内可重试。
  async function submit() {
    if (content.trim().length === 0) {
      setErr('请先粘贴或上传剧本文本(YAML/JSON)')
      return
    }
    setBusy(true)
    setErr(null)
    try {
      // 原文可选：留空时不回传(传 undefined)，溯源由后端尽力重建。
      let novel: string | undefined
      if (text.trim().length > 0) {
        novel = text
      } else {
        novel = undefined
      }
      const data = await importScreenplay(content, novel)
      onImported(data)
    } catch (e) {
      let msg = '导入失败，请检查剧本格式后重试'
      if (e instanceof Error && e.message) {
        msg = e.message
      }
      setErr(msg)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal import-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>导入剧本续编</h3>
          <button className="btn modal-close" onClick={onClose} disabled={busy} title="关闭">
            关闭
          </button>
        </div>

        <p className="import-hint">
          粘贴或上传由本工具导出的剧本(YAML/JSON)，可选附上原著原文以重建溯源，导入后即可继续编辑。
        </p>

        <label className="import-label">剧本文本(YAML / JSON，必填)</label>
        <textarea
          className="import-area"
          placeholder="在此粘贴剧本 YAML 或 JSON……"
          value={content}
          onChange={(e) => setContent(e.target.value)}
          disabled={busy}
        />
        <div className="import-row">
          <button
            className="btn"
            onClick={() => scriptFileRef.current?.click()}
            disabled={busy}
          >
            上传剧本文件
          </button>
          <input
            ref={scriptFileRef}
            type="file"
            accept=".yaml,.yml,.json,application/json,text/yaml"
            style={{ display: 'none' }}
            onChange={onScriptFile}
          />
        </div>

        <label className="import-label">原著原文(可选，用于溯源)</label>
        <textarea
          className="import-area import-area-sm"
          placeholder="可选：粘贴原著小说原文……"
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={busy}
        />
        <div className="import-row">
          <button
            className="btn"
            onClick={() => novelFileRef.current?.click()}
            disabled={busy}
          >
            上传原文文件
          </button>
          <input
            ref={novelFileRef}
            type="file"
            accept=".txt,text/plain"
            style={{ display: 'none' }}
            onChange={onNovelFile}
          />
        </div>

        {err ? <div className="import-error">{err}</div> : null}

        <div className="modal-foot">
          <button className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button
            className="btn primary"
            onClick={submit}
            disabled={busy || content.trim().length === 0}
          >
            {busy ? '导入中…' : '导入到工作台'}
          </button>
        </div>
      </div>
    </div>
  )
}
