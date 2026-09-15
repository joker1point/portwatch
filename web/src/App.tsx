import { useEffect, useMemo, useState } from 'react'
import { API_BASE } from './api'
import { FilterBar } from './components/FilterBar'
import { Header } from './components/Header'
import { PortList } from './components/PortList'
import { Timeline } from './components/Timeline'
import {
  applyFilters,
  rowStateOf,
  sortEntries,
  type AccessFilter,
  type ProtoFilter,
} from './filters'
import { usePortStream } from './hooks/usePortStream'

export default function App() {
  const { entries, events, meta, connected, beat, enteredKeys, flashedKeys } = usePortStream()

  const [q, setQ] = useState('')
  const [proto, setProto] = useState<ProtoFilter>('all')
  const [access, setAccess] = useState<AccessFilter>('all')
  const [showBanner, setShowBanner] = useState(false)

  const all = useMemo(() => sortEntries([...entries.values()]), [entries])

  const filtered = useMemo(
    () => applyFilters(all, { q, proto, access }),
    [all, q, proto, access],
  )

  const stats = useMemo(() => {
    let actionable = 0
    let limited = 0
    for (const entry of all) {
      if (rowStateOf(entry) === 'ok') actionable += 1
      else limited += 1
    }
    return { actionable, limited }
  }, [all])

  // 首屏给连接一点建立时间，避免刚打开就闪一条断连告警
  useEffect(() => {
    if (connected) {
      setShowBanner(false)
      return
    }
    const timer = window.setTimeout(() => setShowBanner(true), 2000)
    return () => window.clearTimeout(timer)
  }, [connected])

  // API 的 /api/meta 下发的是 interval（单位：秒）
  const intervalMs =
    meta?.scan_interval_ms ??
    meta?.interval_ms ??
    (typeof meta?.interval === 'number' ? Math.round(meta.interval * 1000) : null)
  const hasFilters = q.trim() !== '' || proto !== 'all' || access !== 'all'

  return (
    <div className="app">
      <Header
        total={all.length}
        actionable={stats.actionable}
        limited={stats.limited}
        beat={beat}
        connected={connected}
        intervalMs={intervalMs}
      />

      <FilterBar
        q={q}
        onQ={setQ}
        proto={proto}
        onProto={setProto}
        access={access}
        onAccess={setAccess}
        matched={filtered.length}
        total={all.length}
      />

      {showBanner && (
        <div className="banner" role="status">
          与 <code>{API_BASE}</code> 的连接已中断，EventSource 正在自动重连…
        </div>
      )}

      <div className="workspace">
        <main className="listpane">
          <PortList
            entries={filtered}
            enteredKeys={enteredKeys}
            flashedKeys={flashedKeys}
            total={all.length}
            hasFilters={hasFilters}
          />
        </main>
        <Timeline events={events} />
      </div>
    </div>
  )
}
