import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react'
import clsx from 'clsx'
import styles from './InlineActionButton.module.css'

// 表格单元 / 抽屉行级紧凑按钮——15+ 处页面之前各自 inline style，统一抽出来。
// 不用 Mantine Button：尺寸太大、padding 不能压到 2px。
// variant:
//   - accent: 实心 accent（高强调，如「查询」「运行」「应用」）
//   - link:   透明 + accent border（次级链接式，如「详情」「展开」「全部」）
//   - danger: 透明 + danger border（破坏性入口，如「cancel」「force exit」）
//   - warning: 透明 + warning border（暂停 / 中风险）

export type InlineButtonVariant = 'accent' | 'link' | 'danger' | 'warning'

export type InlineActionButtonProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'type'> & {
  variant?: InlineButtonVariant
  children: ReactNode
}

export const InlineActionButton = forwardRef<HTMLButtonElement, InlineActionButtonProps>(
  function InlineActionButton({ variant = 'link', className, children, ...rest }, ref) {
    return (
      <button
        ref={ref}
        type="button"
        className={clsx(styles.btn, styles[`btn_${variant}`], className)}
        {...rest}
      >
        {children}
      </button>
    )
  },
)
