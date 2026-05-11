import { ActionIcon, CopyButton, Tooltip } from '@mantine/core'
import { IconCheck, IconCopy } from '@tabler/icons-react'
import { truncateId } from '@shared/format'
import styles from './CopyableId.module.css'

type Props = {
  value: string | null | undefined
  head?: number
  tail?: number
  /** 表格密集场景走 4/4；默认 6/4 适合 detail / overview。 */
  dense?: boolean
  label?: string
}

export function CopyableId({ value, head, tail, dense, label }: Props) {
  if (!value) return <span className={styles.empty}>—</span>
  const h = head ?? (dense ? 4 : 6)
  const t = tail ?? (dense ? 4 : 4)
  const display = truncateId(value, h, t)
  return (
    <span className={styles.wrap}>
      <Tooltip label={value} withArrow openDelay={300} position="top">
        <code className={styles.code}>{label ? `${label}: ${display}` : display}</code>
      </Tooltip>
      <CopyButton value={value} timeout={1200}>
        {({ copied, copy }) => (
          <Tooltip label={copied ? '已复制' : '复制'} withArrow>
            <ActionIcon
              size="xs"
              variant="subtle"
              color={copied ? 'teal' : 'gray'}
              onClick={copy}
              aria-label="复制"
            >
              {copied ? <IconCheck size={12} /> : <IconCopy size={12} />}
            </ActionIcon>
          </Tooltip>
        )}
      </CopyButton>
    </span>
  )
}
