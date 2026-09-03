import { useQuery } from '@tanstack/react-query'
import {
  listAgentRuns,
  getAgentRun,
  getAgentRunStats,
  type ListAgentRunsParams,
} from '@/api/agent-runs'
import { QUERY_KEYS } from '@/lib/constants'

// 列表
export function useAgentRuns(params: ListAgentRunsParams = {}) {
  return useQuery({
    queryKey: QUERY_KEYS.agentRunsList(params as Record<string, unknown>),
    queryFn: () => listAgentRuns(params),
    // 仅活动状态自动轮询；所有任务进入终态后停止。
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? []
      if (items.length === 0) return false
      const hasActive = items.some(
        (r) => r.status === 'running' || r.status === 'pending',
      )
      return hasActive ? 5000 : false
    },
  })
}

// 详情
export function useAgentRun(runId: string | undefined) {
  return useQuery({
    queryKey: QUERY_KEYS.agentRun(runId || ''),
    queryFn: () => getAgentRun(runId!),
    enabled: !!runId,
    // 终态（succeeded/failed/cancelled）停止轮询
    refetchInterval: (query) => {
      const r = query.state.data
      if (!r) return false
      if (r.status === 'running' || r.status === 'pending') {
        return 3000
      }
      return false
    },
  })
}

// 统计
export function useAgentRunStats() {
  return useQuery({
    queryKey: QUERY_KEYS.agentRunStats,
    queryFn: getAgentRunStats,
    refetchInterval: 10000,
  })
}
