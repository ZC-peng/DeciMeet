import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Activity, Coins, CheckCircle2, XCircle, PlayCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { QueryErrorState } from '@/components/ui/query-error-state'
import { AgentRunStatusBadge } from '../components/agent-run-status-badge'
import { useAgentRuns, useAgentRunStats } from '../hooks/use-agent-runs'
import { formatDateTime } from '@/lib/utils'
import type { AgentRun, AgentRunStatus } from '@/types'

const STATUS_FILTERS: { label: string; value: AgentRunStatus | 'all' }[] = [
  { label: '全部', value: 'all' },
  { label: '执行中', value: 'running' },
  { label: '成功', value: 'succeeded' },
  { label: '失败', value: 'failed' },
]

export default function AgentRunListPage() {
  const [statusFilter, setStatusFilter] = useState<AgentRunStatus | 'all'>('all')
  const navigate = useNavigate()

  const {
    data: stats,
    isLoading: statsLoading,
    isError: statsIsError,
    refetch: refetchStats,
  } = useAgentRunStats()
  const { data, isLoading, isError, error, refetch } = useAgentRuns({
    status: statusFilter === 'all' ? undefined : statusFilter,
    page_size: 50,
  })
  const runs = data?.items ?? []

  return (
    <div className="space-y-6">
      {/* 标题栏 */}
      <div>
        <h1 className="text-2xl font-bold">Agent 运行监控</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          工作流运行状态：Planner / 节点 / Token / 异常
        </p>
      </div>

      {/* 统计卡片 */}
      <div className="grid gap-4 sm:grid-cols-3">
        <StatCard
          title="总运行数"
          value={stats?.total_runs ?? 0}
          icon={<Activity className="h-4 w-4" />}
          loading={statsLoading}
        />
        <StatCard
          title="成功率"
          value={`${((stats?.success_rate ?? 0) * 100).toFixed(1)}%`}
          icon={<CheckCircle2 className="h-4 w-4" />}
          loading={statsLoading}
        />
        <StatCard
          title="总 Token"
          value={(stats?.total_tokens ?? 0).toLocaleString()}
          icon={<Coins className="h-4 w-4" />}
          loading={statsLoading}
        />
      </div>

      {statsIsError && (
        <QueryErrorState
          compact
          title="运行统计加载失败"
          description="运行列表仍可继续使用。"
          onRetry={() => void refetchStats()}
        />
      )}

      {/* 状态分布 */}
      {stats && (
        <div className="flex flex-wrap gap-3 text-sm">
          {(['running', 'succeeded', 'failed'] as AgentRunStatus[]).map((s) => (
            <div
              key={s}
              className="flex items-center gap-1.5 rounded-md border bg-card px-3 py-1.5"
            >
              <StatusIcon status={s} />
              <span className="text-muted-foreground">{statusLabel(s)}</span>
              <span className="font-semibold">{stats.status_counts[s] ?? 0}</span>
            </div>
          ))}
        </div>
      )}

      {/* 状态过滤 */}
      <div className="flex flex-wrap gap-2">
        {STATUS_FILTERS.map((f) => (
          <Button
            key={f.value}
            size="sm"
            variant={statusFilter === f.value ? 'default' : 'outline'}
            onClick={() => setStatusFilter(f.value)}
          >
            {f.label}
          </Button>
        ))}
      </div>

      {/* Run 列表 */}
      {isLoading ? (
        <div className="space-y-3">
          {[...Array(5)].map((_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      ) : isError ? (
        <QueryErrorState
          error={error}
          title="Agent Run 列表加载失败"
          onRetry={() => void refetch()}
        />
      ) : runs.length > 0 ? (
        <div className="space-y-3">
          {runs.map((run) => (
            <RunRow key={run.id} run={run} onClick={() => navigate(`/agent-runs/${run.id}`)} />
          ))}
        </div>
      ) : (
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed py-16">
          <Activity className="h-10 w-10 text-muted-foreground/50" />
          <p className="mt-3 text-sm text-muted-foreground">暂无 Agent 运行记录</p>
        </div>
      )}
    </div>
  )
}

// ── 子组件 ──

interface StatCardProps {
  title: string
  value: string | number
  icon: React.ReactNode
  loading?: boolean
}

function StatCard({ title, value, icon, loading }: StatCardProps) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center justify-between">
          <span className="text-sm text-muted-foreground">{title}</span>
          {icon}
        </div>
        <div className="mt-2 text-2xl font-bold">
          {loading ? <Skeleton className="h-7 w-16" /> : value}
        </div>
      </CardContent>
    </Card>
  )
}

function RunRow({ run, onClick }: { run: AgentRun; onClick: () => void }) {
  const stepCount = run.steps.length
  const succeededSteps = run.steps.filter((s) => s.status === 'succeeded').length
  const failedSteps = run.steps.filter(
    (s) => s.status === 'failed' || s.status === 'timeout' || s.status === 'budget_exceeded',
  ).length
  const budgetPct = run.max_tokens ? Math.min(100, (run.total_tokens / run.max_tokens) * 100) : 0

  return (
    <Card className="cursor-pointer transition-shadow hover:shadow-md" onClick={onClick}>
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-3">
          {/* 左侧 */}
          <div className="min-w-0 flex-1 space-y-1">
            <div className="flex items-center gap-2">
              <AgentRunStatusBadge status={run.status} />
              <span className="font-mono text-xs text-muted-foreground">{run.id.slice(0, 8)}</span>
              <span className="text-xs text-muted-foreground">·</span>
              <span className="text-xs text-muted-foreground">
                meeting {run.meeting_id.slice(0, 8)}
              </span>
            </div>
            <div className="text-xs text-muted-foreground">
              {formatDateTime(run.started_at)}
              {run.finished_at && ` → ${formatDateTime(run.finished_at)}`}
            </div>
            <div className="text-xs text-muted-foreground">
              步骤: {succeededSteps}/{stepCount} 成功
              {failedSteps > 0 && <span className="text-destructive"> · {failedSteps} 失败</span>}
            </div>
          </div>

          {/* 右侧：Token 预算 */}
          <div className="shrink-0 space-y-1 text-right">
            <div className="text-xs text-muted-foreground">
              Token: {run.total_tokens.toLocaleString()} / {run.max_tokens.toLocaleString()}
            </div>
            <div className="h-1.5 w-32 overflow-hidden rounded-full bg-muted">
              <div
                className={`h-full ${budgetPct > 80 ? 'bg-destructive' : 'bg-primary'}`}
                style={{ width: `${budgetPct}%` }}
              />
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function StatusIcon({ status }: { status: AgentRunStatus }) {
  const cls = 'h-3.5 w-3.5'
  switch (status) {
    case 'running':
      return <PlayCircle className={`${cls} text-blue-500`} />
    case 'succeeded':
      return <CheckCircle2 className={`${cls} text-green-500`} />
    case 'failed':
      return <XCircle className={`${cls} text-red-500`} />
    default:
      return <Activity className={`${cls} text-muted-foreground`} />
  }
}

function statusLabel(s: AgentRunStatus): string {
  const m: Record<AgentRunStatus, string> = {
    pending: '待执行',
    running: '执行中',
    succeeded: '成功',
    failed: '失败',
    paused: '已暂停',
    cancelled: '已取消',
  }
  return m[s]
}
