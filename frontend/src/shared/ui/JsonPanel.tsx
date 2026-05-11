import { ActionIcon, CopyButton, Group, Tooltip } from '@mantine/core'
import { IconCheck, IconCopy } from '@tabler/icons-react'
import styles from './JsonPanel.module.css'

type Props = {
  value: unknown
  maxHeight?: number
}

export function JsonPanel({ value, maxHeight = 360 }: Props) {
  const text = stringify(value)
  return (
    <div className={styles.wrap} style={{ maxHeight }}>
      <Group justify="flex-end" mb={4}>
        <CopyButton value={text} timeout={1200}>
          {({ copied, copy }) => (
            <Tooltip label={copied ? '已复制' : '复制 JSON'} withArrow>
              <ActionIcon size="sm" variant="subtle" onClick={copy} aria-label="复制 JSON">
                {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
              </ActionIcon>
            </Tooltip>
          )}
        </CopyButton>
      </Group>
      <pre className={styles.pre}>{text}</pre>
    </div>
  )
}

function stringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}
