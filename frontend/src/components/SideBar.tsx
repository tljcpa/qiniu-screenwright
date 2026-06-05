// 右侧侧栏：质量看板 + 故事圣经(人物/地点/时间线)
import type { Screenplay, QualityMetrics } from '../api'

interface Props {
  screenplay: Screenplay
  metrics: QualityMetrics
}

export default function SideBar(props: Props) {
  const { screenplay, metrics } = props
  const bible = screenplay.story_bible
  // 溯源覆盖率转百分比展示
  const coveragePct = Math.round(metrics.trace_coverage * 100)

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

      {/* 故事圣经 - 人物 */}
      <div className="panel">
        <h4>人物</h4>
        {bible.characters.map((c) => (
          <div className="char-card" key={c.id}>
            <span className="nm">{c.name}</span>
            {c.aliases.length > 0 ? (
              <span className="alias">别名 {c.aliases.join('、')}</span>
            ) : null}
            <div className="traits">
              {c.traits.map((t, i) => (
                <span className="chip" key={i}>
                  {t}
                </span>
              ))}
            </div>
          </div>
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
