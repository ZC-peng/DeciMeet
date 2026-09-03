import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { resolve } from 'node:path'
import { visualizer } from 'rollup-plugin-visualizer'

// vite-plugin-compression 是 CJS 模块，需要 default 取值
import viteCompression from 'vite-plugin-compression'

const bundleAnalysisPlugins =
  process.env.ANALYZE === 'true'
    ? [
        visualizer({
          open: false,
          filename: resolve(__dirname, 'build-analysis/stats.html'),
          gzipSize: true,
          brotliSize: true,
        }),
      ]
    : []

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    // @ts-expect-error CJS 默认导出类型不匹配
    viteCompression({
      verbose: true,
      disable: false,
      threshold: 10240,
      algorithm: 'gzip',
      ext: '.gz',
    }),
    ...bundleAnalysisPlugins,
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  build: {
    // modulePreload：预加载关键 chunk，提升路由切换速度
    modulePreload: {
      polyfill: true,
      // 只预加载首屏关键 chunk
      resolveDependencies: (_, deps) =>
        deps.filter(
          (dep) =>
            dep.includes('react-vendor') ||
            dep.includes('ui-vendor') ||
            dep.includes('state-vendor'),
        ),
    },
    // CSS 代码分割
    cssCodeSplit: true,
    // 分包策略：将 vendor 库拆分，优化首屏加载
    rollupOptions: {
      output: {
        manualChunks(id) {
          const normalizedId = id.replaceAll('\\', '/')
          if (!normalizedId.includes('/node_modules/')) return undefined

          const isPackage = (name: string) => normalizedId.includes(`/node_modules/${name}/`)

          // 先匹配名称中包含 react 的业务依赖，避免被宽泛的 React 匹配吞并。
          if (
            isPackage('react-markdown') ||
            isPackage('remark-gfm') ||
            isPackage('rehype-highlight') ||
            isPackage('highlight.js')
          ) {
            return 'markdown-vendor'
          }
          if (
            isPackage('@tanstack/react-query') ||
            isPackage('@tanstack/react-virtual') ||
            isPackage('zustand')
          ) {
            return 'state-vendor'
          }
          if (
            isPackage('lucide-react') ||
            isPackage('class-variance-authority') ||
            isPackage('clsx') ||
            isPackage('tailwind-merge')
          ) {
            return 'ui-vendor'
          }
          if (
            isPackage('react') ||
            isPackage('react-dom') ||
            isPackage('react-router') ||
            isPackage('react-router-dom') ||
            isPackage('scheduler')
          ) {
            return 'react-vendor'
          }

          return undefined
        },
      },
    },
    // chunk 大小警告阈值
    chunkSizeWarningLimit: 500,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8787',
        changeOrigin: true,
      },
    },
  },
})
