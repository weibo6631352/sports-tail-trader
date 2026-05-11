import { Badge, Group, Stack, Text } from '@mantine/core'
import clsx from 'clsx'
import type { ReactNode } from 'react'
import type { TimelineEvent } from '@core/api/types'
import { CopyableId } from '@shared/ui/CopyableId'
import { formatIso } from '@shared/format'
import { formatDecimal, formatUsdc } from '@shared/format'
import styles from './TimelineEventNode.module.css'

type Props = {
  event: TimelineEvent
  onOpenDetail?: (event: TimelineEvent) => void
}

const KIND_LABEL: Record<string, string> = {
  decision: '决策',
  order: '订单',
  fill: '成交',
  audit: '审计',
  outbox: 'outbox',
  sports: '体育',
}

const KIND_TONE: Record<string, string> = {
  decision: 'blue',
  order: 'teal',
  fill: 'accent',
  audit: 'yellow',
  outbox: 'gray',
  sports: 'cyan',
}

// 后端 trade timeline 把 sports_live_state_recorded 当 audit event 推上来。
// 前端识别后用专属节点渲染——比分 / 时钟 / signal_allowed 比"通用 audit"更直观。
const SPORTS_EVENT_TITLE = 'sports_live_state_recorded'

function isSportsEvent(event: TimelineEvent): boolean {
  return event.kind === 'audit' && event.event_title === SPORTS_EVENT_TITLE
}

export function TimelineEventNode({ event, onOpenDetail }: Props) {
  const effectiveKind = isSportsEvent(event) ? 'sports' : event.kind
  return (
    <div
      className={clsx(styles.row, onOpenDetail && styles.clickable)}
      onClick={onOpenDetail ? () => onOpenDetail(event) : undefined}
    >
      <div className={styles.dotCol}>
        <div className={clsx(styles.dot, styles[`dot_${effectiveKind}`])} />
        <div className={styles.line} />
      </div>
      <div className={styles.content}>
        <Group gap="xs">
          <Badge color={KIND_TONE[effectiveKind] ?? 'gray'} size="xs">
            {KIND_LABEL[effectiveKind] ?? effectiveKind}
          </Badge>
          <Text size="xs" c="dimmed" ff="var(--font-mono)" title={formatIso(event.timestamp)}>
            {formatIso(event.timestamp, 'MM-DD HH:mm:ss')}
          </Text>
          {event.trace_id ? <CopyableId value={event.trace_id} label="trace" dense /> : null}
        </Group>
        <div className={styles.body}>{renderBody(event)}</div>
      </div>
    </div>
  )
}

function renderBody(event: TimelineEvent): ReactNode {
  switch (event.kind) {
    case 'decision':
      return (
        <Stack gap={2}>
          <Text size="sm">
            <code>{event.hook_name ?? 'decision'}</code> →{' '}
            <span style={{ color: event.accepted ? 'var(--color-success)' : 'var(--color-danger)' }}>
              {event.accepted ? 'accepted' : 'rejected'}
            </span>
            {event.reason ? ` · ${event.reason}` : ''}
          </Text>
          <Text size="xs" c="dimmed">
            fair_value={formatDecimal(stringOf(event.decision_output, 'fair_value'), { dp: 4 })} · entry_price_cap=
            {formatDecimal(stringOf(event.decision_output, 'entry_price_cap'), { dp: 4 })} · edge=
            {formatDecimal(stringOf(event.decision_output, 'edge_bps'), { dp: 1, suffix: ' bps' })}
          </Text>
        </Stack>
      )
    case 'order':
      return (
        <Stack gap={2}>
          <Text size="sm">
            {event.side} · status={event.status} · price={formatDecimal(event.price, { dp: 4 })}
          </Text>
          <Text size="xs" c="dimmed">
            filled={formatDecimal(event.filled_shares, { dp: 2 })} / remaining=
            {formatDecimal(event.remaining_shares, { dp: 2 })} · notional={formatUsdc(event.notional_usdc)}
          </Text>
        </Stack>
      )
    case 'fill':
      return (
        <Text size="sm">
          {event.side?.toUpperCase() ?? ''} {formatDecimal(event.size, { dp: 2 })} @ {formatDecimal(event.price, { dp: 4 })} · notional=
          {formatUsdc(event.notional_usdc)}
        </Text>
      )
    case 'audit':
      if (event.event_title === SPORTS_EVENT_TITLE) {
        // 后端 sports_live_state_worker payload 结构：
        //   { source, observed_at, signal_allowed, signal_reason, phase, match_payload: {...} }
        // score / clock 等比赛细节嵌在 match_payload 内（strategy-defined）。
        const payload = (event.full?.payload ?? {}) as Record<string, unknown>
        const allowed = payload.signal_allowed as boolean | null | undefined
        const phase = payload.phase as string | undefined
        const reason = payload.signal_reason as string | undefined
        const matchPayload = (payload.match_payload ?? {}) as Record<string, unknown>
        const clockRaw = matchPayload.clock ?? payload.clock
        const clock = typeof clockRaw === 'string' ? clockRaw : null
        const scoreRaw = matchPayload.score ?? payload.score
        const score =
          scoreRaw && typeof scoreRaw === 'object'
            ? JSON.stringify(scoreRaw).slice(0, 60)
            : typeof scoreRaw === 'string'
              ? scoreRaw
              : null
        return (
          <Stack gap={2}>
            <Text size="sm">
              {phase ?? 'live'} · signal=
              <span
                style={{
                  color:
                    allowed === true
                      ? 'var(--color-success)'
                      : allowed === false
                        ? 'var(--color-danger)'
                        : 'var(--color-text-dim)',
                }}
              >
                {String(allowed ?? 'unknown')}
              </span>
              {reason ? ` · ${reason}` : ''}
            </Text>
            {clock || score ? (
              <Text size="xs" c="dimmed" ff="var(--font-mono)">
                {clock ? `clock=${clock}` : ''}
                {clock && score ? ' · ' : ''}
                {score ? `score=${score}` : ''}
              </Text>
            ) : null}
          </Stack>
        )
      }
      return (
        <Stack gap={2}>
          <Text size="sm">{event.event_title ?? '—'}</Text>
          {event.reason ? (
            <Text size="xs" c="dimmed">
              {event.reason}
            </Text>
          ) : null}
        </Stack>
      )
    case 'outbox':
      return (
        <Stack gap={2}>
          <Text size="sm">
            {event.event_type ?? '—'}
            {typeof event.retry_count === 'number' && event.retry_count > 0 ? (
              <span style={{ color: 'var(--color-warning)' }}> · retry={event.retry_count}</span>
            ) : null}
          </Text>
          {event.last_error ? (
            <Text size="xs" c="red">
              {event.last_error}
            </Text>
          ) : null}
        </Stack>
      )
  }
}

function stringOf(payload: Record<string, unknown> | undefined, key: string): string | null {
  if (!payload) return null
  const v = payload[key]
  if (v === undefined || v === null) return null
  return String(v)
}
