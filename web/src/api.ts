import type { Meta, NormalizedDelta, PortEntry, Proto } from './types'

/** API 地址：默认指向本机 collector 服务，可用 VITE_API_BASE 覆盖 */
const RAW_BASE = import.meta.env.VITE_API_BASE as string | undefined
export const API_BASE = (RAW_BASE ?? 'http://127.0.0.1:8787').replace(/\/+$/, '')
export const STREAM_URL = `${API_BASE}/api/stream`

export async function fetchMeta(signal?: AbortSignal): Promise<Meta> {
  const res = await fetch(`${API_BASE}/api/meta`, { signal })
  if (!res.ok) throw new Error(`meta ${res.status}`)
  return (await res.json()) as Meta
}

export async function fetchPorts(signal?: AbortSignal): Promise<PortEntry[]> {
  const res = await fetch(`${API_BASE}/api/ports`, { signal })
  if (!res.ok) throw new Error(`ports ${res.status}`)
  const data = (await res.json()) as unknown
  // 兼容两种返回形态：裸数组 或 { entries: [...] }
  if (Array.isArray(data)) return data as PortEntry[]
  const wrapped = data as { entries?: PortEntry[]; items?: PortEntry[] }
  return wrapped.entries ?? wrapped.items ?? []
}

function isEntry(v: unknown): v is PortEntry {
  return !!v && typeof v === 'object' && 'port' in (v as object) && 'key' in (v as object)
}

/**
 * 把 SSE delta 载荷归一成 { added, removed, changed }。
 * 采集层的 diff 结构可能在演进，这里做容错而不写死 schema。
 */
export function normalizeDelta(raw: unknown): NormalizedDelta {
  const out: NormalizedDelta = { added: [], removed: [], changed: [] }
  if (!raw || typeof raw !== 'object') return out

  const p = raw as Record<string, unknown>

  if (Array.isArray(p.added)) out.added = p.added.filter(isEntry)
  if (Array.isArray(p.changed)) out.changed = p.changed.filter(isEntry)
  if (Array.isArray(p.removed)) {
    out.removed = p.removed
      .map((item) => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object' && 'key' in (item as object)) {
          return String((item as { key: unknown }).key)
        }
        return null
      })
      .filter((k): k is string => !!k)
  }

  // 单条形态兜底：{ kind: 'added', entry: {...} }
  if (out.added.length === 0 && out.removed.length === 0 && out.changed.length === 0) {
    const kind = p.kind
    const entry = p.entry ?? p.item
    if (kind === 'added' && isEntry(entry)) out.added = [entry]
    else if (kind === 'changed' && isEntry(entry)) out.changed = [entry]
    else if (kind === 'removed') {
      const k = typeof entry === 'string' ? entry : (entry as { key?: string } | undefined)?.key
      if (k) out.removed = [String(k)]
    }
  }

  return out
}

export function entryKeyOf(entry: PortEntry): string {
  return entry.key || `${entry.proto}:${entry.port}:${entry.pid}`
}

export function protoOf(entry: Pick<PortEntry, 'proto'>): Proto {
  return entry.proto === 'udp' ? 'udp' : 'tcp'
}
