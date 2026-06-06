// 右侧侧栏：质量看板 + 故事圣经(人物/地点/时间线)
// 创新点：故事圣经"可编辑"——人物名/性格/别名支持就地编辑，编辑结果通过
//   onUpdateBible 回写内存中的 screenplay.story_bible，从而导出 YAML 反映修改。
//   这是纯前端编辑(不触后端、不重生成)，体现"结构化可编辑"。
import { useState } from 'react'
import type { Screenplay, QualityMetrics, StoryBible, Character } from '../api'

interface Props {
  screenplay: Screenplay
  metrics: QualityMetrics
  // 故事圣经更新回调：传入新的 story_bible，由父组件写回 screenplay。
  onUpdateBible: (bible: StoryBible) => void
}

export default function SideBar(props: Props) {
  const { screenplay, metrics, onUpdateBible } = props
  const bible = screenplay.story_bible
  // 溯源覆盖率转百分比展示
  const coveragePct = Math.round(metrics.trace_coverage * 100)

  // 用一个不可变更新工具：替换某 id 的人物，返回新的 bible(其余引用不变)。
  function updateCharacter(charId: string, patch: Partial<Character>) {
    const nextChars = bible.characters.map((c) => {
      if (c.id === charId) {
        return { ...c, ...patch }
      }
      return c
    })
    onUpdateBible({ ...bible, characters: nextChars })
  }

  return (
    <aside className="aside">
      {/* 质量看板 */}
      <div className="panel">
        <h4>质量看板</h4>
        <div className="metrics">
          <div className="metric">
            <div className="v">{metrics.scene_count}</div>
            <div className="k">场数</div>
          </div>
          <div className="metric">
            <div className="v">{metrics.dialogue_count}</div>
            <div className="k">对白数</div>
          </div>
          <div className="metric">
            <div className="v">{metrics.externalization_count}</div>
            <div className="k">内心戏外化</div>
          </div>
          <div className="metric">
            <div className="v">{coveragePct}%</div>
            <div className="k">溯源覆盖率</div>
          </div>
          <div className="metric">
            <div className="v">{metrics.continuity_conflicts}</div>
            <div className="k">连贯性冲突</div>
          </div>
        </div>
      </div>

      {/* 故事圣经 - 人物(可就地编辑) */}
      <div className="panel">
        <h4>人物</h4>
        {bible.characters.map((c) => (
          <CharacterCard key={c.id} character={c} onChange={(patch) => updateCharacter(c.id, patch)} />
        ))}
      </div>

      {/* 故事圣经 - 地点 */}
      <div className="panel">
        <h4>地点</h4>
        <ul className="lst">
          {bible.locations.map((l) => (
            <li key={l.id}>{l.name}</li>
          ))}
        </ul>
      </div>

      {/* 故事圣经 - 时间线 */}
      <div className="panel">
        <h4>时间线</h4>
        <ul className="lst">
          {bible.timeline
            .slice()
            .sort((a, b) => a.order - b.order)
            .map((t) => (
              <li key={t.id}>
                <span className="ord">{t.order}.</span>
                {t.label}
              </li>
            ))}
        </ul>
      </div>
    </aside>
  )
}

// 单张人物卡：默认只读展示；点"编辑"切到编辑态，可改 名/别名/性格。
// 别名与性格在编辑态下用顿号(、)分隔的输入，提交时拆回数组。
interface CharCardProps {
  character: Character
  // 把改动以 patch 形式上抛(只含改了的字段)
  onChange: (patch: Partial<Character>) => void
}

function CharacterCard(props: CharCardProps) {
  const { character: c, onChange } = props
  // 是否处于编辑态
  const [editing, setEditing] = useState(false)
  // 编辑态的草稿值(独立于父 state，确认时才上抛)
  const [name, setName] = useState(c.name)
  const [aliasText, setAliasText] = useState(c.aliases.join('、'))
  const [traitText, setTraitText] = useState(c.traits.join('、'))

  // 进入编辑：用当前值初始化草稿。
  function startEdit() {
    setName(c.name)
    setAliasText(c.aliases.join('、'))
    setTraitText(c.traits.join('、'))
    setEditing(true)
  }

  // 把"、/，/,"分隔的字符串拆成去空、去重后的数组。
  function splitList(s: string): string[] {
    const parts = s.split(/[、,，]/)
    const out: string[] = []
    for (const p of parts) {
      const v = p.trim()
      if (v.length > 0 && !out.includes(v)) {
        out.push(v)
      }
    }
    return out
  }

  // 确认保存：组装 patch 上抛，退出编辑态。名称为空时回退为原名，避免空名。
  function save() {
    let nextName = name.trim()
    if (nextName.length === 0) {
      nextName = c.name
    }
    onChange({
      name: nextName,
      aliases: splitList(aliasText),
      traits: splitList(traitText),
    })
    setEditing(false)
  }

  // 取消：丢弃草稿，退出编辑态。
  function cancel() {
    setEditing(false)
  }

  if (editing) {
    return (
      <div className="char-card editing">
        <input
          className="bible-input"
          value={name}
          placeholder="人物名"
          onChange={(e) => setName(e.target.value)}
        />
        <input
          className="bible-input"
          value={aliasText}
          placeholder="别名(顿号分隔)"
          onChange={(e) => setAliasText(e.target.value)}
        />
        <input
          className="bible-input"
          value={traitText}
          placeholder="性格(顿号分隔)"
          onChange={(e) => setTraitText(e.target.value)}
        />
        <div className="bible-edit-actions">
          <button className="btn primary bible-btn" onClick={save}>
            保存
          </button>
          <button className="btn bible-btn" onClick={cancel}>
            取消
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="char-card">
      <span className="nm">{c.name}</span>
      {c.aliases.length > 0 ? <span className="alias">别名 {c.aliases.join('、')}</span> : null}
      <button className="bible-edit" onClick={startEdit} title="编辑该人物">
        编辑
      </button>
      <div className="traits">
        {c.traits.map((t, i) => (
          <span className="chip" key={i}>
            {t}
          </span>
        ))}
      </div>
    </div>
  )
}
