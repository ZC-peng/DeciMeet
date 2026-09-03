import { AlertCircle, RefreshCw } from 'lucide-react'
import { Button } from './button'
import { getErrorMessage } from '@/lib/errors'
import { cn } from '@/lib/utils'

interface QueryErrorStateProps {
  error?: unknown
  title?: string
  description?: string
  onRetry?: () => void
  compact?: boolean
  className?: string
}

export function QueryErrorState({
  error,
  title = '加载失败',
  description,
  onRetry,
  compact = false,
  className,
}: QueryErrorStateProps) {
  return (
    <div
      role="alert"
      className={cn(
        'flex flex-col items-center justify-center rounded-lg border border-destructive/30 bg-destructive/5 text-center',
        compact ? 'px-4 py-6' : 'px-6 py-14',
        className,
      )}
    >
      <AlertCircle className="h-8 w-8 text-destructive" />
      <p className="mt-3 text-sm font-medium">{title}</p>
      <p className="mt-1 max-w-xl text-xs text-muted-foreground">
        {description ?? getErrorMessage(error)}
      </p>
      {onRetry && (
        <Button variant="outline" size="sm" className="mt-4" onClick={onRetry}>
          <RefreshCw className="h-3.5 w-3.5" />
          重试
        </Button>
      )}
    </div>
  )
}
