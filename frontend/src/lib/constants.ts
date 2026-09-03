export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api'
export const SSE_BASE_URL = import.meta.env.VITE_SSE_BASE_URL || '/api'

export const QUERY_KEYS = {
  // meetings
  meetings: 'meetings',
  meeting: (id: string) => ['meeting', id] as const,
  transcripts: (meetingId: string) => ['transcripts', meetingId] as const,
  transcriptionStatus: (meetingId: string) => ['transcription-status', meetingId] as const,
  // summaries
  summaries: 'summaries',
  summaryList: (page: number, pageSize: number) => ['summary-list', page, pageSize] as const,
  meetingSummary: (meetingId: string) => ['meeting-summary', meetingId] as const,
  summaryDetail: (meetingId: string) => ['summary-detail', meetingId] as const,
  actionItems: (meetingId: string) => ['action-items', meetingId] as const,
  risks: (meetingId: string) => ['risks', meetingId] as const,
  // chat
  chatSessions: (meetingId: string) => ['chat-sessions', meetingId] as const,
  chatMessages: (sessionId: string) => ['chat-messages', sessionId] as const,
  // knowledge
  knowledge: 'knowledge',
  knowledgeDocuments: (page: number, pageSize: number) =>
    ['knowledge', 'documents', page, pageSize] as const,
  // agent runs
  agentRuns: ['agent-runs'] as const,
  agentRunsList: (params: Record<string, unknown>) => ['agent-runs', 'list', params] as const,
  agentRun: (id: string) => ['agent-runs', 'detail', id] as const,
  agentRunStats: ['agent-runs', 'stats'] as const,
  // decisions
  decisions: 'decisions',
  decisionList: (skip: number, limit: number, meetingId?: string) =>
    ['decision-list', skip, limit, meetingId] as const,
  decision: (id: string) => ['decision', id] as const,
  decisionSearch: (query: string) => ['decision-search', query] as const,
} as const

export const APP_TITLE = 'DeciMeet · 多 Agent 会议决策工作台'
