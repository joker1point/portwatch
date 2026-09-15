import type { ChangeKind, TimelineEvent } from '../types'

interface Props {
  events: TimelineEvent[]
}

const MARK: Record<ChangeKind, string> = {
  added: '+',
  removed: '\u2212',
  changed: '~',
}

const LABEL: Record<ChangeKind, string> = {
  added: '新增监听',
  removed: '监听消失',
  changed: '属性变化',
}

function fmtTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '--:--:--'
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export function Timeline({ events }: Props) {
  return (
    <aside className="timeline" aria-label="变化时间线">
      <div className="timeline__head">
        <h2 className="timeline__title">变化时间线</h2>
        <span className="timeline__hint">{events.length} 条</span>
      </div>

      <div className="timeline__body">
        {events.length === 0 ? (
          <p className="timeline__hint" style={{ padding: 'var(--s-4)', lineHeight: 1.65 }}>
            暂无变化。启动或关闭一个监听端口的进程，这里会出现增删记录。
          </p>
        ) : (
          events.map((ev) => (
            <div className="event" data-kind={ev.kind} key={ev.id}>
              <span className="event__mark" aria-hidden="true">
                {MARK[ev.kind]}
              </span>
              <div className="event__main">
                <div className="event__line">
                  <span className="event__port">{ev.port}</span>
                  <span className="event__proto">{ev.proto}</span>
                  <span className="event__time" title={LABEL[ev.kind]}>
                    {fmtTime(ev.at)}
                  </span>
                </div>
                <span className="event__proc">{ev.process ?? `PID ${ev.pid}`}</span>
              </div>
            </div>
          ))
        )}
      </div>
    </aside>
  )
}
