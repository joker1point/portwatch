interface Props {
  total: number
  hasFilters: boolean
}

export function EmptyState({ total, hasFilters }: Props) {
  // 一条数据都没有 = 连接还没就绪；有数据但被过滤空 = 过滤条件太窄
  if (total === 0) {
    return (
      <div className="empty">
        <span className="empty__glyph">[ : : ]</span>
        <h2 className="empty__title">等待首帧快照</h2>
        <p className="empty__hint">
          正在连接采集服务。若长时间无数据，请确认已在 portwatch 目录下运行
          <code> python server.py </code>
          ，且服务监听于 127.0.0.1:8787。
        </p>
      </div>
    )
  }

  return (
    <div className="empty">
      <span className="empty__glyph">[ 0 ]</span>
      <h2 className="empty__title">没有匹配的端口</h2>
      <p className="empty__hint">
        {hasFilters
          ? '当前搜索或过滤条件下没有结果，试试放宽协议 / 可处置性筛选，或清空搜索词。'
          : '当前没有任何匹配的监听条目。'}
      </p>
    </div>
  )
}
