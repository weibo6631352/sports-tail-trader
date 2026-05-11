// 通用 Recharts 主题片段——9 个 Tooltip / Axis 之前各自硬编码 hex，改主题色靠
// 全局搜索替换。集中在这里之后，重涂主题只动这一处。
// 颜色源自 styles/tokens.css 的 --color-surface / --color-border / --color-text-dim。

export const chartTooltipStyle = {
  background: '#121e36',
  border: '1px solid #243352',
} as const

export const chartAxisStroke = '#97a6c2'
export const chartGridStroke = '#243352'
