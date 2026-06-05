// 左侧：小说原文面板（按章展示）
// 职责：把每章原文按溯源分片渲染；命中高亮的分片加 .hl；
//       点击分片时，把该分片 owners 里的 element key 回传给父组件(实现 左->右)。
import { useEffect, useRef } from 'react'
import type { Chapter } from '../api'
import type { ChapterSegments } from '../trace'

interface Props {
  chapters: Chapter[]
  segmentsByChapter: Map<number, ChapterSegments>
  // 当前需要高亮的 element keys 集合
  activeKeys: Set<string>
  // 点击原文分片：把该分片关联的 element keys 交给父组件决定高亮谁
  onSegmentClick: (ownerKeys: string[]) => void
}

export default function NovelPane(props: Props) {
  const { chapters, segmentsByChapter, activeKeys, onSegmentClick } = props
  // 用 ref 把第一个高亮分片滚到可见
  const firstHlRef = useRef<HTMLSpanElement | null>(null)

  useEffect(() => {
    if (firstHlRef.current) {
      firstHlRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, [activeKeys])

  // 记录是否已经把"第一个高亮分片"的 ref 绑定过，保证只滚到第一个
  let assignedFirst = false

  // 防御：按 index 去重，确保每章只渲染一次。
  // 正常后端每章 index 唯一；但若上游异常返回重复章节，
  // 这里保证左栏不会把同一章原文刷多遍(对应历史 Bug1 的根因防线)。
  const seenIndex = new Set<number>()
  const uniqueChapters = chapters.filter((ch) => {
    if (seenIndex.has(ch.index)) {
      return false
    }
    seenIndex.add(ch.index)
    return true
  })

  return (
    <div className="pane left">
      <h2 className="pane-title">小说原文</h2>
      {uniqueChapters.map((ch) => {
        const seg = segmentsByChapter.get(ch.index)
        return (
          <div className="chapter" key={ch.index}>
            <h3>{ch.title}</h3>
            <div className="body">
              {seg
                ? seg.segments.map((s, i) => {
                    // 该分片是否命中高亮：它的某个 owner 在 activeKeys 内
                    const hit = s.owners.some((k) => activeKeys.has(k))
                    const clickable = s.owners.length > 0
                    let ref: ((el: HTMLSpanElement | null) => void) | undefined
                    if (hit && !assignedFirst) {
                      assignedFirst = true
                      ref = (el) => {
                        firstHlRef.current = el
                      }
                    }
                    let cls = 'src-seg'
                    if (!clickable) cls = '' // 非溯源文本不可点、无悬停态
                    if (hit) cls = 'src-seg hl'
                    return (
                      <span
                        key={i}
                        ref={ref}
                        className={cls}
                        onClick={
                          clickable
                            ? () => onSegmentClick(s.owners)
                            : undefined
                        }
                      >
                        {s.text}
                      </span>
                    )
                  })
                : ch.text}
            </div>
          </div>
        )
      })}
    </div>
  )
}
