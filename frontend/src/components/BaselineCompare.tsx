// ============================================================================
// 朴素基线对比(创新点①路演杀手锏)
// ----------------------------------------------------------------------------
// 职责：以模态浮层并排展示"朴素版"(左)与"我们的结构化版"(右)，再用一张五维
//       对比表直观呈现差异(跨章一致性/可溯源/内心戏外化/结构化可编辑/连贯性检查)。
//   - 朴素版数据来自 /api/baseline 的 naive(自由文本场、无溯源/无外化/无圣经)。
//   - 我们的版数据来自当前工作台 screenplay + metrics。
//   - 五维布尔差异固定为"朴素 ✗ / 我们 ✓"，数值列用真实 metrics 增强冲击力。
// 视觉沿用纸感墨色风格，复用既有 .scene/.el 等类，不引入新主题色。
// ============================================================================
import type { BaselineResult, Screenplay, QualityMetrics } from '../api'

interface Props {
  // 朴素版结果(已请求成功)
  result: BaselineResult
  // 我们的结构化剧本(当前工作台)
  screenplay: Screenplay
  // 我们的质量指标
  metrics: QualityMetrics
  // 关闭浮层
  onClose: () => void
}

// 人物 id -> 显示名(我们的版渲染对白时用)
function nameOf(sp: Screenplay, charId: string): string {
  const c = sp.story_bible.characters.find((x) => x.id === charId)
  if (c) {
    return c.name
  }
  return charId
}

// 地点 id -> 显示名
function locOf(sp: Screenplay, locId: string): string {
  const l = sp.story_bible.locations.find((x) => x.id === locId)
  if (l) {
    return l.name
  }
  return locId
}

// 五维对比表的一行定义。
// naive/ours 用布尔表达"是否具备该能力"；ours 恒为 true、naive 恒为 false。
// detail 给我们这一侧补一句量化说明，体现真实数据。
interface DimRow {
  label: string
  naive: boolean
  ours: boolean
  detail: string
}

export default function BaselineCompare(props: Props) {
  const { result, screenplay, metrics, onClose } = props
  const naive = result.naive

  // 溯源覆盖率转百分比
  const coveragePct = Math.round(metrics.trace_coverage * 100)

  // 五维差异(布尔列固定，detail 用真实 metrics 增强说服力)
  const rows: DimRow[] = [
    {
      label: '跨章一致性(故事圣经)',
      naive: false,
      ours: true,
      detail: '统一人物/地点/时间线，' + screenplay.story_bible.characters.length + ' 个角色受约束',
    },
    {
      label: '双向可溯源',
      naive: false,
      ours: true,
      detail: '溯源覆盖率 ' + coveragePct + '%，可点击逐句回溯原文',
    },
    {
      label: '内心戏外化',
      naive: false,
      ours: true,
      detail: '已外化 ' + metrics.externalization_count + ' 处内心戏为动作/潜台词/画面',
    },
    {
      label: '结构化可编辑',
      naive: false,
      ours: true,
      detail: '有序元素 + 单场重生成 + 圣经就地编辑，导出 YAML 可回导',
    },
    {
      label: '连贯性检查',
      naive: false,
      ours: true,
      detail: '自动标记冲突，当前 ' + metrics.continuity_conflicts + ' 处需关注',
    },
  ]

  return (
    <div className="modal-mask" onClick={onClose}>
      {/* 阻止点内容区冒泡到遮罩(避免误关) */}
      <div className="modal baseline-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>朴素版 对比 结构化版</h3>
          <button className="btn modal-close" onClick={onClose} title="关闭对比">
            关闭
          </button>
        </div>

        {/* 五维对比表 */}
        <table className="cmp-table">
          <thead>
            <tr>
              <th>能力维度</th>
              <th>朴素直转</th>
              <th>Screenwright</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td className="dim">{r.label}</td>
                <td className="naive-cell">
                  <span className="mark-no">不支持</span>
                </td>
                <td className="ours-cell">
                  <span className="mark-yes">支持</span>
                  <span className="dim-detail">{r.detail}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {/* 并排剧本对比 */}
        <div className="cmp-cols">
          {/* 左：朴素版(自由文本，明显粗糙) */}
          <div className="cmp-col naive-col">
            <div className="cmp-col-head">
              朴素直转
              <span className="cmp-col-sub">
                自由文本 · {naive.scenes.length} 场 · 无结构
              </span>
            </div>
            <div className="cmp-col-body">
              {naive.scenes.map((sc, i) => (
                <div className="naive-scene" key={i}>
                  <div className="naive-heading">{sc.heading}</div>
                  {sc.lines.map((ln, j) => (
                    <p className="naive-line" key={j}>
                      {ln}
                    </p>
                  ))}
                </div>
              ))}
              {naive.scenes.length === 0 ? (
                <p className="naive-line">(朴素版未返回内容)</p>
              ) : null}
            </div>
          </div>

          {/* 右：我们的结构化版 */}
          <div className="cmp-col ours-col">
            <div className="cmp-col-head">
              Screenwright 结构化版
              <span className="cmp-col-sub">
                有序元素 · {screenplay.scenes.length} 场 · 可溯源/可编辑
              </span>
            </div>
            <div className="cmp-col-body">
              {screenplay.scenes.map((sc) => (
                <div className="scene" key={sc.id}>
                  <div className="scene-head">
                    <span className="scene-slug">
                      {sc.heading.int_ext}. {locOf(screenplay, sc.heading.location_id)} —{' '}
                      {sc.heading.time_of_day}
                    </span>
                  </div>
                  {sc.synopsis ? <div className="scene-synopsis">{sc.synopsis}</div> : null}
                  {sc.elements.map((el, idx) => {
                    if (el.type === 'transition') {
                      return (
                        <div className="el el-transition" key={idx}>
                          {el.text}
                        </div>
                      )
                    }
                    if (el.type === 'action') {
                      return (
                        <div className="el el-action" key={idx}>
                          {el.text}
                          {el.adaptation ? <span className="adapt-tag">外化</span> : null}
                        </div>
                      )
                    }
                    return (
                      <div className="el el-dialogue" key={idx}>
                        <div className="char">{nameOf(screenplay, el.character)}</div>
                        {el.parenthetical ? (
                          <div className="paren">（{el.parenthetical}）</div>
                        ) : null}
                        <div className="line">{el.line}</div>
                      </div>
                    )
                  })}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
