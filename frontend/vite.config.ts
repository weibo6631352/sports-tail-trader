import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// 分包策略：
//   - react-vendor: react / react-dom / react-router-dom（基础运行时，长期缓存）
//   - mantine: @mantine/* 全家桶（最重的 UI 依赖，独立缓存）
//   - icons: @tabler/icons-react（按需 tree-shake 但 SVG 多，单独成块）
//   - charts: recharts + d3-*（仅分析页 / 盘口回放需要）
//   - table: @tanstack/react-table
//   - query: @tanstack/react-query
//   - utils: decimal.js + dayjs + clsx + zustand（小工具合并）
//   - vendor: 其他第三方
// 业务代码默认由 Vite 按动态 import() 边界自动分块——即每条路由独立 chunk。

function pickVendorChunk(id: string): string | undefined {
  if (!id.includes('node_modules')) return undefined
  if (id.includes('/react-router')) return 'react-vendor'
  if (id.includes('/react-dom/') || /\/react\//.test(id) || id.endsWith('/react')) return 'react-vendor'
  if (id.includes('@mantine/')) return 'mantine'
  if (id.includes('@tabler/icons-react')) return 'icons'
  if (id.includes('/recharts/') || id.includes('/d3-')) return 'charts'
  if (id.includes('@tanstack/react-table')) return 'table'
  if (id.includes('@tanstack/react-query')) return 'query'
  if (
    id.includes('/decimal.js/') ||
    id.includes('/dayjs/') ||
    id.includes('/clsx/') ||
    id.includes('/zustand/')
  ) {
    return 'utils'
  }
  return 'vendor'
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const proxyTarget = env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000'

  return {
    plugins: [react()],
    resolve: {
      alias: [
        { find: /^@app\//, replacement: `${path.resolve(__dirname, 'src/app')}/` },
        { find: /^@core$/, replacement: path.resolve(__dirname, 'src/core/index.ts') },
        { find: /^@core\//, replacement: `${path.resolve(__dirname, 'src/core')}/` },
        { find: /^@shared$/, replacement: path.resolve(__dirname, 'src/shared/index.ts') },
        { find: /^@shared\//, replacement: `${path.resolve(__dirname, 'src/shared')}/` },
        { find: /^@features\//, replacement: `${path.resolve(__dirname, 'src/features')}/` },
        { find: /^@strategy$/, replacement: path.resolve(__dirname, 'src/strategy/index.ts') },
        { find: /^@strategy\//, replacement: `${path.resolve(__dirname, 'src/strategy')}/` },
        { find: /^@styles\//, replacement: `${path.resolve(__dirname, 'src/styles')}/` },
      ],
    },
    server: {
      host: '0.0.0.0',
      port: 5173,
      proxy: {
        '/api': {
          target: proxyTarget,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
        },
        '/stream': {
          target: proxyTarget,
          changeOrigin: true,
          ws: false,
        },
      },
    },
    build: {
      // 警告阈值上调到 800KB——主 chunk 拆完后仍可能略大于默认 500KB，但不构成问题。
      chunkSizeWarningLimit: 800,
      rollupOptions: {
        output: {
          manualChunks: pickVendorChunk,
        },
      },
    },
  }
})
