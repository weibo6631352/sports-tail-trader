import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import utc from 'dayjs/plugin/utc'

dayjs.extend(relativeTime)
dayjs.extend(utc)

export function formatIso(value: string | null | undefined, fmt = 'YYYY-MM-DD HH:mm:ss'): string {
  if (!value) return '—'
  const m = dayjs(value)
  if (!m.isValid()) return value
  return m.format(fmt)
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return '—'
  const m = dayjs(value)
  if (!m.isValid()) return value
  return m.fromNow()
}

export function formatEpochMs(ms: number | null | undefined, fmt = 'YYYY-MM-DD HH:mm:ss'): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—'
  return dayjs(ms).format(fmt)
}

export function isoToEpochMs(iso: string | null | undefined): number | null {
  if (!iso) return null
  const m = dayjs(iso)
  return m.isValid() ? m.valueOf() : null
}
