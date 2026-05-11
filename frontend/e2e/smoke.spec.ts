import { expect, test } from '@playwright/test'

// 最小冒烟：只验证 shell 渲染，不依赖后端 /api。
// 用例失败 = SPA 启动链路（Vite build、Mantine、router、Sidebar）断了。
test('shell renders sidebar with brand and nav groups', async ({ page }) => {
  await page.goto('/')

  // 品牌字样
  await expect(page.getByText('Sports Tail Trader')).toBeVisible()

  // 至少出现一个导航分组标题；任一命中即可，避免对未来 IA 调整过敏。
  const navGroupLabels = ['盯盘 Live', '复盘 Investigate', '分析 Analytics']
  const groupLocator = page.locator('text=/' + navGroupLabels.join('|') + '/').first()
  await expect(groupLocator).toBeVisible()
})
