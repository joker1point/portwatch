import type { PortEntry } from '../types'
import { STATE_LABEL, rowStateOf } from '../filters'
import { EmptyState } from './EmptyState'

interface Props {
  entries: PortEntry[]
  enteredKeys: Set<string>
  flashedKeys: Set<string>
  total: number
  hasFilters: boolean
}

function Row({
  entry,
  entering,
  flashing,
}: {
  entry: PortEntry
  entering: boolean
  flashing: boolean
}) {
  const state = rowStateOf(entry)
  const cmd = entry.cmdline || entry.exe || null

  const cls = ['row', entering ? 'row--enter' : '', flashing ? 'row--flash' : '']
    .filter(Boolean)
    .join(' ')

  return (
    <div className={cls} data-state={state} role="row">
      <span className="row__port" role="cell">
        {entry.port}
      </span>

      <span className="badge" data-proto={entry.proto} role="cell">
        {entry.proto}
      </span>

      <span className="row__proc" role="cell">
        {entry.process ? (
          <span className="row__proc-name" title={entry.exe ?? entry.process}>
            {entry.process}
          </span>
        ) : (
          <span className="row__proc-name row__proc-name--ghost">未知进程</span>
        )}
        {entry.pid > 0 && <span className="row__pid">{entry.pid}</span>}
      </span>

      <span className="row__cmd col--cmd" role="cell" title={cmd ?? entry.protected_reason ?? ''}>
        {cmd ?? (
          <span className="row__cmd--ghost">{entry.protected_reason ?? '未采集到命令行'}</span>
        )}
      </span>

      <span className="row__state" role="cell">
        <span
          className={`shield shield--${state}`}
          title={entry.protected_reason ?? `归因完整度：${entry.access ?? 'full'}`}
        >
          {STATE_LABEL[state]}
        </span>
      </span>
    </div>
  )
}

export function PortList({ entries, enteredKeys, flashedKeys, total, hasFilters }: Props) {
  if (entries.length === 0) {
    return <EmptyState total={total} hasFilters={hasFilters} />
  }

  return (
    <div className="portlist" role="table" aria-label="端口占用列表">
      <div className="portlist__head" role="row">
        <span role="columnheader">端口</span>
        <span role="columnheader">协议</span>
        <span role="columnheader">进程</span>
        <span className="col--cmd" role="columnheader">
          命令行 / 路径
        </span>
        <span role="columnheader" style={{ textAlign: 'right' }}>
          状态
        </span>
      </div>

      {entries.map((entry) => (
        <Row
          key={entry.key}
          entry={entry}
          entering={enteredKeys.has(entry.key)}
          flashing={flashedKeys.has(entry.key)}
        />
      ))}
    </div>
  )
}
