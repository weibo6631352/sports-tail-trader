// 人工操作 trace_id 工具。
//   - confirmAction 用它自动生成 → 注入 onConfirm 回调。
//   - 手搓 modal（如 ReplaceModal）也 import 这里复用，保持格式一致。
//   后端 5 个写入 endpoint (markets pause/resume, operations pause/resume-trading,
//   parameters set/clear) 都已接受 trace_id 入参；callsite 直接传 body.trace_id 即可。

export function generateManualTraceId(): string {
  // crypto.randomUUID 在 secure context（https / localhost）下可用；
  // 反代到非 secure HTTP 时退回到 ad-hoc 拼接——格式仍是 manual-{12hex}。
  const uuid =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 14)}`
  return `manual-${uuid.replace(/-/g, '').slice(0, 12)}`
}
