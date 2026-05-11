import { useEffect, useRef, useState } from 'react'
import { Button, Group, NumberInput, Select, Stack, Text } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { useTimeWindowStore } from '@core/time/store'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { TimeWindowPicker } from '@shared/time/TimeWindowPicker'
import { buildExportDownloadUrl } from '@core/api/client'
import { describeError } from '@core/api/errors'

const RESOURCE_OPTIONS = [
  { value: 'orders', label: 'orders' },
  { value: 'fills', label: 'fills' },
  { value: 'audit_events', label: 'audit_events' },
] as const

const FORMAT_OPTIONS = [
  { value: 'csv', label: 'CSV' },
  { value: 'jsonl', label: 'JSONL' },
] as const

type Resource = (typeof RESOURCE_OPTIONS)[number]['value']
type Format = (typeof FORMAT_OPTIONS)[number]['value']

export function ExportsPage() {
  const since = useTimeWindowStore((s) => s.since)
  const until = useTimeWindowStore((s) => s.until)
  const [resource, setResource] = useState<Resource>('orders')
  const [format, setFormat] = useState<Format>('csv')
  const [limit, setLimit] = useState(10000)
  const [downloading, setDownloading] = useState(false)
  const [lastError, setLastError] = useState<string | null>(null)

  // 切页时主动 abort 在飞下载——避免后台跑完后浏览器弹出过时的文件，
  // 也省后端 server-side cursor cycle。
  const abortRef = useRef<AbortController | null>(null)
  useEffect(() => () => abortRef.current?.abort(), [])

  const href = buildExportDownloadUrl(resource, {
    format,
    since: since ?? undefined,
    until: until ?? undefined,
    limit,
  })

  const handleDownload = async () => {
    setDownloading(true)
    setLastError(null)
    // 取消上一次仍在飞的下载（如果有）——同时点两次"开始下载"也只保留最新。
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    const startId = notifications.show({
      title: '导出中…',
      message: `${resource} · ${format.toUpperCase()} · limit=${limit}`,
      color: 'blue',
      loading: true,
      autoClose: false,
      withCloseButton: false,
    })
    try {
      const response = await fetch(href, { method: 'GET', signal: controller.signal })
      if (!response.ok) {
        const text = await response.text().catch(() => '')
        throw new Error(`${response.status} ${response.statusText}${text ? ` · ${text.slice(0, 120)}` : ''}`)
      }
      const warning = response.headers.get('x-export-warning')
      // server-side cursor 流式响应；blob() 会把整条流读完——超大导出会占内存。
      // 100k 行 CSV 大约 10–30MB，浏览器吃得下；后端已有 100k 上限。
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filenameFor(resource, format, since, until)
      document.body.appendChild(a)
      a.click()
      a.remove()
      // 异步释放，让浏览器有时间开始下载。
      setTimeout(() => URL.revokeObjectURL(url), 1000)
      notifications.update({
        id: startId,
        title: '导出完成',
        message: warning ? `已下载 · 后端警告：${warning}` : `已下载 ${resource}.${format}`,
        color: warning ? 'yellow' : 'teal',
        loading: false,
        autoClose: 4000,
        withCloseButton: true,
      })
    } catch (err) {
      // 主动 abort（切页 / 用户重点）不算失败——静默掉提示，避免误导。
      if (err instanceof DOMException && err.name === 'AbortError') {
        notifications.hide(startId)
        return
      }
      const msg = describeError(err)
      setLastError(msg)
      notifications.update({
        id: startId,
        title: '导出失败',
        message: msg,
        color: 'red',
        loading: false,
        autoClose: 6000,
        withCloseButton: true,
      })
    } finally {
      setDownloading(false)
      if (abortRef.current === controller) abortRef.current = null
    }
  }

  return (
    <>
      <PageHeader
        title="导出中心 Exports"
        subtitle="orders / fills / audit_events → CSV / JSONL 流式下载（后端 server-side cursor）"
      />
      <SectionCard>
        <Stack gap="sm">
          <Group gap="md" wrap="wrap" align="flex-end">
            <Select
              size="xs"
              label="resource"
              value={resource}
              data={[...RESOURCE_OPTIONS]}
              onChange={(v) => v && setResource(v as Resource)}
              w={160}
            />
            <Select
              size="xs"
              label="format"
              value={format}
              data={[...FORMAT_OPTIONS]}
              onChange={(v) => v && setFormat(v as Format)}
              w={120}
            />
            <NumberInput
              size="xs"
              label="limit"
              value={limit}
              onChange={(v) => setLimit(typeof v === 'number' ? v : 10000)}
              min={1}
              max={100_000}
              step={1000}
              w={150}
            />
          </Group>
          <TimeWindowPicker />
          <Text size="xs" c="dimmed">
            limit 超 100,000 后端会自动夹紧并在响应头 X-Export-Warning 中标注。
          </Text>
          <Group>
            <Button
              size="xs"
              color="accent"
              loading={downloading}
              onClick={() => void handleDownload()}
            >
              {downloading ? '导出中…' : '开始下载'}
            </Button>
            <Text size="xs" c="dimmed" ff="var(--font-mono)">
              {href}
            </Text>
          </Group>
          {lastError ? (
            <Text size="xs" c="red">
              上次失败：{lastError}
            </Text>
          ) : null}
        </Stack>
      </SectionCard>
    </>
  )
}

function filenameFor(resource: string, format: string, since: number | null, until: number | null): string {
  const sinceTag = since !== null ? String(since) : ''
  const untilTag = until !== null ? String(until) : ''
  return `${resource}_${sinceTag}_${untilTag}.${format}`
}
