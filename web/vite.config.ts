/// <reference types="vitest/config" />
import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 开发时把 /api 代理到本地 FastAPI，避免开发期跨域。
export default defineConfig({
  // 相对资源路径，使 dist/ 可被任意挂载点托管（本地预览、FastAPI 子路径均可）。
  base: './',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    // 显式绑回环 IPv4。默认的 'localhost' 在本机只监听 IPv6 的 [::1]，
    // 会让 127.0.0.1:5173 连不上，而后端 uvicorn 绑的是 IPv4，两边不一致。
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: process.env.OBSAI_UI_TARGET ?? 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  // 预览构建产物。给 `scripts/web-smoke.py` 用：开发服务器挂着 HMR 的
  // WebSocket，无头浏览器等不到网络空闲；而预览服务是纯静态的，且跑的是
  // 真正要发布的那份产物。
  preview: {
    host: '127.0.0.1',
    port: 4173,
    proxy: {
      '/api': {
        target: process.env.OBSAI_UI_TARGET ?? 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  build: {
    // 这个界面只跑在 127.0.0.1 上，资源从磁盘读，不存在"首屏体积"这个约束。
    // Vite 默认的 500 kB 阈值是为公网分发的站点定的，在这里只会变成一条常年被
    // 忽略的构建噪音（B-3 引入路由与侧栏后，包体从 299 kB 涨到约 520 kB，增量是
    // react-router 的数据路由与 sidebar 依赖的 radix 原语，均已正确 tree-shake）。
    // 真要按需拆分，等阶段 F 决定桌面封装时再说。
    chunkSizeWarningLimit: 800,
  },
  test: {
    // 只测纯逻辑（错误映射、fetch 封装），不需要 DOM；用 node 环境省掉 jsdom。
    environment: 'node',
    include: ['src/**/*.test.ts'],
    // 复用上面的 `@` 别名，使测试里的导入路径与源码一致。
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
})
