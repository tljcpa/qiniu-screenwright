// ============================================================================
// 双向溯源（创新点②）的核心数据结构与算法
// ----------------------------------------------------------------------------
// 思路：
//   - 每个剧本 element 用一个稳定 key 标识：`${sceneId}#${elementIndex}`。
//   - element.source_ref 给出 (chapter, spans)；spans 是该章原文上的字符区间。
//   - 我们把"原文章节文本"按所有命中的 span 切成若干分片(segment)，
//     每个分片记录它属于哪些 element key。这样：
//       右 -> 左：选中某 element，左侧凡 owners 含该 key 的分片高亮。
//       左 -> 右：点击某分片，取其 owners 里的 element key，右侧对应 element 高亮。
//   - 这是一个纯函数层，组件只消费它产出的结构，便于测试与替换。
// ============================================================================

import type { WorkbenchData, Element } from './api'

// element 的稳定标识
export function elementKey(sceneId: string, index: number): string {
  return sceneId + '#' + index
}

// 解析 elementKey
export function parseElementKey(key: string): { sceneId: string; index: number } {
  const i = key.lastIndexOf('#')
  return { sceneId: key.slice(0, i), index: Number(key.slice(i + 1)) }
}

// 取元素的 source_ref（transition 没有）
function refOf(el: Element): { chapter: number; spans: { start: number; end: number }[] } | null {
  if (el.type === 'transition') return null
  if (!el.source_ref) return null
  return el.source_ref
}

// 原文分片：原文文本上的一段，owners 表示它被哪些 element 溯源覆盖
export interface Segment {
  start: number
  end: number
  text: string
  owners: string[] // element keys
}

// 一章的分片结果
export interface ChapterSegments {
  chapter: number
  segments: Segment[]
}

// 把整部作品的 element source_ref 投影到各章原文上，切出分片。
// 算法：对每章收集所有断点(各 span 的 start/end)，排序去重形成边界，
//       相邻边界构成基础分片；再判断每个基础分片落在哪些 element 的 span 内。
export function buildChapterSegments(data: WorkbenchData): Map<number, ChapterSegments> {
  // 先建立 chapter -> 该章原文长度
  const chapterText = new Map<number, string>()
  for (const ch of data.chapters) {
    chapterText.set(ch.index, ch.text)
  }

  // chapter -> 该章上所有 (span, ownerKey)
  interface OwnedSpan {
    start: number
    end: number
    key: string
  }
  const perChapter = new Map<number, OwnedSpan[]>()

  for (const sc of data.screenplay.scenes) {
    sc.elements.forEach((el, idx) => {
      const ref = refOf(el)
      if (!ref) return
      const key = elementKey(sc.id, idx)
      const arr = perChapter.get(ref.chapter) ?? []
      for (const sp of ref.spans) {
        // 防御：把 span 夹到合法范围内
        const text = chapterText.get(ref.chapter) ?? ''
        const start = Math.max(0, Math.min(sp.start, text.length))
        const end = Math.max(start, Math.min(sp.end, text.length))
        if (end > start) {
          arr.push({ start, end, key })
        }
      }
      perChapter.set(ref.chapter, arr)
    })
  }

  const result = new Map<number, ChapterSegments>()

  for (const ch of data.chapters) {
    const text = ch.text
    const owned = perChapter.get(ch.index) ?? []

    // 收集边界
    const boundSet = new Set<number>([0, text.length])
    for (const o of owned) {
      boundSet.add(o.start)
      boundSet.add(o.end)
    }
    const bounds = Array.from(boundSet).sort((a, b) => a - b)

    const segments: Segment[] = []
    for (let i = 0; i < bounds.length - 1; i++) {
      const s = bounds[i]
      const e = bounds[i + 1]
      if (e <= s) continue
      // 找出覆盖 [s,e) 的所有 owner（owner.span 完整包含该基础分片即可，
      // 因为边界正是由这些 span 端点产生，分片不会跨越任一 span 的边界）
      const owners: string[] = []
      for (const o of owned) {
        if (o.start <= s && o.end >= e) {
          if (!owners.includes(o.key)) owners.push(o.key)
        }
      }
      segments.push({ start: s, end: e, text: text.slice(s, e), owners })
    }
    result.set(ch.index, { chapter: ch.index, segments })
  }

  return result
}
