import type { AccessLevel, PortEntry, Proto } from './types'

export type ProtoFilter = 'all' | Proto
export type AccessFilter = 'all' | 'ready' | 'limited'

export interface Filters {
  q: string
  proto: ProtoFilter
  access: AccessFilter
}

/** 行的可处置性：protected 由采集层判定（内核进程等），其余按归因完整度推导 */
export type RowState = 'ok' | 'limited' | 'protected'

export function rowStateOf(entry: PortEntry): RowState {
  if (entry.protected) return 'protected'
  const access: AccessLevel = entry.access ?? 'full'
  if (access !== 'full') return 'limited'
  return 'ok'
}

export const STATE_LABEL: Record<RowState, string> = {
  ok: '正常',
  limited: '受限',
  protected: '保护',
}

/** 搜索时覆盖的字段：端口、协议、PID、进程名、命令行、可执行路径、监听地址 */
function haystack(entry: PortEntry): string {
  return [
    entry.port,
    entry.proto,
    entry.pid,
    entry.process,
    entry.cmdline,
    entry.exe,
    entry.cwd,
    entry.protected_reason,
    ...(entry.listen_addrs ?? []),
  ]
    .filter((v) => v !== null && v !== undefined)
    .join(' ')
    .toLowerCase()
}

export function applyFilters(entries: PortEntry[], f: Filters): PortEntry[] {
  const needle = f.q.trim().toLowerCase()

  return entries.filter((entry) => {
    if (f.proto !== 'all' && entry.proto !== f.proto) return false

    if (f.access !== 'all') {
      const state = rowStateOf(entry)
      if (f.access === 'ready' && state !== 'ok') return false
      if (f.access === 'limited' && state === 'ok') return false
    }

    if (needle && !haystack(entry).includes(needle)) return false

    return true
  })
}

/** 默认排序：监听端口升序，同端口 tcp 在前 */
export function sortEntries(entries: PortEntry[]): PortEntry[] {
  return [...entries].sort((a, b) => {
    if (a.port !== b.port) return a.port - b.port
    if (a.proto !== b.proto) return a.proto === 'tcp' ? -1 : 1
    return a.pid - b.pid
  })
}
