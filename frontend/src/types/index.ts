// 会议状态
export type MeetingStatus = 'pending' | 'transcribing' | 'processed' | 'failed'

// 会议
export interface Meeting {
  id: string
  title: string
  description?: string
  audio_url?: string
  status: MeetingStatus
  // 转写模式：real(真实API) / mock(本地降级) / null(未转写)
  transcription_mode?: 'real' | 'mock' | null
  start_time?: string
  end_time?: string
  participants?: string[]
  created_at: string
  updated_at: string
}

// 转写记录
export interface Transcript {
  id: string
  meeting_id: string
  speaker: string
  content: string
  start_time: number
  end_time: number
  seq_index: number
  created_at: string
}

// 纪要
export interface Summary {
  id: string
  meeting_id: string
  content: string
  key_points?: string[]
  status: 'generating' | 'completed' | 'failed'
  created_at: string
}

// 纪要列表项
export interface SummaryListItem {
  id: string
  meeting_id: string
  meeting_title: string
  content: string
  status: string
  created_at: string
}

// 纪要综合响应（纪要 + 行动项 + 风险）
export interface MeetingSummary {
  summary: Summary | null
  action_items: ActionItem[]
  risks: Risk[]
}

// 行动项
export interface ActionItem {
  id: string
  meeting_id: string
  title: string
  assignee?: string
  due_date?: string
  priority: 'high' | 'medium' | 'low'
  status: 'pending' | 'in_progress' | 'done'
  created_at: string
}

// 风险
export interface Risk {
  id: string
  meeting_id: string
  description: string
  severity: 'high' | 'medium' | 'low'
  mitigation?: string
  created_at: string
}

// 对话会话
export interface ChatSession {
  id: string
  meeting_id?: string
  title: string
  created_at: string
}

// 对话消息
export interface ChatSource {
  title: string
  source_type: string
  score?: number
}

export interface ChatMessage {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  metadata?: {
    sources?: ChatSource[]
  }
  created_at: string
}

// 知识文档
export interface KnowledgeDocument {
  id: string
  title: string
  source_type: 'meeting_summary' | 'uploaded_doc'
  source_id?: string
  content: string
  metadata?: Record<string, unknown>
  created_at: string
}

// 检索结果项
export interface SearchResult {
  id: string
  content: string
  title: string
  source_type: string
  source_id?: string
  metadata?: Record<string, unknown>
  score: number
  rerank_score?: number
}

// 检索响应
export interface KnowledgeSearchResponse {
  query: string
  results: SearchResult[]
  total: number
}

// SSE 流式事件
export interface SSEEvent {
  event: string
  data: string
  id?: string
}

// SSE 对话事件
export interface ChatSSEEvent {
  type: 'token' | 'done' | 'error'
  content?: string
  message?: string
  sources?: ChatSource[]
}

// API 分页响应
export interface PaginatedResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

// API 错误响应
export interface ApiError {
  detail: string
  code?: string
}

// ── AgentRun（Harness 生命周期） ──

// AgentRun 状态
export type AgentRunStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'paused' | 'cancelled'

// Planner 输出的执行计划
export interface ExecutionPlan {
  meeting_type: 'standup' | 'review' | 'decision' | 'brainstorm' | 'unknown'
  should_run_summary: boolean
  should_run_actions: boolean
  should_run_risks: boolean
  should_run_decisions: boolean
  transcript_strategy: 'full' | 'compressed'
  estimated_tokens: number
  reason: string
}

// 节点执行 step
export interface AgentRunStep {
  node: string
  status:
    | 'running'
    | 'succeeded'
    | 'failed'
    | 'timeout'
    | 'skipped'
    | 'budget_exceeded'
    | 'invalid_output'
  started_at?: string
  finished_at?: string
  duration_ms?: number
  error?: string
}

// 节点级 Token 消耗
export interface NodeUsage {
  [nodeName: string]: {
    tokens: number
    input_tokens?: number
    output_tokens?: number
    total_tokens?: number
  }
}

// AgentRun
export interface AgentRun {
  id: string
  meeting_id: string
  graph_name: string
  status: AgentRunStatus
  current_node?: string | null
  plan?: ExecutionPlan | null
  started_at: string
  finished_at?: string | null
  max_tokens: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  steps: AgentRunStep[]
  node_usage?: NodeUsage
  error?: string | null
  created_at: string
}

// AgentRun 列表响应
export interface AgentRunListResponse {
  items: AgentRun[]
  total: number
  page: number
  page_size: number
}

// ── 评审决策知识库 ───────────────────────────────────────────

// 决策候选方案
export interface DecisionOption {
  id: string
  name: string
  pros?: string[]
  cons?: string[]
  proposed_by?: string | null
  is_chosen: boolean
}

// 关联决策（向量相似 top-3）
export interface RelatedDecision {
  id: string
  title: string
  similarity_score?: number | null
  relation_type: string
  context?: string | null
}

// 决策详情
export interface DecisionDetail {
  id: string
  meeting_id: string | null
  title: string
  context?: string | null
  snippet?: string | null
  chosen_option?: string | null
  reasons?: string[] | null
  objections?: Array<{ from: string; content: string }> | null
  decided_by?: string[] | null
  decided_at?: string | null
  confidence?: number | null
  created_at: string
  options: DecisionOption[]
  related_decisions: RelatedDecision[]
}

// 决策列表项（精简版）
export interface DecisionListItem {
  id: string
  title: string
  chosen_option?: string | null
  meeting_id: string | null
  decided_by?: string[] | null
  confidence?: number | null
  created_at: string
}

// 决策列表响应
export interface DecisionListResponse {
  items: DecisionListItem[]
  total: number
  skip: number
  limit: number
}

// 决策搜索结果项
export interface DecisionSearchResult {
  id: string
  title: string
  context?: string | null
  chosen_option?: string | null
  meeting_id: string | null
  score: number
  source_type: string
}

// 决策搜索响应
export interface DecisionSearchResponse {
  items: DecisionSearchResult[]
  query: string
  total: number
}
