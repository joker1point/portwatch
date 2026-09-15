import type { ProtoFilter, AccessFilter } from '../filters'

interface Props {
  q: string
  onQ: (v: string) => void
  proto: ProtoFilter
  onProto: (v: ProtoFilter) => void
  access: AccessFilter
  onAccess: (v: AccessFilter) => void
  matched: number
  total: number
}

const PROTO_OPTIONS: { value: ProtoFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'tcp', label: 'tcp' },
  { value: 'udp', label: 'udp' },
]

const ACCESS_OPTIONS: { value: AccessFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'ready', label: '可处置' },
  { value: 'limited', label: '受限' },
]

export function FilterBar({
  q,
  onQ,
  proto,
  onProto,
  access,
  onAccess,
  matched,
  total,
}: Props) {
  return (
    <div className="filterbar">
      <label className="search">
        <span className="search__icon">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <circle cx="7" cy="7" r="4.5" stroke="currentColor" strokeWidth="1.6" />
            <path d="M10.6 10.6 14 14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </span>
        <input
          value={q}
          onChange={(e) => onQ(e.target.value)}
          placeholder="搜索端口 / 进程 / 命令行 / PID"
          spellCheck={false}
          aria-label="搜索端口"
        />
      </label>

      <div className="segment" role="group" aria-label="协议过滤">
        {PROTO_OPTIONS.map((o) => (
          <button
            key={o.value}
            type="button"
            aria-pressed={proto === o.value}
            onClick={() => onProto(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>

      <div className="segment" role="group" aria-label="可处置性过滤">
        {ACCESS_OPTIONS.map((o) => (
          <button
            key={o.value}
            type="button"
            aria-pressed={access === o.value}
            onClick={() => onAccess(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>

      <span className="count">
        <b>{matched}</b> / {total} 条
      </span>
    </div>
  )
}
