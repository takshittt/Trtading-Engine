import { useCallback, useEffect, useRef, useState } from 'react'
import { api, wsUrl } from '../api'
import type { LogRow, Snapshot } from '../types'

/** Live connection to the engine: snapshots every second over WS (prices tick
 * every second on screen), live log stream, REST fallback if the socket dies. */
export function useEngine() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [liveLogs, setLiveLogs] = useState<LogRow[]>([])
  const [wsUp, setWsUp] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const retryRef = useRef(1000)

  const bumpLogs = useCallback((entry: LogRow) => {
    setLiveLogs((prev) => [entry, ...prev].slice(0, 200))
  }, [])

  useEffect(() => {
    let closed = false
    let pingTimer: ReturnType<typeof setInterval> | null = null

    const connect = () => {
      if (closed) return
      const ws = new WebSocket(wsUrl())
      wsRef.current = ws
      ws.onopen = () => {
        setWsUp(true)
        retryRef.current = 1000
        pingTimer = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send('ping')
        }, 10000)
      }
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data)
          if (msg.type === 'snapshot') setSnapshot(msg.data)
          else if (msg.type === 'log') bumpLogs(msg.data)
        } catch { /* ignore malformed */ }
      }
      ws.onclose = () => {
        setWsUp(false)
        if (pingTimer) clearInterval(pingTimer)
        if (!closed) {
          setTimeout(connect, retryRef.current)
          retryRef.current = Math.min(retryRef.current * 2, 10000)
        }
      }
      ws.onerror = () => ws.close()
    }
    connect()

    // REST fallback poll — keeps the dashboard alive even if the WS is blocked.
    // Guarded against overlap: while the WS is down this fires every 2s, and
    // an in-flight request skipped here left nothing to stop the NEXT tick
    // from firing another on top of it. Each one pins a Starlette worker
    // thread waiting on the (WAN, occasionally slow) DB — a burst of
    // overlapping polls could saturate that fixed-size thread pool, at which
    // point even unrelated, DB-free requests queued behind them for tens of
    // seconds. One in-flight request at a time removes the pile-up entirely.
    let polling = false
    const poll = setInterval(async () => {
      if (polling || wsRef.current?.readyState === WebSocket.OPEN) return
      polling = true
      try { setSnapshot(await api.state()) } catch { /* engine down */ }
      finally { polling = false }
    }, 2000)

    // seed the recent-log strip
    api.logs(5).then((r) => setLiveLogs(r.logs)).catch(() => {})

    return () => {
      closed = true
      clearInterval(poll)
      if (pingTimer) clearInterval(pingTimer)
      wsRef.current?.close()
    }
  }, [bumpLogs])

  return { snapshot, liveLogs, wsUp }
}
