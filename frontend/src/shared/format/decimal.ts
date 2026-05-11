import Decimal from 'decimal.js'

// 后端 Decimal 一律字符串化（避免 JS 数字精度漂移）。前端用 decimal.js 处理。

export function toDecimal(value: string | number | null | undefined): Decimal | null {
  if (value === null || value === undefined || value === '') return null
  try {
    return new Decimal(value)
  } catch {
    return null
  }
}

export function formatDecimal(
  value: string | number | null | undefined,
  options: { dp?: number; signed?: boolean; zeroAs?: string; suffix?: string } = {},
): string {
  const dp = options.dp ?? 4
  const dec = toDecimal(value)
  if (dec === null) return '—'
  if (dec.isZero() && options.zeroAs) return options.zeroAs
  const text = dec.toFixed(dp)
  const stripped = options.dp === undefined ? trimTrailingZeros(text) : text
  const signed = options.signed && dec.isPositive() ? `+${stripped}` : stripped
  return options.suffix ? `${signed}${options.suffix}` : signed
}

function trimTrailingZeros(text: string): string {
  if (!text.includes('.')) return text
  const [intPart, fracPart] = text.split('.')
  const trimmed = fracPart.replace(/0+$/, '')
  return trimmed ? `${intPart}.${trimmed}` : intPart
}

export function formatUsdc(value: string | number | null | undefined, dp = 2): string {
  const dec = toDecimal(value)
  if (dec === null) return '—'
  return `$${dec.toFixed(dp)}`
}

export function formatBps(value: string | number | null | undefined, dp = 1): string {
  const dec = toDecimal(value)
  if (dec === null) return '—'
  return `${dec.toFixed(dp)} bps`
}

export function formatPercent(
  value: string | number | null | undefined,
  options: { dp?: number; signed?: boolean; alreadyMultiplied?: boolean } = {},
): string {
  const dec = toDecimal(value)
  if (dec === null) return '—'
  const scaled = options.alreadyMultiplied ? dec : dec.mul(100)
  const text = scaled.toFixed(options.dp ?? 2)
  return options.signed && scaled.isPositive() ? `+${text}%` : `${text}%`
}

export function compactNumber(value: number | null | undefined, dp = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  if (abs < 1_000) return value.toFixed(0)
  if (abs < 1_000_000) return `${(value / 1_000).toFixed(dp)}K`
  if (abs < 1_000_000_000) return `${(value / 1_000_000).toFixed(dp)}M`
  return `${(value / 1_000_000_000).toFixed(dp)}B`
}

export function pnlTone(value: string | number | null | undefined): 'pos' | 'neg' | 'neutral' {
  const dec = toDecimal(value)
  if (dec === null || dec.isZero()) return 'neutral'
  return dec.isPositive() ? 'pos' : 'neg'
}

/** 把 pnlTone 结果映射为 CSS 颜色。neutral 返回 undefined，不影响默认色。 */
export function pnlToneColor(tone: 'pos' | 'neg' | 'neutral'): string | undefined {
  if (tone === 'pos') return 'var(--color-success)'
  if (tone === 'neg') return 'var(--color-danger)'
  return undefined
}
