import { ActionIcon, CopyButton, Tooltip } from '@mantine/core'
import { IconCheck, IconCopy } from '@tabler/icons-react'
import { truncateId } from '@shared/format'
import styles from './CopyableId.module.css'

type Props = {
  value: string | null | undefined
  head?: number
  tail?: number
  label?: string
}

export function CopyableId({ value, head = 6, tail = 4, label }: Props) {
  if (!value) return <span className={styles.empty}>—</span>
  const display = truncateId(value, head, tail)
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
