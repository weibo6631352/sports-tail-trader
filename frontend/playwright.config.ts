import { defineConfig } from '@playwright/test'

// 前端 e2e 骨架。
// 当前只放最小冒烟用例（shell 渲染），不依赖后端 /api。
// webServer 用 vite preview 跑 dist 构建产物，避免 dev server 启动慢导致 CI 抖动。
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  retries: 0,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'off',
  },
  projects: [
    {
      name: 'chromium',
      use: { browserName: 'chromium' },
    },
  ],
  webServer: {
    // 先 build 再 preview——保证测的是和发布产物一致的 shell。
    command: 'npm run build && npm run preview -- --port 4173 --host 127.0.0.1 --strictPort',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
})
