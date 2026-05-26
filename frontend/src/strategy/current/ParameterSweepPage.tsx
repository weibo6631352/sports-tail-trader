import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  Alert,
  Badge,
  Button,
  Group,
  NumberInput,
  SimpleGrid,
  Stack,
  Text,
  Textarea,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconAlertTriangle, IconPlayerPlay, IconTrophy } from '@tabler/icons-react'
import type { ColumnDef } from '@tanstack/react-table'
import { qk } from '@core/api/keys'
import { operationsApi, parametersApi } from '@core/api/resources'
import { describeError } from '@core/api/errors'
import type {
  ParameterRegistryEntry,
  ParameterSweepRequest,
  ParameterSweepResponse,
  SweepCandidateResult,
  SweepParameterKey,
  SweepParamSpec,
} from '@core/api/types'
import { PageHeader } from '@shared/ui/PageHeader'
import { SectionCard } from '@shared/ui/SectionCard'
import { EmptyState } from '@shared/ui/EmptyState'
import { QueryErrorNotice } from '@shared/ui/QueryErrorNotice'
import { InlineActionButton } from '@shared/ui/InlineActionButton'
import { MonoCell } from '@shared/ui/MonoCell'
import { DataTable } from '@shared/tables/DataTable'
import { TimeWindowPicker } from '@shared/time/TimeWindowPicker'
import { confirmAction } from '@shared/forms/confirmAction'
import { type DiffRow } from '@shared/forms/DiffPreview'
import { formatUsdc, pnlTone } from '@shared/format'
import { useTimeWindowStore } from '@core/time/store'

type SweepInputs = Record<SweepParameterKey, string>

function parseCandidates(inputs: SweepInputs, paramSpecs: SweepParamSpec[]): {
  candidates: Partial<Record<SweepParameterKey, Array<number | string>>>
  gridSize: number
  errors: Array<{ key: SweepParameterKey; message: string }>
} {
  const out: Partial<Record<SweepParameterKey, Array<number | string>>> = {}
  const errors: Array<{ key: SweepParameterKey; message: string }> = []
  let gridSize = 1
  let anyKey = false
  for (const meta of paramSpecs) {
    const raw = inputs[meta.key].trim()
    if (!raw) continue
    anyKey = true
    const parts = raw
      .split(/[,，\s]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    if (parts.length === 0) continue
    const normalized: Array<number | string> = []
    for (const p of parts) {
      if (meta.type === 'int') {
        const n = Number(p)
        if (!Number.isFinite(n) || !Number.isInteger(n)) {
          errors.push({ key: meta.key, message: `'${p}' 不是整数` })
          continue
        }
        normalized.push(n)
      } else {
        // decimal: 后端用 Decimal(str(value))，可传字符串保精度
        if (!/^-?\d+(\.\d+)?$/.test(p)) {
          errors.push({ key: meta.key, message: `'${p}' 不是合法 decimal` })
          continue
        }
        normalized.push(p)
      }
    }
    if (normalized.length === 0) continue
    // 去重：'300, 400, 300' → 后端没必要乘 3。string 形态比 Decimal 等价更宽容，
    // 但同字符串去重已足够；数字也按字符串比较保稳。
    const seen = new Set<string>()
    const unique = normalized.filter((v) => {
      const key = String(v)
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    out[meta.key] = unique
    gridSize *= unique.length
  }
  if (!anyKey) gridSize = 0
  return { candidates: out, gridSize, errors }
}

export function ParameterSweepPage() {
  const since = useTimeWindowStore((s) => s.since)
  const until = useTimeWindowStore((s) => s.until)

  const paramSpecsQuery = useQuery({
    queryKey: ['parameter-sweep-params'],
    queryFn: ({ signal }) => operationsApi.parameterSweepParams(signal),
    staleTime: 60_000,
  })
  const registryQuery = useQuery({
    queryKey: qk.parameters.list(),
    queryFn: ({ signal }) => parametersApi.list(signal),
    staleTime: 30_000,
  })
  const registry = useMemo(() => registryQuery.data?.parameters ?? [], [registryQuery.data])
  // 稳定引用：paramSpecsQuery.data ?? [] 每次渲染都产生新数组，useMemo 避免下游依赖抖动
  const paramSpecs = useMemo(() => paramSpecsQuery.data ?? [], [paramSpecsQuery.data])

  // 初始候选 = 当前生效值（override 优先，否则默认值），让 sweep 默认包含
  // "当前参数" 作为基线对照；用户可在此基础上加变体或清空。
  const seedInputs = useMemo(() => {
    return Object.fromEntries(
      paramSpecs.map((s) => {
        const regEntry = registry.find((e) => e.scope === s.scope && e.key === s.key)
        const current = regEntry?.override?.value ?? regEntry?.default_value ?? null
        return [s.key, current !== null ? String(current) : '']
      }),
    ) as SweepInputs
  }, [paramSpecs, registry])
  const [inputs, setInputs] = useState<SweepInputs>({
    outright_min_edge_bps: '',
    outright_max_entry_price: '',
    outright_min_orderbook_depth_usdc: '',
    entry_no_price_max: '',
  })
  // seedInputs 是 useMemo——paramSpecs 或 registry 变化时引用更新。
  // render 期间同步派生，避免 useEffect 级联渲染。
  const [prevSeed, setPrevSeed] = useState<SweepInputs | null>(null)
  if (paramSpecs.length > 0 && prevSeed !== seedInputs) {
    setPrevSeed(seedInputs)
    setInputs(seedInputs)
  }
  const [perDecisionUsdc, setPerDecisionUsdc] = useState(10)
  const [decisionLimit, setDecisionLimit] = useState(2000)
  const [settlementLimit, setSettlementLimit] = useState(2000)
  const [result, setResult] = useState<ParameterSweepResponse | null>(null)

  const parsed = useMemo(() => parseCandidates(inputs, paramSpecs), [inputs, paramSpecs])
  const gridOverLimit = parsed.gridSize > 1000
  const hasParseError = parsed.errors.length > 0
  const canRun = parsed.gridSize > 0 && !gridOverLimit && !hasParseError

  const sweep = useMutation({
    mutationFn: (body: ParameterSweepRequest) => operationsApi.parameterSweep(body),
    onSuccess: (data) => {
      setResult(data)
      notifications.show({
        title: 'sweep 完成',
        message: `${data.candidate_count} 组合 × ${data.scorable_decision_count} 可评分决策`,
        color: 'teal',
      })
    },
    onError: (err) => {
      notifications.show({ title: 'sweep 失败', message: describeError(err), color: 'red' })
    },
  })

  const handleRun = () => {
    if (!canRun) return
    const body: ParameterSweepRequest = {
      candidates: parsed.candidates,
      per_decision_usdc: perDecisionUsdc,
      since: since ?? undefined,
      until: until ?? undefined,
      decision_limit: decisionLimit,
      settlement_limit: settlementLimit,
    }
    sweep.mutate(body)
  }

  return (
    <>
      <PageHeader
        title="参数扫描 Parameter Sweep"
        subtitle="离线回放历史决策；不下单、不改 ParameterStore；笛卡尔积上限 1000"
        actions={
          <Badge size="sm" color={gridOverLimit ? 'red' : parsed.gridSize > 0 ? 'teal' : 'gray'} variant="light">
            grid: {parsed.gridSize} {gridOverLimit ? '（超上限）' : ''}
          </Badge>
        }
      />

      <Stack gap="md">
        <SectionCard title="候选参数" description="每行多个候选值，逗号 / 空格 / 换行分隔；留空 = 不扫该字段">
          <Stack gap="sm">
            {paramSpecs.map((meta) => {
              const fieldErrors = parsed.errors.filter((e) => e.key === meta.key)
              const regEntry = registry.find((e) => e.scope === meta.scope && e.key === meta.key)
              const currentEffective = regEntry?.override?.value ?? regEntry?.default_value ?? null
              const hasOverride = regEntry?.override != null
              return (
                <Group key={meta.key} gap="sm" wrap="wrap" align="flex-start">
                  <Stack gap={2} style={{ minWidth: 280 }}>
                    <Text size="sm" fw={500} ff="var(--font-mono)">
                      {meta.key}
                    </Text>
                    <Text size="xs" c="dimmed">
                      {meta.label}
                    </Text>
                    <Text size="xs" c="dimmed">
                      {meta.input_hint}
                    </Text>
                    <Text size="xs" c="dimmed">
                      当前：<code>{currentEffective !== null ? String(currentEffective) : '—'}</code>
                      {currentEffective !== null ? (
                        <span style={{ marginLeft: 4, fontSize: 10 }}>
                          {hasOverride ? '(override)' : '(默认)'}
                        </span>
                      ) : null}
                    </Text>
                  </Stack>
                  <Textarea
                    size="xs"
                    style={{ flex: 1, minWidth: 280 }}
                    placeholder={`例如：${meta.example}`}
                    value={inputs[meta.key] ?? ''}
                    onChange={(e) => setInputs({ ...inputs, [meta.key]: e.currentTarget.value })}
                    autosize
                    minRows={1}
                    error={fieldErrors.length > 0 ? fieldErrors.map((e) => e.message).join('；') : undefined}
                  />
                </Group>
              )
            })}
          </Stack>
        </SectionCard>

        <SectionCard title="运行参数">
          <Group justify="space-between" wrap="wrap" align="flex-end">
            <Group gap="sm" wrap="wrap" align="flex-end">
              <TimeWindowPicker />
              <NumberInput
                size="xs"
                label="per_decision_usdc"
                value={perDecisionUsdc}
                onChange={(v) => setPerDecisionUsdc(typeof v === 'number' ? v : 10)}
                min={0.01}
                max={100_000}
                step={1}
                w={150}
              />
              <NumberInput
                size="xs"
                label="decision_limit"
                value={decisionLimit}
                onChange={(v) => setDecisionLimit(typeof v === 'number' ? v : 2000)}
                min={1}
                max={20_000}
                step={500}
                w={140}
              />
              <NumberInput
                size="xs"
                label="settlement_limit"
                value={settlementLimit}
                onChange={(v) => setSettlementLimit(typeof v === 'number' ? v : 2000)}
                min={1}
                max={20_000}
                step={500}
                w={140}
              />
            </Group>
            <Button
              size="sm"
              color="accent"
              leftSection={<IconPlayerPlay size={14} />}
              disabled={!canRun}
              loading={sweep.isPending}
              onClick={handleRun}
            >
              运行 sweep
            </Button>
          </Group>
          {gridOverLimit ? (
            <Alert color="red" mt="sm" icon={<IconAlertTriangle size={14} />}>
              笛卡尔积 {parsed.gridSize} 超过上限 1000；缩小某一维度的候选数。
            </Alert>
          ) : null}
        </SectionCard>

        {sweep.error ? <QueryErrorNotice error={sweep.error} onRetry={handleRun} /> : null}

        {!result ? (
          <EmptyState
            title="填入候选值后点「运行 sweep」"
            description="结果按 hypothetical PnL 降序；可一键把某组合应用到 /parameters。"
          />
        ) : (
          <ResultsView data={result} registry={registry} />
        )}
      </Stack>
    </>
  )
}

function ResultsView({ data, registry }: { data: ParameterSweepResponse; registry: ParameterRegistryEntry[] }) {
  const fallbackWarn = data.entry_price_cap_fallback_count > 0
  const bestPnlKey = data.best_by_pnl ? paramsKey(data.best_by_pnl.parameters) : null
  const bestWinKey = data.best_by_win_rate ? paramsKey(data.best_by_win_rate.parameters) : null

  return (
    <Stack gap="md">
      {fallbackWarn ? (
        <Alert color="yellow" icon={<IconAlertTriangle size={16} />}>
          {data.entry_price_cap_fallback_count} 个样本的 entry_price 缺 best_ask，回退用 entry_price_cap——
          这部分 hypothetical PnL 系统性偏高。生产 sweep 前让 evaluator 把真实 best_ask 写到 decision_output.metadata 再跑。
        </Alert>
      ) : null}

      <SimpleGrid cols={{ base: 2, md: 5 }} spacing="md">
        <Stat label="grid 组合" value={String(data.candidate_count)} />
        <Stat
          label="决策样本"
          value={String(data.decision_sample_count)}
          hint={`可评分 ${data.scorable_decision_count}`}
        />
        <Stat
          label="无法评分"
          value={String(data.unscorable_decision_count)}
          tone={data.unscorable_decision_count > 0 ? 'neutral' : undefined}
        />
        <Stat
          label="fallback 样本"
          value={String(data.entry_price_cap_fallback_count)}
          tone={fallbackWarn ? 'neg' : undefined}
        />
        <Stat label="per_decision_usdc" value={`$${data.per_decision_usdc}`} />
      </SimpleGrid>

      {data.best_by_pnl || data.best_by_win_rate ? (
        <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
          {data.best_by_pnl ? (
            <BestCard title="Top by PnL" data={data.best_by_pnl} icon={<IconTrophy size={16} />} registry={registry} />
          ) : null}
          {data.best_by_win_rate ? (
            <BestCard title="Top by Win Rate" data={data.best_by_win_rate} icon={<IconTrophy size={16} />} registry={registry} />
          ) : null}
        </SimpleGrid>
      ) : null}

      <SectionCard
        title={`全部结果 ${data.results.length} 组合`}
        description="按 hypothetical PnL 降序；点行可一键应用到 /parameters"
      >
        <ResultsTable rows={data.results} bestPnlKey={bestPnlKey} bestWinKey={bestWinKey} registry={registry} />
      </SectionCard>
    </Stack>
  )
}

function BestCard({
  title,
  data,
  icon,
  registry,
}: {
  title: string
  data: SweepCandidateResult
  icon: React.ReactNode
  registry: ParameterRegistryEntry[]
}) {
  return (
    <SectionCard
      title={
        <Group gap={6}>
          {icon}
          <Text fw={600}>{title}</Text>
        </Group>
      }
    >
      <Stack gap={6}>
        <code style={{ fontSize: 11, padding: '4px 6px', background: 'var(--color-surface-alt)', borderRadius: 4 }}>
          {paramsKey(data.parameters)}
        </code>
        <Group gap="lg" wrap="wrap">
          <Stat
            label="hypo PnL"
            value={formatUsdc(data.hypothetical_pnl_usdc)}
            tone={pnlTone(data.hypothetical_pnl_usdc)}
            compact
          />
          <Stat
            label="win rate"
            value={data.win_rate ? `${(Number(data.win_rate) * 100).toFixed(1)}%` : '—'}
            compact
          />
          <Stat label="入场 / 结算" value={`${data.would_have_entered_count} / ${data.settled_count}`} compact />
          <Stat
            label="mean PnL / 入场"
            value={formatUsdc(data.mean_pnl_per_entered_usdc)}
            tone={pnlTone(data.mean_pnl_per_entered_usdc)}
            compact
          />
        </Group>
        <Button
          size="compact-xs"
          color="accent"
          variant="light"
          onClick={() => promptApplyToParameters(data, registry)}
        >
          应用到 /parameters
        </Button>
      </Stack>
    </SectionCard>
  )
}

function ResultsTable({
  rows,
  bestPnlKey,
  bestWinKey,
  registry,
}: {
  rows: SweepCandidateResult[]
  bestPnlKey: string | null
  bestWinKey: string | null
  registry: ParameterRegistryEntry[]
}) {
  const columns: ColumnDef<SweepCandidateResult, unknown>[] = [
    {
      header: 'parameters',
      cell: ({ row }) => {
        const key = paramsKey(row.original.parameters)
        const isBestPnl = key === bestPnlKey
        const isBestWin = key === bestWinKey
        return (
          <Stack gap={2}>
            <MonoCell>{key}</MonoCell>
            {(isBestPnl || isBestWin) && (
              <Group gap={4}>
                {isBestPnl ? (
                  <Badge size="xs" color="teal" variant="light">
                    PnL #1
                  </Badge>
                ) : null}
                {isBestWin ? (
                  <Badge size="xs" color="blue" variant="light">
                    win rate #1
                  </Badge>
                ) : null}
              </Group>
            )}
          </Stack>
        )
      },
    },
    { header: '入场', accessorKey: 'would_have_entered_count' },
    { header: '结算', accessorKey: 'settled_count' },
    { header: '挂起', accessorKey: 'pending_unsettled_count' },
    {
      header: '胜 / 负',
      cell: ({ row }) => `${row.original.win_count} / ${row.original.loss_count}`,
    },
    {
      header: 'hypo PnL',
      cell: ({ row }) => (
        <span style={{ color: pnlColor(row.original.hypothetical_pnl_usdc) }}>
          {formatUsdc(row.original.hypothetical_pnl_usdc)}
        </span>
      ),
    },
    {
      header: 'win rate',
      cell: ({ row }) =>
        row.original.win_rate ? `${(Number(row.original.win_rate) * 100).toFixed(1)}%` : '—',
    },
    {
      header: 'mean PnL / 入场',
      cell: ({ row }) => (
        <span style={{ color: pnlColor(row.original.mean_pnl_per_entered_usdc) }}>
          {formatUsdc(row.original.mean_pnl_per_entered_usdc)}
        </span>
      ),
    },
    {
      header: '操作',
      cell: ({ row }) => (
        <InlineActionButton
          variant="link"
          onClick={(e) => {
            e.stopPropagation()
            promptApplyToParameters(row.original, registry)
          }}
        >
          应用
        </InlineActionButton>
      ),
    },
  ]
  return (
    <DataTable<SweepCandidateResult>
      columns={columns}
      data={rows}
      rowKey={(r) => paramsKey(r.parameters)}
    />
  )
}

function Stat({
  label,
  value,
  hint,
  tone,
  compact,
}: {
  label: string
  value: string
  hint?: string
  tone?: 'pos' | 'neg' | 'neutral'
  compact?: boolean
}) {
  const color = tone === 'pos' ? 'var(--color-success)' : tone === 'neg' ? 'var(--color-danger)' : undefined
  if (compact) {
    return (
      <Stack gap={0}>
        <Text size="xs" c="dimmed">
          {label}
        </Text>
        <Text size="sm" fw={600} c={color}>
          {value}
        </Text>
      </Stack>
    )
  }
  return (
    <SectionCard>
      <Stack gap={2}>
        <Text size="xs" c="dimmed">
          {label}
        </Text>
        <Text size="lg" fw={700} c={color}>
          {value}
        </Text>
        {hint ? (
          <Text size="xs" c="dimmed">
            {hint}
          </Text>
        ) : null}
      </Stack>
    </SectionCard>
  )
}

function paramsKey(params: Record<string, string | number>): string {
  return Object.entries(params)
    .map(([k, v]) => `${k}=${v}`)
    .join(' · ')
}

function pnlColor(v: string | null | undefined): string | undefined {
  const tone = pnlTone(v)
  if (tone === 'pos') return 'var(--color-success)'
  if (tone === 'neg') return 'var(--color-danger)'
  return undefined
}

// 把一组 sweep 参数批量 PUT 到 /parameters/{scope}/{key}。
// 所有 sweep 参数都在 strategy scope；走 confirmAction 显示 diff。
// 失败的项继续后续 PUT，最后汇总通知——避免半应用。
function promptApplyToParameters(result: SweepCandidateResult, registry: ParameterRegistryEntry[]) {
  const entries = Object.entries(result.parameters)
  if (entries.length === 0) return
  const diff: DiffRow[] = entries.map(([key, value]) => {
    const regEntry = registry.find((e) => e.scope === 'strategy' && e.key === key)
    const before = regEntry
      ? String(regEntry.override?.value ?? regEntry.default_value ?? '(default)')
      : '(unknown)'
    return {
      field: `strategy.${key}`,
      before,
      after: value,
      risk: 'high' as const,
      hint: '点应用后立即生效；重启即丢',
    }
  })
  confirmAction({
    title: '把这组参数应用到 /parameters',
    description: `共 ${entries.length} 个 strategy 参数；逐个 PUT，全部走 audit_events 记录。`,
    tone: 'danger',
    diff,
    onConfirm: async ({ operator, reason, trace_id }) => {
      // 一组里所有 PUT 共用同一 trace_id，整组 apply 在审计上是一条意图。
      const applied: string[] = []
      const failed: Array<{ key: string; err: string }> = []
      for (const [key, value] of entries) {
        try {
          await parametersApi.set('strategy', key, {
            value: typeof value === 'number' ? value : String(value),
            operator,
            reason,
            trace_id,
          })
          applied.push(key)
        } catch (err) {
          failed.push({ key, err: describeError(err) })
        }
      }
      if (failed.length === 0) {
        notifications.show({
          title: `已应用 ${applied.length} 个参数`,
          message: '可在「策略参数热调」页 audit tab 看记录',
          color: 'teal',
        })
      } else {
        notifications.show({
          title: `部分应用：${applied.length} 成 / ${failed.length} 失败`,
          message: failed.map((f) => `${f.key}: ${f.err}`).join('\n'),
          color: 'yellow',
          autoClose: false,
        })
      }
    },
  })
}

