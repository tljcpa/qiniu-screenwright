// 右侧：结构化剧本面板（按场展示，每场有序 elements）
// 职责：渲染场标题 + 元素序列；adaptation 元素显示外化标签(创新点③)；
//       点击元素 -> 触发 右->左 高亮(把该 element key 交给父组件)；
//       当某 element key 在 activeKeys 内时高亮自身(实现 左->右 命中)。
import { useEffect, useRef, useState, type MutableRefObject } from 'react'
import type { Screenplay, Element, Adaptation, Scene } from '../api'
import { elementKey } from '../trace'

interface Props {
  screenplay: Screenplay
  activeKeys: Set<string>
  // 点击右侧元素
  onElementClick: (key: string) => void
  // 重生成某场：传该场 id + 用户填写的指令(可空)，返回 Promise 以便卡片管理 loading/错误。
  onRegenerate: (sceneId: string, instruction: string) => Promise<void>
  // H：sceneId -> 上一版该场。某场存在上一版时，卡片提供"看上一版/对比"切换。
  prevScenes: Record<string, Scene>
}

// 人物 id -> 显示名
function nameOf(sp: Screenplay, charId: string): string {
  const c = sp.story_bible.characters.find((x) => x.id === charId)
  if (c) return c.name
  return charId
}

// 地点 id -> 显示名
function locOf(sp: Screenplay, locId: string): string {
  const l = sp.story_bible.locations.find((x) => x.id === locId)
  if (l) return l.name
  return locId
}

// 外化标签文案：把 from/technique 翻成人话。
// 入参直接收整个 adaptation 对象，内部读 "from"(真实 API 键，from 是 JS 保留字故用解构改名)，
// from_ 仅兜底历史 mock。任一关键字段缺失就返回 null，渲染层据此不渲染标签，绝不出现 undefined。
function adaptLabel(adaptation: Adaptation): string | null {
  // from 是保留字，不能直接当变量名，解构时改名成 fromKind；再用 from_ 兜底。
  const { from: fromKind, from_, technique } = adaptation
  const sourceKind = fromKind ?? from_
  // 源类型或技法任一缺失，则不渲染该标签(容错，避免 undefined)。
  if (!sourceKind || !technique) {
    return null
  }
  const fromMap: Record<string, string> = {
    interior_monologue: '内心戏',
    narration: '旁白',
    description: '描写',
  }
  const techMap: Record<string, string> = {
    subtext: '潜台词',
    action: '动作',
    voiceover: '画外音',
    visual: '画面',
  }
  const f = fromMap[sourceKind] ?? sourceKind
  const t = techMap[technique] ?? technique
  return f + ' → ' + t
}

export default function ScriptPane(props: Props) {
  const { screenplay, activeKeys, onElementClick, onRegenerate, prevScenes } = props
  const firstHlRef = useRef<HTMLDivElement | null>(null)
  // 用一个外层闭包变量记录"本轮渲染是否已分配过首个高亮 ref"，
  // 通过对象包裹传给各 SceneView，使跨场也只滚动到第一个命中元素。
  const assigned = { done: false }

  useEffect(() => {
    if (firstHlRef.current) {
      firstHlRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, [activeKeys])

  return (
    <div className="pane right">
      <h2 className="pane-title">结构化剧本</h2>
      {screenplay.scenes.map((sc) => (
        <SceneView
          key={sc.id}
          scene={sc}
          screenplay={screenplay}
          activeKeys={activeKeys}
          onElementClick={onElementClick}
          onRegenerate={onRegenerate}
          firstHlRef={firstHlRef}
          assigned={assigned}
          prevScene={prevScenes[sc.id]}
        />
      ))}
    </div>
  )
}

// 单场卡片：自带"重生成"交互状态(展开指令框 / loading / 错误)。
// 把 regen UI 状态收在每张卡片内部，是为了实现"只动这一场"的隔离感——
// 一张卡片重生成中/出错，绝不影响其他卡片。
interface SceneProps {
  scene: Screenplay['scenes'][number]
  screenplay: Screenplay
  activeKeys: Set<string>
  onElementClick: (key: string) => void
  onRegenerate: (sceneId: string, instruction: string) => Promise<void>
  firstHlRef: MutableRefObject<HTMLDivElement | null>
  assigned: { done: boolean }
  // H：该场的上一版(重生成前的内容)，存在则提供对比切换。
  prevScene?: Scene
}

function SceneView(p: SceneProps) {
  const { scene: sc, screenplay, activeKeys, onElementClick, onRegenerate, firstHlRef, assigned, prevScene } = p
  // 是否展开指令输入框
  const [open, setOpen] = useState(false)
  // 指令文本(可选填)
  const [instruction, setInstruction] = useState('')
  // 本场是否正在重生成
  const [busy, setBusy] = useState(false)
  // 本场错误信息(为 null 表示无错)
  const [err, setErr] = useState<string | null>(null)
  // H：是否正在查看上一版(默认看当前版)。
  const [showPrev, setShowPrev] = useState(false)

  // 点确认：调父级 onRegenerate，期间本卡片进入 loading；成功收起输入框，失败保留并显示错误可重试。
  async function submit() {
    setBusy(true)
    setErr(null)
    try {
      await onRegenerate(sc.id, instruction.trim())
      // 成功后收起输入框、清空指令(新内容已由父级替换进 state)。
      setOpen(false)
      setInstruction('')
    } catch (e) {
      // 出错只在本卡片内展示，可重试，不污染全局。
      let msg = '重生成失败，请重试'
      if (e instanceof Error && e.message) {
        msg = e.message
      }
      setErr(msg)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="scene" key={sc.id} data-scene-id={sc.id}>
      <div className="scene-head">
        <span className="scene-slug">
          {sc.heading.int_ext}. {locOf(screenplay, sc.heading.location_id)} — {sc.heading.time_of_day}
        </span>
        <span className="scene-id">{sc.id}</span>
        {/* H：存在上一版时提供"看上一版/看当前版"切换 */}
        {prevScene ? (
          <button
            className={'btn scene-diff' + (showPrev ? ' active' : '')}
            onClick={() => setShowPrev((v) => !v)}
            title="对比重生成前后的内容"
          >
            {showPrev ? '看当前版' : '看上一版'}
          </button>
        ) : null}
        <button
          className="btn scene-regen"
          onClick={() => setOpen((v) => !v)}
          disabled={busy}
          title="增量重生成这一场（编辑安全）"
        >
          重生成
        </button>
      </div>
      {sc.synopsis ? <div className="scene-synopsis">{sc.synopsis}</div> : null}

      {/* 重生成指令面板：点"重生成"展开。指令可空。 */}
      {open ? (
        <div className="regen-panel">
          <input
            className="regen-input"
            type="text"
            value={instruction}
            placeholder="例如: 更紧张、加冲突、台词更口语（可不填）"
            disabled={busy}
            onChange={(e) => setInstruction(e.target.value)}
            onKeyDown={(e) => {
              // 回车直接提交，体验更顺。
              if (e.key === 'Enter' && !busy) {
                submit()
              }
            }}
          />
          <button className="btn primary regen-confirm" onClick={submit} disabled={busy}>
            {busy ? '重生成中…' : '确认重生成'}
          </button>
          {err ? (
            <div className="regen-error">
              {err}
              <button className="btn regen-retry" onClick={submit} disabled={busy}>
                重试
              </button>
            </div>
          ) : null}
        </div>
      ) : null}

      {/* H：查看上一版时，只读渲染重生成前的内容(不参与溯源/高亮/点击) */}
      {showPrev && prevScene ? (
        <div className="prev-version">
          <div className="prev-banner">以下为重生成前的上一版（只读）</div>
          {prevScene.elements.map((el, idx) => (
            <ElementView
              key={'prev-' + idx}
              el={el}
              hit={false}
              screenplay={screenplay}
              onClick={() => {}}
            />
          ))}
        </div>
      ) : (
        sc.elements.map((el, idx) => {
          const key = elementKey(sc.id, idx)
          const hit = activeKeys.has(key)
          let ref: ((node: HTMLDivElement | null) => void) | undefined
          if (hit && !assigned.done) {
            assigned.done = true
            ref = (node) => {
              firstHlRef.current = node
            }
          }
          return (
            <ElementView
              key={key}
              el={el}
              hit={hit}
              screenplay={screenplay}
              onClick={() => onElementClick(key)}
              rootRef={ref}
            />
          )
        })
      )}

      {sc.continuity_flags.map((f, i) => (
        <div className={'flag ' + f.level} key={i}>
          {f.msg}
        </div>
      ))}
    </div>
  )
}

// 单个元素渲染
interface ElProps {
  el: Element
  hit: boolean
  screenplay: Screenplay
  onClick: () => void
  rootRef?: (node: HTMLDivElement | null) => void
}

function ElementView(p: ElProps) {
  const { el, hit, screenplay, onClick, rootRef } = p
  const cls = 'el ' + (hit ? 'hl ' : '')

  if (el.type === 'transition') {
    // 转场没有 source_ref，不参与溯源，但仍渲染
    return (
      <div className="el el-transition" ref={rootRef}>
        {el.text}
      </div>
    )
  }

  if (el.type === 'action') {
    // 先算出标签文案，为 null(字段缺失)时整段标签不渲染，杜绝 undefined。
    const actionTag = el.adaptation ? adaptLabel(el.adaptation) : null
    return (
      <div className={cls + 'el-action'} onClick={onClick} ref={rootRef}>
        {el.text}
        {actionTag ? <span className="adapt-tag">{actionTag}</span> : null}
      </div>
    )
  }

  // dialogue
  const dialogueTag = el.adaptation ? adaptLabel(el.adaptation) : null
  return (
    <div className={cls + 'el-dialogue'} onClick={onClick} ref={rootRef}>
      <div className="char">
        {nameOf(screenplay, el.character)}
        {dialogueTag ? <span className="adapt-tag">{dialogueTag}</span> : null}
      </div>
      {el.parenthetical ? <div className="paren">（{el.parenthetical}）</div> : null}
      <div className="line">{el.line}</div>
    </div>
  )
}
