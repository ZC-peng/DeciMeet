import { lazy } from 'react'
import type { RouteObject } from 'react-router-dom'
import { AppLayout } from '@/components/layout/app-layout'

// 路由懒加载
const MeetingListPage = lazy(() => import('@/features/meetings/pages/meeting-list-page'))
const MeetingDetailPage = lazy(() => import('@/features/meetings/pages/meeting-detail-page'))
const SummaryListPage = lazy(() => import('@/features/summaries/pages/summary-list-page'))
const SummaryDetailPage = lazy(() => import('@/features/summaries/pages/summary-detail-page'))
const ChatPage = lazy(() => import('@/features/chat/pages/chat-page'))
const KnowledgePage = lazy(() => import('@/features/knowledge/pages/knowledge-page'))
const AgentRunListPage = lazy(() => import('@/features/agent-runs/pages/agent-run-list-page'))
const AgentRunDetailPage = lazy(() => import('@/features/agent-runs/pages/agent-run-detail-page'))
const DecisionListPage = lazy(() => import('@/features/decisions/pages/decision-list-page'))
const DecisionDetailPage = lazy(() => import('@/features/decisions/pages/decision-detail-page'))

export const routes: RouteObject[] = [
  {
    path: '/',
    element: <AppLayout />,
    children: [
      {
        index: true,
        element: <MeetingListPage />,
      },
      {
        path: 'meetings/:id',
        element: <MeetingDetailPage />,
      },
      {
        path: 'summaries',
        element: <SummaryListPage />,
      },
      {
        path: 'summaries/:id',
        element: <SummaryDetailPage />,
      },
      {
        path: 'chat',
        element: <ChatPage />,
      },
      {
        path: 'knowledge',
        element: <KnowledgePage />,
      },
      {
        path: 'agent-runs',
        element: <AgentRunListPage />,
      },
      {
        path: 'agent-runs/:id',
        element: <AgentRunDetailPage />,
      },
      {
        path: 'decisions',
        element: <DecisionListPage />,
      },
      {
        path: 'decisions/:id',
        element: <DecisionDetailPage />,
      },
    ],
  },
]
