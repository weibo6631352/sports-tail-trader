// 人工操作 trace_id 工具。
//   - confirmAction 用它自动生成 → 注入 onConfirm 回调。
//   - 手搓 modal（如 ReplaceModal）也 import 这里复用，保持格式一致。
//   - 后端某些 PUT/DELETE body 没有 trace_id 字段（如 /parameters）→ 用
//     appendTraceToReason 在 reason 末尾附 [trace=manual-xxx]，让审计能 grep 回。

const TRACE_RE = /\[trace=manual-[0-9a-f]+\]/

export function generateManualTraceId(): string {
  // crypto.randomUUID 在 secure context（https / localhost）下可用；
  // 反代到非 secure HTTP 时退回到 ad-hoc 拼接——格式仍是 manual-{12hex}。
  const uuid =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 14)}`
  return `manual-${uuid.replace(/-/g, '').slice(0, 12)}`
}

/** 把 trace_id 附到 reason 末尾（若 reason 已含则不重复）。后端 audit 可 grep 回。 */
export function appendTraceToReason(reason: string, traceId: string): string {
  const trimmed = reason.trim()
  if (!trimmed) return `[trace=${traceId}]`
  if (TRACE_RE.test(trimmed)) return trimmed
  return `${trimmed} [trace=${traceId}]`
}
