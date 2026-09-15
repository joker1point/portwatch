interface Props {
  total: number
  actionable: number
  limited: number
  beat: number
  connected: boolean
  intervalMs: number | null
}

export function Header({ total, actionable, limited, beat, connected, intervalMs }: Props) {
  return (
    <header className="header">
      <div className="header__left">
        <h1 className="brand__name">
          Port<em>watch</em>
        </h1>
        <span className="brand__tag">本机端口占用</span>
        <span className="pulse" data-live={connected} title={`SSE ${connected ? '已连接' : '已断开'}`}>
          {/* key 随采集轮次变化，元素重建即可重播脉冲动画 */}
          <span key={beat} className="pulse__dot pulse__dot--beat" />
          {connected ? '实时采集中' : '连接已中断'}
        </span>
      </div>

      <div className="stats">
        <div className="stat">
          <span className="stat__label">监听条目</span>
          <span className="stat__value">{total}</span>
        </div>
        <div className="stat">
          <span className="stat__label">可处置</span>
          <span className="stat__value stat__value--ok">{actionable}</span>
        </div>
        <div className="stat">
          <span className="stat__label">归因受限</span>
          <span className="stat__value stat__value--warn">{limited}</span>
        </div>
        <div className="stat stat--time">
          <span className="stat__label">轮询间隔</span>
          <span className="stat__value stat__value--muted">
            {intervalMs ? `${Math.round(intervalMs / 1000)}s` : '—'}
          </span>
        </div>
      </div>
    </header>
  )
}
