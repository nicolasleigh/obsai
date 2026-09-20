import { QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from 'next-themes'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import './index.css'
import App from './App.tsx'
import { queryClient } from '@/lib/queryClient'

/**
 * `ThemeProvider` 是 `components/ui/sonner.tsx` 的前提——它用 `next-themes` 的
 * `useTheme()` 决定提示卡的明暗。没有 Provider 时不会报错，只是主题永远停在
 * "system" 的默认值上；`index.css` 里的 `.dark` 变量块也就永远不会被应用。
 *
 * `attribute="class"` 与 `index.css` 的 `@custom-variant dark (&:is(.dark *))` 对应。
 */
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
)
