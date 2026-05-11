import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Badge,
  Button,
  Group,
  NumberInput,
  SimpleGrid,
  Stack,
  Text,
  TextInput,
  Tabs,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconAlertTriangle } from '@tabler/icons-react'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { parametersApi, auditEventsApi } from '@core/api/resources'
import { ApiError, describeError } from '@core/api/errors'
import type { AuditEventRow, ParameterRegistryEntry } from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { StatusPill } from '@shared/ui/StatusPill'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { CopyableId } from '@shared/ui/CopyableId'
import { DataTable } from '@shared/tables/DataTable'
import { confirmAction } from '@shared/forms/confirmAction'
import { type DiffRow } from '@shared/forms/DiffPreview'
import { formatIso } from '@shared/format'
import { getParamMetadata, type ParamMetadata } from './paramMetadata'

// 实时调参：
//   1. /parameters 返回 registry（白名单）+ 当前 override 状态
//   2. 表单按 metadata.group 分组、按风险标徽章
//   3. set 流程：编辑 → confirmAction(diff + operator + reason 必填) → PUT
//   4. clear 流程：confirmAction → DELETE → 回到 Settings/策略默认
//   5. 历史从 /audit-events filter event_title='parameter_override_applied' 拉

const AUDIT_EVENT_TITLE = 'parameter_override_applied'

export function StrategyConfigPage() {
  const client = useQueryClient()

  const registry = useQuery({
    queryKey: qk.parameters.list(),
    queryFn: ({ signal }) => parametersApi.list(signal),
    retry: false,
  })

  const history = useQuery({
    queryKey: qk.auditEvents.list({ event_title: AUDIT_EVENT_TITLE, limit: 30 }),
    queryFn: ({ signal }) => auditEventsApi.list({ event_title: AUDIT_EVENT_TITLE, limit: 30 }, signal),
    retry: false,
  })

  const offline = registry.error instanceof ApiError && registry.error.isUnavailable()

  // 把 entries 解构挪进 useMemo，避免每次 render 都产生新数组使依赖不稳定。
  const grouped = useMemo(() => {
    const entries = registry.data?.parameters ?? []
    const m = new Map<string, ParameterRegistryEntry[]>()
    for (const entry of entries) {
      const group = getParamMetadata(entry.scope, entry.key).group
      const list = m.get(group)
      if (list) list.push(entry)
      else m.set(group, [entry])
    }
    return Array.from(m.entries()).sort(([a], [b]) => a.localeCompare(b))
  }, [registry.data?.parameters])
  // 总览数字卡仍需要 entries 数组——直接复用 grouped 推导，节省一次扫描。
  const entries = useMemo(() => grouped.flatMap(([, list]) => list), [grouped])

  const setMutation = useMutation({
    mutationFn: (params: {
      scope: string
      key: string
      value: unknown
      operator: string
      reason: string
      trace_id: string
    }) =>
      parametersApi.set(params.scope, params.key, {
        value: params.value,
        operator: params.operator,
        reason: params.reason,
        trace_id: params.trace_id,
      }),
    onSuccess: (data) => {
      notifications.show({
        title: `${data.scope}.${data.key} 已生效`,
        message: `value=${String(data.value)} · operator=${data.operator}`,
        color: 'teal',
      })
      client.invalidateQueries({ queryKey: qk.parameters.list() })
      client.invalidateQueries({ queryKey: qk.auditEvents.list({ event_title: AUDIT_EVENT_TITLE, limit: 30 }) })
    },
    onError: (err) => notifications.show({ title: '写入失败', message: describeError(err), color: 'red' }),
  })

  const clearMutation = useMutation({
    mutationFn: (params: { scope: string; key: string; operator: string; reason: string; trace_id: string }) =>
      parametersApi.clear(params.scope, params.key, {
        operator: params.operator,
        reason: params.reason,
        trace_id: params.trace_id,
      }),
    onSuccess: (data) => {
      notifications.show({
        title: `${data.scope}.${data.key} 已清除`,
        message: `回到 Settings / 策略默认；previous=${String(data.previous_value ?? '∅')}`,
        color: 'yellow',
      })
      client.invalidateQueries({ queryKey: qk.parameters.list() })
      client.invalidateQueries({ queryKey: qk.auditEvents.list({ event_title: AUDIT_EVENT_TITLE, limit: 30 }) })
    },
    onError: (err) => notifications.show({ title: '清除失败', message: describeError(err), color: 'red' }),
  })

  const activeOverrideCount = registry.data?.active_override_count ?? 0
  const totalCount = entries.length

  return (
    <>
      <PageHeader
        title="策略参数热调"
        subtitle="后端 /parameters · 重启即丢 · 写入自动落 parameter_override_applied 审计"
        actions={
          <Group gap={6}>
            <Badge variant="light" color="gray">
              registry: {totalCount}
            </Badge>
            <Badge variant="light" color={activeOverrideCount > 0 ? 'teal' : 'gray'}>
              active overrides: {activeOverrideCount}
            </Badge>
          </Group>
        }
      />

      {offline ? (
        <Alert color="yellow" icon={<IconAlertTriangle size={16} />} mb="md">
          后端 <code>/parameters</code> 不可用（503）。
        </Alert>
      ) : registry.error ? (
        <QueryErrorNotice error={registry.error} onRetry={() => registry.refetch()} />
      ) : null}

      <Tabs defaultValue="registry">
        <Tabs.List>
          <Tabs.Tab value="registry">参数 ({totalCount})</Tabs.Tab>
          <Tabs.Tab value="history">变更历史</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="registry" pt="sm">
          <Stack gap="md">
            {grouped.length === 0 && !registry.isLoading ? (
              <Text c="dimmed" size="sm">
                后端 registry 为空。
              </Text>
            ) : null}
            {grouped.map(([group, fields]) => (
              <SectionCard key={group} title={group} description={`${fields.length} 个字段`}>
                <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
                  {fields.map((entry) => (
                    <ParamEditor
                      key={`${entry.scope}.${entry.key}`}
                      entry={entry}
                      onSet={(value) =>
                        promptSet(entry, value, (params) => setMutation.mutateAsync(params))
                      }
                      onClear={() => promptClear(entry, (params) => clearMutation.mutateAsync(params))}
                    />
                  ))}
                </SimpleGrid>
              </SectionCard>
            ))}
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="history" pt="sm">
          <HistoryTable history={history} />
        </Tabs.Panel>
      </Tabs>
    </>
  )
}

function ParamEditor({
  entry,
  onSet,
  onClear,
}: {
  entry: ParameterRegistryEntry
  onSet: (value: string) => void
  onClear: () => void
}) {
  const meta = getParamMetadata(entry.scope, entry.key)
  const currentValue = entry.override?.value ?? null
  const currentText = stringValue(currentValue)
  const [draft, setDraft] = useState<string>(currentText)
  const hasOverride = entry.override !== null
  const dirty = draft.trim() !== '' && draft !== currentText

  return (
    <SectionCard
      title={
        <Group gap={6}>
          <Text fw={600} size="sm" ff="var(--font-mono)">
            {entry.scope}.{entry.key}
          </Text>
          <RiskBadge risk={meta.risk} />
          {hasOverride ? (
            <Badge color="teal" size="xs" variant="light">
              override
            </Badge>
          ) : null}
        </Group>
      }
      description={
        <Stack gap={2}>
          <Text size="xs">{entry.description}</Text>
          {meta.hint ? (
            <Text size="xs" c="dimmed">
              {meta.hint}
            </Text>
          ) : null}
        </Stack>
      }
    >
      <Stack gap={6}>
        <Group gap="xs" align="flex-end">
          {renderInput(meta, draft, setDraft)}
          <Button
            size="xs"
            color={meta.risk === 'high' ? 'red' : 'accent'}
            disabled={!dirty}
            onClick={() => onSet(draft.trim())}
          >
            Set
          </Button>
          {hasOverride ? (
            <Button size="xs" variant="default" onClick={onClear}>
              Clear
            </Button>
          ) : null}
        </Group>
        <Group justify="space-between" gap="xs">
          <Text size="xs" c="dimmed">
            当前生效：<code>{currentText || '∅ (Settings/策略默认)'}</code>
          </Text>
          {entry.override ? (
            <Text size="xs" c="dimmed">
              {entry.override.operator} · {formatIso(entry.override.applied_at, 'MM-DD HH:mm:ss')}
            </Text>
          ) : null}
        </Group>
      </Stack>
    </SectionCard>
  )
}

function renderInput(meta: ParamMetadata, value: string, onChange: (v: string) => void) {
  // integer / bps：走 NumberInput 但传 string value——避免 Number(value) 截 IEEE754 精度。
  // decimal / price_0_to_1 / usdc：一律用 TextInput 保留任意小数位字符串原样写入；
  // Decimal as string 是后端契约，NumberInput 的 number 中转必丢精度。
  if (meta.inputHint === 'integer' || meta.inputHint === 'bps') {
    return (
      <NumberInput
        size="xs"
        value={value}
        onChange={(v) => onChange(typeof v === 'number' ? String(v) : String(v ?? ''))}
        min={0}
        step={meta.inputHint === 'bps' ? 10 : 1}
        w={180}
        allowDecimal={false}
      />
    )
  }
  const placeholder =
    meta.inputHint === 'price_0_to_1'
      ? '0–1 Decimal（如 0.5500）'
      : meta.inputHint === 'usdc'
        ? 'USDC（Decimal as string）'
        : meta.inputHint === 'decimal'
          ? 'Decimal as string'
          : 'value'
  return (
    <TextInput
      size="xs"
      value={value}
      onChange={(e) => onChange(e.currentTarget.value)}
      placeholder={placeholder}
      w={180}
    />
  )
}

function RiskBadge({ risk }: { risk: ParamMetadata['risk'] }) {
  if (risk === 'high') return <Badge size="xs" color="red">高风险</Badge>
  if (risk === 'medium') return <Badge size="xs" color="yellow">中</Badge>
  return <Badge size="xs" color="gray">低</Badge>
}

function promptSet(
  entry: ParameterRegistryEntry,
  newValue: string,
  submit: (params: {
    scope: string
    key: string
    value: unknown
    operator: string
    reason: string
    trace_id: string
  }) => Promise<unknown>,
) {
  const meta = getParamMetadata(entry.scope, entry.key)
  const before = entry.override?.value ?? '(default)'
  const diff: DiffRow[] = [
    {
      field: `${entry.scope}.${entry.key}`,
      before,
      after: newValue,
      risk: meta.risk,
      hint: entry.description,
    },
  ]
  confirmAction({
    title: `Set ${entry.scope}.${entry.key}`,
    description: '生效后立即影响策略 / 风控，重启即丢；高风险变更请明确 reason。',
    tone: meta.risk === 'high' ? 'danger' : 'warning',
    diff,
    onConfirm: async ({ operator, reason, trace_id }) =>
      submit({
        scope: entry.scope,
        key: entry.key,
        value: newValue,
        operator,
        reason,
        trace_id,
      }),
  })
}

function promptClear(
  entry: ParameterRegistryEntry,
  submit: (params: { scope: string; key: string; operator: string; reason: string; trace_id: string }) => Promise<unknown>,
) {
  const meta = getParamMetadata(entry.scope, entry.key)
  confirmAction({
    title: `Clear ${entry.scope}.${entry.key}`,
    description: '清除后该参数回到 Settings / 策略默认值；不会修改 .env 或代码。',
    tone: meta.risk === 'high' ? 'danger' : 'warning',
    diff: [
      {
        field: `${entry.scope}.${entry.key}`,
        before: entry.override?.value ?? '(default)',
        after: '(default)',
        risk: meta.risk,
      },
    ],
    onConfirm: async ({ operator, reason, trace_id }) =>
      submit({
        scope: entry.scope,
        key: entry.key,
        operator,
        reason,
        trace_id,
      }),
  })
}

function HistoryTable({
  history,
}: {
  history: ReturnType<typeof useQuery<{ items: AuditEventRow[] }, Error>>
}) {
  const columns: ColumnDef<AuditEventRow, unknown>[] = [
    {
      header: 'when',
      cell: ({ row }) => formatIso(row.original.created_at, 'MM-DD HH:mm:ss'),
    },
    {
      header: 'scope.key',
      cell: ({ row }) => {
        const p = (row.original.payload ?? {}) as Record<string, unknown>
        return (
          <code style={{ fontSize: 11 }}>
            {String(p.scope ?? '?')}.{String(p.key ?? '?')}
          </code>
        )
      },
    },
    {
      header: '变更',
      cell: ({ row }) => {
        const p = (row.original.payload ?? {}) as Record<string, unknown>
        const cleared = Boolean(p.cleared)
        return (
          <Group gap={6} ff="var(--font-mono)">
            <code style={beforeStyle}>{String(p.previous_value ?? '∅')}</code>
            <Text size="xs" c="dimmed">
              →
            </Text>
            <code style={afterStyle}>{cleared ? '(cleared)' : String(p.new_value ?? '∅')}</code>
          </Group>
        )
      },
    },
    {
      header: 'operator',
      cell: ({ row }) => {
        const p = (row.original.payload ?? {}) as Record<string, unknown>
        return <span>{String(p.operator ?? row.original.operator ?? '—')}</span>
      },
    },
    {
      header: 'reason',
      cell: ({ row }) => (
        <code style={{ fontSize: 11, color: 'var(--color-text-dim)' }}>
          {row.original.reason ?? '—'}
        </code>
      ),
    },
    {
      header: 'cleared',
      cell: ({ row }) => {
        const p = (row.original.payload ?? {}) as Record<string, unknown>
        return p.cleared ? (
          <StatusPill tone="warning" size="xs">
            yes
          </StatusPill>
        ) : (
          <span style={{ color: 'var(--color-text-dim)' }}>no</span>
        )
      },
    },
    {
      header: 'trace',
      cell: ({ row }) => <CopyableId value={row.original.trace_id ?? ''} dense />,
    },
  ]

  return (
    <DataTable<AuditEventRow>
      columns={columns}
      data={history.data?.items}
      isLoading={history.isLoading}
      isFetching={history.isFetching}
      error={history.error}
      onRefresh={() => history.refetch()}
      rowKey={(r) => r.event_id}
    />
  )
}

function stringValue(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

const beforeStyle: React.CSSProperties = {
  color: 'var(--color-text-dim)',
  textDecoration: 'line-through',
  padding: '1px 6px',
  background: 'rgba(240, 101, 104, 0.08)',
  borderRadius: 4,
  fontSize: 11,
}

const afterStyle: React.CSSProperties = {
  color: 'var(--color-accent)',
  padding: '1px 6px',
  background: 'rgba(92, 217, 197, 0.1)',
  borderRadius: 4,
  fontSize: 11,
}
