// 右侧：结构化剧本面板（按场展示，每场有序 elements）
// 职责：渲染场标题 + 元素序列；adaptation 元素显示外化标签(创新点③)；
//       点击元素 -> 触发 右->左 高亮(把该 element key 交给父组件)；
//       当某 element key 在 activeKeys 内时高亮自身(实现 左->右 命中)。
import { useEffect, useRef } from 'react'
import type { Screenplay, Element, Adaptation } from '../api'
import { elementKey } from '../trace'

interface Props {
  screenplay: Screenplay
  activeKeys: Set<string>
  // 点击右侧元素
  onElementClick: (key: string) => void
  // 重生成某场
  onRegenerate: (sceneId: string) => void
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
  const { screenplay, activeKeys, onElementClick, onRegenerate } = props
  const firstHlRef = useRef<HTMLDivElement | null>(null)
  let assignedFirst = false

  useEffect(() => {
    if (firstHlRef.current) {
      firstHlRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, [activeKeys])

  return (
    <div className="pane right">
      <h2 className="pane-title">结构化剧本</h2>
      {screenplay.scenes.map((sc) => (
        <div className="scene" key={sc.id}>
          <div className="scene-head">
            <span className="scene-slug">
              {sc.heading.int_ext}. {locOf(screenplay, sc.heading.location_id)} — {sc.heading.time_of_day}
            </span>
            <span className="scene-id">{sc.id}</span>
            <button
              className="btn scene-regen"
              onClick={() => onRegenerate(sc.id)}
              title="增量重生成这一场（编辑安全）"
            >
              重生成
            </button>
          </div>
          {sc.synopsis ? <div className="scene-synopsis">{sc.synopsis}</div> : null}

          {sc.elements.map((el, idx) => {
            const key = elementKey(sc.id, idx)
            const hit = activeKeys.has(key)
            let ref: ((el: HTMLDivElement | null) => void) | undefined
            if (hit && !assignedFirst) {
              assignedFirst = true
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
          })}

          {sc.continuity_flags.map((f, i) => (
            <div className={'flag ' + f.level} key={i}>
              {f.msg}
            </div>
          ))}
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
