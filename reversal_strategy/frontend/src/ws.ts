import { useEffect, useRef } from 'react'
import { API_BASE, getToken } from './auth'

export interface WSMessage {
  event: string
  data: any
  ts?: string
}

/** Subscribe to the dashboard WebSocket. Auto-reconnects on drop. */
export function useWebSocket(onMessage: (msg: WSMessage) => void) {
  const handlerRef = useRef(onMessage)
  handlerRef.current = onMessage

  useEffect(() => {
    let ws: WebSocket | null = null
    let closed = false
    let retry: ReturnType<typeof setTimeout>

    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      // A browser can't set headers on a WebSocket, so the Gateway user token
      // rides as ?token= (the backend rejects the socket without it).
      const t = getToken()
      const q = t ? `?token=${encodeURIComponent(t)}` : ''
      const origin = API_BASE ? API_BASE.replace(/^http/, 'ws') : `${proto}://${location.host}`
      ws = new WebSocket(`${origin}/api/ws${q}`)
      ws.onmessage = (e) => {
        try { handlerRef.current(JSON.parse(e.data)) } catch { /* ignore */ }
      }
      ws.onclose = () => {
        if (!closed) retry = setTimeout(connect, 2000)
      }
      ws.onerror = () => ws?.close()
    }
    connect()

    return () => { closed = true; clearTimeout(retry); ws?.close() }
  }, [])
}
