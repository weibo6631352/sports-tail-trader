export function truncateId(id: string | null | undefined, head = 6, tail = 4): string {
  if (!id) return '—'
  if (id.length <= head + tail + 2) return id
  return `${id.slice(0, head)}…${id.slice(-tail)}`
}

export function shortAddress(addr: string | null | undefined): string {
  return truncateId(addr, 6, 4)
}
