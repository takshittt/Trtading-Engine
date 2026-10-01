import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react'

// Per-browser UI state (panel collapse, filters, sort order) — deliberately
// localStorage, not synced to the backend, so each machine keeps its own
// dashboard layout instead of one shared setting following the user around.
const PREFIX = 'gw_ui:'

function readStored<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(PREFIX + key)
    return raw === null ? fallback : (JSON.parse(raw) as T)
  } catch {
    return fallback
  }
}

/** Drop-in replacement for useState that persists to localStorage under `key`. */
export function usePersistedState<T>(key: string, initial: T | (() => T)): [T, Dispatch<SetStateAction<T>>] {
  const [state, setState] = useState<T>(() =>
    readStored(key, typeof initial === 'function' ? (initial as () => T)() : initial)
  )

  // Skip the write on first render — we just read this value, no need to echo it back.
  const first = useRef(true)
  useEffect(() => {
    if (first.current) {
      first.current = false
      return
    }
    try {
      localStorage.setItem(PREFIX + key, JSON.stringify(state))
    } catch {
      // best-effort — a full/blocked localStorage shouldn't break the dashboard
    }
  }, [key, state])

  return [state, setState]
}
