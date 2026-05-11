import type { ReactNode } from 'react'

// 表格密集单元格使用的小号等宽 code 块。fontSize 11 是 admin 表格事实标准，
// 抽出来避免 30+ 处 callsite 重复 style 字面量。
export function MonoCell({ children }: { children: ReactNode }) {
  return <code style={{ fontSize: 11 }}>{children}</code>
}

// dim 版本：弱化的 reason / 默认值文案，背景色与 MonoCell 一致。
export function DimMonoCell({ children }: { children: ReactNode }) {
  return <code style={{ fontSize: 11, color: 'var(--color-text-dim)' }}>{children}</code>
}

// 非 code 的等宽 span：when 列等需要等宽对齐但不需要代码视觉块的位置。
export function MonoText({ children }: { children: ReactNode }) {
  return <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11 }}>{children}</span>
}

// 弱化文本占位（多用于 "—"）。
export function DimText({ children }: { children: ReactNode }) {
  return <span style={{ color: 'var(--color-text-dim)' }}>{children}</span>
}
