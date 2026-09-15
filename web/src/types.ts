// 与 API 层（server.py）对齐的类型定义。
// 字段命名保持 snake_case，避免前后端来回转换。

export type Proto = 'tcp' | 'udp'

/** 归因完整度：full=字段齐全；partial=命令行/路径受限；denied/gone=已消失或无权读取 */
export type AccessLevel = 'full' | 'partial' | 'denied' | 'gone'

export interface PortEntry {
  /** entry_key，形如 tcp:8787:12345 */
  key: string
  port: number
  proto: Proto
  listen_addrs?: string[]
  pid: number
  process?: string | null
  exe?: string | null
  cmdline?: string | null
  cwd?: string | null
  parent_pid?: number | null
  started_at?: string | null
  access?: AccessLevel
  /** 系统内核进程等不可处置对象 */
  protected?: boolean
  protected_reason?: string | null
}

export interface Meta {
  schema?: string
  scan_interval_ms?: number
  interval_ms?: number
  last_scan_at?: string
  first_scan_at?: string
  total?: number
  counts?: {
    total?: number
    full?: number
    partial?: number
    protected?: number
  }
  [k: string]: unknown
}

export type ChangeKind = 'added' | 'removed' | 'changed'

export interface TimelineEvent {
  id: number
  /** 本地接收时刻（展示用） */
  at: string
  kind: ChangeKind
  port: number
  proto: Proto
  pid: number
  process: string | null
  note?: string
}

/** 归一化后的增量块 */
export interface NormalizedDelta {
  added: PortEntry[]
  removed: string[]
  changed: PortEntry[]
}
