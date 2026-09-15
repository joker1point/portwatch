import { useEffect, useReducer, useRef, useState } from 'react'
import { STREAM_URL, entryKeyOf, fetchMeta, normalizeDelta } from '../api'
import type { ChangeKind, Meta, PortEntry, Proto, TimelineEvent } from '../types'

const MAX_EVENTS = 200
/** 页面重新可见时，超过这个时长就重连一次，避免后台标签页积累的陈旧状态 */
const STALE_RECONNECT_MS = 30_000

interface State {
  entries: Map<string, PortEntry>
  events: TimelineEvent[]
  meta: Meta | null
  connected: boolean
  everConnected: boolean
  /** 收到的采集轮次，驱动顶部心跳脉冲 */
  beat: number
  lastSyncAt: number
  /** 本轮新增的 key，用于播放进入动画 */
  enteredKeys: Set<string>
  /** 本轮变更的 key，用于播放闪烁 */
  flashedKeys: Set<string>
}

type Action =
  | { type: 'snapshot'; entries: PortEntry[] }
  | { type: 'delta'; raw: unknown }
  | { type: 'meta'; meta: Meta }
  | { type: 'connected'; value: boolean }
  | { type: 'clearHighlights' }

let eventSeq = 0

function parseKey(key: string): { proto: Proto; port: number; pid: number } {
  const [proto, port, pid] = key.split(':')
  return {
    proto: proto === 'udp' ? 'udp' : 'tcp',
    port: Number(port) || 0,
    pid: Number(pid) || 0,
  }
}

function makeEvent(kind: ChangeKind, e: Partial<PortEntry>): TimelineEvent {
  return {
    id: ++eventSeq,
    at: new Date().toISOString(),
    kind,
    port: e.port ?? 0,
    proto: e.proto === 'udp' ? 'udp' : 'tcp',
    pid: e.pid ?? 0,
    process: e.process ?? null,
  }
}

function init(): State {
  return {
    entries: new Map(),
    events: [],
    meta: null,
    connected: false,
    everConnected: false,
    beat: 0,
    lastSyncAt: 0,
    enteredKeys: new Set(),
    flashedKeys: new Set(),
  }
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'snapshot': {
      const entries = new Map<string, PortEntry>()
      for (const e of action.entries) entries.set(entryKeyOf(e), e)
      return {
        ...state,
        entries,
        beat: state.beat + 1,
        lastSyncAt: Date.now(),
        enteredKeys: new Set(),
        flashedKeys: new Set(),
      }
    }

    case 'delta': {
      const { added, removed, changed } = normalizeDelta(action.raw)
      if (added.length === 0 && removed.length === 0 && changed.length === 0) return state

      const entries = new Map(state.entries)
      const entered = new Set<string>()
      const flashed = new Set<string>()
      let events = state.events

      for (const e of added) {
        const k = entryKeyOf(e)
        entries.set(k, e)
        entered.add(k)
        events = [makeEvent('added', e), ...events].slice(0, MAX_EVENTS)
      }

      for (const e of changed) {
        const k = entryKeyOf(e)
        entries.set(k, e)
        flashed.add(k)
        events = [makeEvent('changed', e), ...events].slice(0, MAX_EVENTS)
      }

      for (const k of removed) {
        const prev = entries.get(k)
        const info = prev ?? parseKey(k)
        entries.delete(k)
        events = [makeEvent('removed', info), ...events].slice(0, MAX_EVENTS)
      }

      return {
        ...state,
        entries,
        events,
        beat: state.beat + 1,
        lastSyncAt: Date.now(),
        enteredKeys: entered,
        flashedKeys: flashed,
      }
    }

    case 'meta':
      return { ...state, meta: action.meta }

    case 'connected':
      return {
        ...state,
        connected: action.value,
        everConnected: state.everConnected || action.value,
      }

    case 'clearHighlights':
      return { ...state, enteredKeys: new Set(), flashedKeys: new Set() }

    default:
      return state
  }
}

function extractEntries(payload: unknown): PortEntry[] {
  if (Array.isArray(payload)) return payload as PortEntry[]
  if (payload && typeof payload === 'object') {
    const p = payload as { entries?: PortEntry[]; items?: PortEntry[]; ports?: PortEntry[] }
    return p.entries ?? p.items ?? p.ports ?? []
  }
  return []
}

export interface PortStream extends State {
  reconnect: () => void
}

export function usePortStream(): PortStream {
  const [state, dispatch] = useReducer(reducer, undefined, init)
  const esRef = useRef<EventSource | null>(null)
  // nonce 变化即重建 SSE 连接：EventSource 会自带重连，但陈旧增量序列只能靠整体重来
  const [nonce, setNonce] = useState(0)
  const bumpNonce = () => setNonce((n) => n + 1)

  // 元信息单独取一次，用于展示轮询间隔等
  useEffect(() => {
    const ctl = new AbortController()
    fetchMeta(ctl.signal)
      .then((meta) => dispatch({ type: 'meta', meta }))
      .catch(() => {
        /* meta 是增强信息，失败不阻塞主流程 */
      })
    return () => ctl.abort()
  }, [])

  // SSE 主连接
  useEffect(() => {
    const es = new EventSource(STREAM_URL)
    esRef.current = es

    es.onopen = () => dispatch({ type: 'connected', value: true })
    es.onerror = () => dispatch({ type: 'connected', value: false })

    const onSnapshot = (ev: MessageEvent) => {
      let payload: unknown = null
      try {
        payload = JSON.parse(ev.data)
      } catch {
        return
      }
      dispatch({ type: 'snapshot', entries: extractEntries(payload) })
    }

    const onDelta = (ev: MessageEvent) => {
      let payload: unknown = null
      try {
        payload = JSON.parse(ev.data)
      } catch {
        return
      }
      dispatch({ type: 'delta', raw: payload })
    }

    const onHeartbeat = () => dispatch({ type: 'connected', value: true })

    es.addEventListener('snapshot', onSnapshot as EventListener)
    es.addEventListener('delta', onDelta as EventListener)
    es.addEventListener('heartbeat', onHeartbeat as EventListener)

    return () => {
      es.removeEventListener('snapshot', onSnapshot as EventListener)
      es.removeEventListener('delta', onDelta as EventListener)
      es.removeEventListener('heartbeat', onHeartbeat as EventListener)
      es.close()
      esRef.current = null
    }
  }, [nonce])

  // 行高亮：动画播完即清，避免 DOM 上残留 class
  useEffect(() => {
    if (state.enteredKeys.size === 0 && state.flashedKeys.size === 0) return
    const t = window.setTimeout(() => dispatch({ type: 'clearHighlights' }), 900)
    return () => window.clearTimeout(t)
  }, [state.enteredKeys, state.flashedKeys])

  // 标签页长时间不可见后回来，主动重建连接，避免拿到陈旧的增量序列
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return
      if (Date.now() - state.lastSyncAt > STALE_RECONNECT_MS) bumpNonce()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [state.lastSyncAt])

  return { ...state, reconnect: bumpNonce }
}
