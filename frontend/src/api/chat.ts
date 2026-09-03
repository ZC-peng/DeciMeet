import { apiClient } from './client'
import { API_BASE_URL } from '@/lib/constants'
import type { ChatSession, ChatMessage, ChatSSEEvent } from '@/types'

// 创建会话
export async function createSession(data: {
  meeting_id?: string
  title?: string
}): Promise<ChatSession> {
  return apiClient.post('chat/sessions', { json: data }).json()
}

// 获取会话列表
export async function listSessions(meetingId?: string): Promise<ChatSession[]> {
  return apiClient
    .get('chat/sessions', {
      searchParams: meetingId ? { meeting_id: meetingId } : {},
    })
    .json()
}

// 获取会话消息
export async function getSessionMessages(sessionId: string): Promise<ChatMessage[]> {
  return apiClient.get(`chat/sessions/${sessionId}/messages`).json()
}

// 删除会话
export async function deleteSession(sessionId: string): Promise<void> {
  await apiClient.delete(`chat/sessions/${sessionId}`)
}

// SSE 流式对话
export async function* streamChat(
  sessionId: string,
  query: string,
  images?: string[],
): AsyncGenerator<ChatSSEEvent> {
  const response = await fetch(`${API_BASE_URL}/chat/sessions/${sessionId}/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, images }),
  })

  if (!response.ok) {
    throw new Error(`请求失败: ${response.status}`)
  }

  const reader = response.body?.getReader()
  if (!reader) throw new Error('无法读取响应流')

  const decoder = new TextDecoder()
  let buffer = ''
  let terminalEventReceived = false
  let streamEnded = false

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) {
        streamEnded = true
        break
      }

      buffer += decoder.decode(value, { stream: true })

      // 当前后端每个 SSE 事件使用单行 data JSON；保留未完整行等待下一个 chunk。
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        const event = parseDataLine(line)
        if (!event) continue
        if (event.type === 'done' || event.type === 'error') {
          terminalEventReceived = true
        }
        yield event
      }
    }

    // flush TextDecoder，并处理没有以换行结尾的最后一个完整 data 行。
    buffer += decoder.decode()
    const trailingEvent = parseDataLine(buffer)
    if (trailingEvent) {
      if (trailingEvent.type === 'done' || trailingEvent.type === 'error') {
        terminalEventReceived = true
      }
      yield trailingEvent
    }

    if (!terminalEventReceived) {
      throw new Error('响应流意外中断，请重试')
    }
  } finally {
    if (!streamEnded) {
      await reader.cancel().catch(() => undefined)
    }
    reader.releaseLock()
  }
}

function parseDataLine(line: string): ChatSSEEvent | null {
  const normalized = line.trimEnd()
  if (!normalized.startsWith('data:')) return null

  const data = normalized.slice(5).trimStart()
  if (!data) return null

  try {
    return JSON.parse(data) as ChatSSEEvent
  } catch {
    throw new Error('响应流数据格式错误')
  }
}
