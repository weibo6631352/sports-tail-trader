import { expect, test, type Page, type Route } from '@playwright/test'
// Node ESM 强制 JSON import 用 import attributes（playwright 走 Node loader）。
import runtimeSnapshot from './fixtures/runtime.snapshot.json' with { type: 'json' }
import readySnapshot from './fixtures/ready.snapshot.json' with { type: 'json' }
import workersSnapshot from './fixtures/workers.snapshot.json' with { type: 'json' }
import portfolioSnapshot from './fixtures/portfolio.snapshot.json' with { type: 'json' }
import metricsSnapshot from './fixtures/metrics.snapshot.json' with { type: 'json' }
import latencySnapshot from './fixtures/latency.snapshot.json' with { type: 'json' }
import candidatesList from './fixtures/candidates.list.json' with { type: 'json' }
import ordersOpen from './fixtures/orders.open.json' with { type: 'json' }

// 业务断言：所有 /api/* 走 page.route() 拦截，返回 fixture JSON——不依赖真后端。
// 既验证 SPA 在权威数据到位时的渲染分支，又规避 preview server 拉真后端的网络抖动。
// 不在此覆盖 SSE / /stream——SSE 失败已有 fallback，且不影响首屏业务展示。

/**
 * 顺序很关键：先按路径精确匹配（precedence 由注册顺序决定，越早注册优先级越高），
 * 兜底所有未匹配 /api/** 返回 404，避免某条 fetch 漏 mock 导致测试静默挂起。
 */
async function mockApi(page: Page): Promise<void> {
  // candidates.list 必须能识别 limit / accepted / 等任意 query；按 pathname 匹配。
  await routeJson(page, '**/api/candidates', candidatesList)
  await routeJson(page, '**/api/orders', ordersOpen)
  await routeJson(page, '**/api/runtime', runtimeSnapshot)
  await routeJson(page, '**/api/ready', readySnapshot)
  await routeJson(page, '**/api/workers', workersSnapshot)
  await routeJson(page, '**/api/portfolio', portfolioSnapshot)
  await routeJson(page, '**/api/metrics/latency-percentiles', latencySnapshot)
  await routeJson(page, '**/api/metrics', metricsSnapshot)

  // 兜底：其它任何 /api/** 返回空 page，避免依赖未 mock 的端点时测试挂死。
  await page.route('**/api/**', (route: Route) => {
    void route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], total: 0, limit: 100, offset: 0, has_more: false }),
    })
  })
}

async function routeJson(page: Page, pattern: string, payload: unknown): Promise<void> {
  await page.route(pattern, (route: Route) => {
    void route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(payload),
    })
  })
}

test.describe('盯盘首页 LiveOverview', () => {
  test('展示 portfolio 净值 / phase / worker 列表', async ({ page }) => {
    await mockApi(page)
    await page.goto('/live')

    // 标题
    await expect(page.getByText('盯盘总览')).toBeVisible()

    // Portfolio 卡：fixture 里 net_value_usdc = 12345.67，formatUsdc 输出可能含千分位
    // 不绑死格式细节，断 "12,345" 子串（覆盖 toLocaleString 与裸字符串两种格式）。
    const portfolioCard = page.locator('text=Portfolio').locator('..')
    await expect(portfolioCard).toBeVisible()
    await expect(page.getByText(/12[,.]?345/).first()).toBeVisible()

    // phase 由 Workers SectionCard 的 description 渲染：phase: running
    await expect(page.getByText(/phase:\s*running/)).toBeVisible()

    // worker 列表至少包含 fixture 中的两个 worker name
    await expect(page.getByText('trading_decision_worker')).toBeVisible()
    await expect(page.getByText('persistence_worker')).toBeVisible()
  })
})

test.describe('候选列表 Candidates', () => {
  test('表格渲染 3 行 fixture 数据并显示 league / market 关键列', async ({ page }) => {
    await mockApi(page)
    await page.goto('/live/candidates')

    await expect(page.getByText('候选 Candidates')).toBeVisible()

    // league 列：NBA / MLB / EPL 都在 fixture
    await expect(page.getByText('NBA').first()).toBeVisible()
    await expect(page.getByText('MLB').first()).toBeVisible()
    await expect(page.getByText('EPL').first()).toBeVisible()

    // market_slug 列
    await expect(page.getByText('nba-lakers-vs-celtics-2026-05-12')).toBeVisible()

    // 表格行数 = 3（不含 thead）
    const dataRows = page.locator('table tbody tr')
    await expect(dataRows).toHaveCount(3)
  })

  test('显示 execution_permission 与 accepted 状态徽章', async ({ page }) => {
    await mockApi(page)
    await page.goto('/live/candidates')

    // auto / manual / record_only 三种 permission 都应出现
    await expect(page.getByText('auto').first()).toBeVisible()
    await expect(page.getByText('manual').first()).toBeVisible()
    await expect(page.getByText('record_only').first()).toBeVisible()

    // accepted 列文本 yes/no
    await expect(page.getByText('yes').first()).toBeVisible()
    await expect(page.getByText('no').first()).toBeVisible()
  })
})

test.describe('开口订单 LiveOrders', () => {
  test('展示 open order 行 + cancel/replace 按钮（不点击）', async ({ page }) => {
    await mockApi(page)
    await page.goto('/live/orders')

    await expect(page.getByText('开口订单')).toBeVisible()

    // fixture 有 2 笔 open order
    const dataRows = page.locator('table tbody tr')
    await expect(dataRows).toHaveCount(2)

    // 关键列：market_slug / side / status
    await expect(page.getByText('nba-lakers-vs-celtics-2026-05-12')).toBeVisible()
    await expect(page.getByText('mlb-yankees-vs-redsox-2026-05-12')).toBeVisible()
    await expect(page.getByText('BUY').first()).toBeVisible()
    await expect(page.getByText('SELL').first()).toBeVisible()
    await expect(page.getByText('OPEN').first()).toBeVisible()

    // cancel / replace 按钮：每行一组，共 2 个
    // InlineActionButton 渲染成 <button>cancel</button>，用 role+name 精确匹配。
    await expect(page.getByRole('button', { name: 'cancel' })).toHaveCount(2)
    await expect(page.getByRole('button', { name: 'replace' })).toHaveCount(2)
  })
})
