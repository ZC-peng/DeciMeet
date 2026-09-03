export function getErrorStatus(error: unknown): number | undefined {
  if (!error || typeof error !== 'object' || !('response' in error)) return undefined

  const response = (error as { response?: { status?: unknown } }).response
  return typeof response?.status === 'number' ? response.status : undefined
}

export function isNotFoundError(error: unknown): boolean {
  return getErrorStatus(error) === 404
}

export function getErrorMessage(error: unknown, fallback = '请求失败，请稍后重试'): string {
  return error instanceof Error && error.message ? error.message : fallback
}
