import { Group, Text, Tooltip } from '@mantine/core'
import { useSseStatus } from '@core/sse'
import { formatEpochMs } from '@shared/format'
import styles from './SseStatusBadge.module.css'

const TONE_BY_STATE: Record<string, string> = {
  idle: '#97a6c2',
  connecting: '#f0b955',
  open: '#38d39f',
  reconnecting: '#f0b955',
  rate_limited: '#f06568',
  closed: '#97a6c2',
}

export function SseStatusBadge() {
  const status = useSseStatus()
  const color = TONE_BY_STATE[status.state] ?? '#97a6c2'
  const labels: Record<string, string> = {
    idle: '未连接',
    connecting: '连接中…',
    open: 'SSE 已连',
    reconnecting: '重连中',
    rate_limited: '订阅已满',
    closed: '已关闭',
  }
  const tooltip = [
    `state: ${status.state}`,
    `url: ${status.url ?? '—'}`,
    status.lastEventAt ? `last event: ${formatEpochMs(status.lastEventAt)}` : null,
    status.lastError ? `last error: ${status.lastError}` : null,
    status.droppedEventsTotal ? `dropped: ${status.droppedEventsTotal}` : null,
    status.retryAfterSeconds ? `retry after: ${status.retryAfterSeconds}s` : null,
  ]
    .filter(Boolean)
    .join('\n')
  return (
    <Tooltip label={<pre className={styles.tooltipPre}>{tooltip}</pre>} multiline withArrow>
      <Group gap={6} className={styles.badge}>
        <span className={styles.dot} style={{ background: color }} />
        <Text size="xs" c="dimmed">
          {labels[status.state] ?? status.state}
        </Text>
      </Group>
    </Tooltip>
  )
}
