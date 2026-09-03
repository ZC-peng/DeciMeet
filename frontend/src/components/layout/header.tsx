import { Moon, Sun } from 'lucide-react'
import { useUIStore } from '@/stores/ui-store'
import { Button } from '@/components/ui/button'

export function Header() {
  const { darkMode, toggleDarkMode } = useUIStore()

  return (
    <header className="flex h-16 items-center justify-between border-b bg-card px-6">
      <div>
        <p className="text-sm font-medium">可控工作流 · 决策可追溯</p>
        <p className="text-xs text-muted-foreground">会议分析、运行时间线与混合检索</p>
      </div>

      <div className="flex items-center gap-2">
        <Button variant="ghost" size="icon" onClick={toggleDarkMode} title="切换主题">
          {darkMode ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
        </Button>
      </div>
    </header>
  )
}
