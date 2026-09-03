import { useVirtualizer } from '@tanstack/react-virtual'
import { useRef, useCallback, useState, useEffect } from 'react'

interface UseVirtualListOptions {
  /** 列表数据数量 */
  count: number
  /** 预估每项高度 */
  estimateSize: number
  /** 滚动方向 */
  horizontal?: boolean
  /** 额外滚动边距 */
  overscan?: number
}

/**
 * 虚拟列表 Hook
 * 基于 @tanstack/react-virtual，支持动态高度
 */
export function useVirtualList({
  count,
  estimateSize,
  horizontal = false,
  overscan = 8,
}: UseVirtualListOptions) {
  const parentRef = useRef<HTMLDivElement>(null)
  const [scrollOffset, setScrollOffset] = useState(0)
  const [isNearBottom, setIsNearBottom] = useState(true)

  const virtualizer = useVirtualizer({
    count,
    getScrollElement: () => parentRef.current,
    estimateSize: () => estimateSize,
    overscan,
    horizontal,
  })

  // 记录滚动位置
  const handleScroll = useCallback(() => {
    if (parentRef.current) {
      const element = parentRef.current
      setScrollOffset(element.scrollTop)
      const remaining = element.scrollHeight - element.scrollTop - element.clientHeight
      setIsNearBottom(remaining <= 80)
    }
  }, [])

  // 恢复滚动位置
  const restoreScroll = useCallback(
    (offset: number) => {
      if (parentRef.current) {
        parentRef.current.scrollTop = offset
      }
    },
    [],
  )

  // 自动滚动到底部
  const scrollToBottom = useCallback(() => {
    if (parentRef.current) {
      const element = parentRef.current
      requestAnimationFrame(() => {
        element.scrollTop = element.scrollHeight
      })
    }
  }, [])

  // 监听滚动
  useEffect(() => {
    const el = parentRef.current
    if (!el) return
    el.addEventListener('scroll', handleScroll, { passive: true })
    return () => el.removeEventListener('scroll', handleScroll)
  }, [handleScroll])

  return {
    parentRef,
    virtualizer,
    scrollOffset,
    isNearBottom,
    restoreScroll,
    scrollToBottom,
    items: virtualizer.getVirtualItems(),
    totalSize: virtualizer.getTotalSize(),
  }
}
