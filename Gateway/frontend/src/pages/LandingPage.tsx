import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import './landing.css'

// Strategy dashboards are separate apps. Override per deployment with
// VITE_GRID_URL / VITE_REVERSAL_URL; the defaults match the local dev ports.
const host = `${window.location.protocol}//${window.location.hostname}`
const GRID_URL = import.meta.env.VITE_GRID_URL ?? `${host}:5175`
const REVERSAL_URL = import.meta.env.VITE_REVERSAL_URL ?? `${host}:5176`

// ── Example ladder replay ────────────────────────────────────────────────────
// Illustrative only: a seeded price path run through the same rung rules as
// grid_strategy/backend/core/exits.py (chain% = 100).
const X = 40, SL = 150
const RUNGS = [2000, 1960, 1920, 1880]
const LO = 1840, HI = 2060
const N = 360

type Lot = { idx: number; entry: number; target: number; stop: number; buyAt: number; exitAt: number | null }

function buildReplay() {
  let seed = 7
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647
  const keys: [number, number][] = [[0, 2028], [0.22, 1995], [0.34, 1958], [0.48, 1906], [0.6, 1945], [0.72, 2004], [0.86, 2052], [1, 2046]]
  const path: number[] = []
  for (let i = 0; i < N; i++) {
    const t = i / (N - 1)
    let k = 0
    while (k < keys.length - 2 && keys[k + 1][0] < t) k++
    const [t0, p0] = keys[k], [t1, p1] = keys[k + 1]
    path.push(p0 + (p1 - p0) * ((t - t0) / (t1 - t0)) + (rnd() - 0.5) * 9)
  }
  const lots: Lot[] = []
  for (let i = 1; i < N; i++) {
    const prev = path[i - 1], p = path[i]
    RUNGS.forEach((r, idx) => {
      if (prev > r && p <= r && !lots.some(l => l.idx === idx)) {
        const above = lots.filter(l => !l.exitAt && l.entry > r).sort((a, b) => a.entry - b.entry)[0]
        lots.push({ idx, entry: r, target: (above ? above.entry : r) + X, stop: r - SL, buyAt: i, exitAt: null })
      }
    })
    lots.forEach(l => { if (!l.exitAt && i > l.buyAt && p >= l.target) l.exitAt = i })
  }
  return { path, lots }
}

const REPLAY = buildReplay()

function LadderBoard() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const reduce = typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches
  // Start mid-replay so the board reads at rest with lots already open.
  const [frame, setFrame] = useState(reduce ? N - 1 : 200)

  useEffect(() => {
    if (reduce) return
    let hold = 0, last = 0, raf = 0
    const tick = (ts: number) => {
      if (ts - last > 28) {
        last = ts
        setFrame(f => {
          if (f < N - 1) return f + 1
          if (++hold > 70) { hold = 0; return 0 }
          return f
        })
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [reduce])

  const [, setRedraw] = useState(0)
  useEffect(() => {
    const bump = () => setRedraw(n => n + 1)
    const mq = matchMedia('(prefers-color-scheme: dark)')
    window.addEventListener('resize', bump)
    mq.addEventListener('change', bump)
    return () => { window.removeEventListener('resize', bump); mq.removeEventListener('change', bump) }
  }, [])

  const { path, lots } = REPLAY

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const rect = canvas.getBoundingClientRect(), d = window.devicePixelRatio || 1
    const W = rect.width, H = rect.height
    if (canvas.width !== Math.round(W * d) || canvas.height !== Math.round(H * d)) {
      canvas.width = Math.round(W * d); canvas.height = Math.round(H * d)
    }
    ctx.setTransform(d, 0, 0, d, 0, 0)

    const style = getComputedStyle(canvas)
    const c = (n: string) => style.getPropertyValue(n).trim()
    const col = { ink: c('--ink'), grid: c('--chart-grid'), accent: c('--accent'), up: c('--up'), surface: c('--surface') }
    const padL = 8, padR = 46, padT = 10, padB = 10
    const xOf = (i: number) => padL + (i / (N - 1)) * (W - padL - padR)
    const yOf = (p: number) => padT + (1 - (p - LO) / (HI - LO)) * (H - padT - padB)
    const dot = (x: number, y: number, fill: string) => {
      ctx.fillStyle = col.surface; ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.fill()
      ctx.fillStyle = fill; ctx.beginPath(); ctx.arc(x, y, 4.5, 0, Math.PI * 2); ctx.fill()
    }

    ctx.clearRect(0, 0, W, H)
    ctx.font = "500 10px 'IBM Plex Mono', monospace"
    ctx.textBaseline = 'middle'
    ctx.lineWidth = 1
    for (let p = 1860; p <= 2040; p += 40) {
      ctx.strokeStyle = col.grid; ctx.setLineDash([])
      ctx.beginPath(); ctx.moveTo(padL, yOf(p)); ctx.lineTo(W - padR, yOf(p)); ctx.stroke()
    }
    RUNGS.forEach((r, idx) => {
      const y = yOf(r)
      ctx.strokeStyle = col.accent; ctx.globalAlpha = 0.55; ctx.setLineDash([4, 4])
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke()
      ctx.globalAlpha = 1; ctx.setLineDash([])
      ctx.fillStyle = col.accent; ctx.textAlign = 'left'
      ctx.fillText(`B${idx + 1} ${r}`, W - padR + 4, y)
    })
    lots.forEach(l => {
      if (l.buyAt > frame || (l.exitAt && l.exitAt <= frame)) return
      const y = yOf(l.target)
      ctx.strokeStyle = col.up; ctx.globalAlpha = 0.6; ctx.setLineDash([2, 3])
      ctx.beginPath(); ctx.moveTo(xOf(l.buyAt), y); ctx.lineTo(W - padR, y); ctx.stroke()
      ctx.globalAlpha = 1; ctx.setLineDash([])
    })
    ctx.strokeStyle = col.ink; ctx.lineWidth = 1.6; ctx.lineJoin = 'round'
    ctx.beginPath()
    for (let i = 0; i <= frame; i++) {
      const x = xOf(i), y = yOf(path[i])
      if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y)
    }
    ctx.stroke()
    lots.forEach(l => {
      if (l.buyAt <= frame) dot(xOf(l.buyAt), yOf(l.entry), col.accent)
      if (l.exitAt && l.exitAt <= frame) dot(xOf(l.exitAt), yOf(l.target), col.up)
    })
    ctx.fillStyle = col.ink
    ctx.beginPath(); ctx.arc(xOf(frame), yOf(path[frame]), 3.5, 0, Math.PI * 2); ctx.fill()
  })

  const ltp = path[frame]
  const rising = ltp >= path[Math.max(0, frame - 1)]

  return (
    <div className="board" aria-label="Example grid ladder">
      <div className="board-head">
        <span className="sym">Example ladder · Crude futures</span>
        <span className="ltp" style={{ color: rising ? 'var(--up)' : 'var(--down)' }}>LTP {ltp.toFixed(2)}</span>
      </div>
      <canvas
        ref={canvasRef}
        className="ladder"
        role="img"
        aria-label="A price line falls through four buy rungs, buying a lot at each, then rallies and each lot exits at its own target."
      />
      <div className="lots">
        <table>
          <thead><tr><th>Rung</th><th>Entry</th><th>Target</th><th>Stop</th><th>Status</th></tr></thead>
          <tbody>
            {RUNGS.map((r, idx) => {
              const l = lots.find(l => l.idx === idx && l.buyAt <= frame)
              if (!l) return (
                <tr key={r}><td>B{idx + 1}</td><td>{r}</td><td>—</td><td>—</td><td><span className="pill">Waiting</span></td></tr>
              )
              const done = l.exitAt !== null && l.exitAt <= frame
              return (
                <tr key={r}>
                  <td>B{idx + 1}</td><td>{l.entry}</td><td>{l.target}</td><td>{l.stop}</td>
                  <td>{done
                    ? <span className="pill won">Exited +{l.target - l.entry}</span>
                    : <span className="pill open">Open</span>}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="board-note">Illustrative replay. Interval 40 pts, target offset 40 pts, stop 150 pts. Not real trades.</p>
    </div>
  )
}

function Brand() {
  return (
    <>
      <svg viewBox="0 0 32 32" aria-hidden="true">
        <path fill="currentColor" d="M3.5 18.5V14c0-2 1.3-3.4 3.2-3.6C8 7.2 10.8 5 14.3 5c3.6 0 5.8 2.2 8.7 4.4 2.8.6 5 2.6 5 5.8V26h-2.6l-.6-6h-2.3l-.4 6h-2.6l-.4-5.6h-5.2l-.4 5.6H11l-.4-4.6H9.2c-1.4 0-2.4-.8-2.9-2l-.5-.4z" />
        <path fill="var(--accent)" d="M7.6 10.6c-1.4-1-1.8-2.6-1.1-4.2.4 1.2 1.2 1.9 2.4 2.3z" />
      </svg>
      Bison
    </>
  )
}

export default function LandingPage() {
  useEffect(() => {
    const prev = document.title
    document.title = 'Bison Trading Engine'
    return () => { document.title = prev }
  }, [])

  return (
    <div className="landing">
      <header className="nav">
        <div className="wrap">
          <a className="brand" href="#top" aria-label="Bison home"><Brand /></a>
          <nav aria-label="Sections">
            <a href="#how">How it works</a>
            <a href="#strategies">Strategies</a>
            <a href="#safety">Safety</a>
          </nav>
          <Link className="btn" to="/signin">Sign in</Link>
        </div>
      </header>

      <main className="wrap" id="top">
        <div className="hero">
          <div className="hero-copy">
            <span className="eyebrow">Self-hosted execution for NSE &amp; MCX</span>
            <h1>Your AmiBroker signals. <em>Exits on autopilot.</em></h1>
            <p className="lede">Bison connects your AmiBroker scans to your Shoonya account. It places verified orders, watches every tick, and closes each position at its own target or stop, so you are not watching charts all day.</p>
            <div className="cta-row">
              <Link className="btn" to="/signin">Sign in to the gateway</Link>
              <a className="btn ghost" href={GRID_URL}>Open Grid</a>
              <a className="btn ghost" href={REVERSAL_URL}>Open Reversal</a>
            </div>
            <div className="venues"><span>NSE F&amp;O</span><span>MCX commodities</span><span>Shoonya broker</span><span>AmiBroker webhook</span></div>
          </div>
          <LadderBoard />
        </div>

        <section id="how">
          <div className="sec-head">
            <span className="eyebrow">How it works</span>
            <h2>From scan to square-off</h2>
            <p className="lede">Your scans keep running in AmiBroker. Bison does the order handling and position management after a signal fires.</p>
          </div>
          <div className="pipe">
            <div className="stage">
              <span className="who">You</span>
              <h3>AmiBroker</h3>
              <p>Your AFL scan fires on a closed candle and posts the signal to a webhook, with ATR attached.</p>
            </div>
            <div className="stage">
              <span className="who">Strategy</span>
              <h3>Grid or Reversal</h3>
              <p>Checks budget, margin and blacklist, then logs each step of its sizing math before it acts.</p>
            </div>
            <div className="stage">
              <span className="who">Gateway</span>
              <h3>One broker session</h3>
              <p>Holds the only Shoonya login. Every strategy places orders and gets ticks through it.</p>
            </div>
            <div className="stage">
              <span className="who">Broker</span>
              <h3>Verified fills</h3>
              <p>Each fill is checked against the broker's order book, and targets are set from the real fill price.</p>
            </div>
          </div>
        </section>

        <section id="strategies">
          <div className="sec-head">
            <span className="eyebrow">Two strategies, one gateway</span>
            <h2>Pick how you want to trade</h2>
          </div>
          <div className="strats">
            <article className="strat">
              <span className="tag">Fully automated · Dip ladder</span>
              <h3>Grid</h3>
              <p>Buys one lot at each rung as price falls, and keeps every lot separate. Lots are never averaged together, so each one exits on its own numbers.</p>
              <pre className="formula">{`target(B1) = entry(B1) + X
target(Bn) = entry(Bn−1) + X × chain%
stop(Bn)   = entry(Bn) − SL`}</pre>
              <dl className="specs">
                <div><dt>Rungs</dt><dd>Fixed interval from an anchor, or strong supports found from 4-hour swing lows</dd></div>
                <div><dt>Offsets</dt><dd>Points or percent, set per instrument</dd></div>
                <div><dt>Rollover</dt><dd>Moves entry, target and stop by the basis, so P&amp;L carries across the roll</dd></div>
                <div><dt>Alerts</dt><dd>WhatsApp messages for fills, exits and faults</dd></div>
              </dl>
              <div><a className="btn" href={GRID_URL}>Open the Grid dashboard</a></div>
            </article>
            <article className="strat">
              <span className="tag">You enter · it exits · NSE futures</span>
              <h3>Reversal</h3>
              <p>A long-only swing strategy for 1H, 4H and daily signals. You review each signal and choose what to buy. From then on, the system manages target, stop, averaging and rollover.</p>
              <pre className="formula">{`Target hit → arm trailing stop
trail = peak × (1 − buffer%)
exit on pullback to trail`}</pre>
              <dl className="specs">
                <div><dt>Entry</dt><dd>Limit orders at LTP ± slippage ticks, retried if rejected</dd></div>
                <div><dt>Target / SL</dt><dd>Set by hand, or from ATR or the nearest resistance, with a live ₹ projection</dd></div>
                <div><dt>Budget</dt><dd>Counted in SPAN margin. A soft cap warns you, a hard cap blocks new buys</dd></div>
                <div><dt>Averaging</dt><dd>Paid only from the reserve, with a limit per stock on how many times</dd></div>
              </dl>
              <div><a className="btn" href={REVERSAL_URL}>Open the Reversal dashboard</a></div>
            </article>
          </div>
        </section>

        <section id="safety">
          <div className="sec-head">
            <span className="eyebrow">Built for the bad days</span>
            <h2>Safety checks on every order</h2>
            <p className="lede">A bad tick or a dropped feed should never sell a position. Here is what stands between the market and your money.</p>
          </div>
          <div className="safety">
            <div className="guard">
              <span className="num">5<small>checks</small></span>
              <h3>Tick validation</h3>
              <p>Every price must fall inside the live bid/ask spread before it can trigger an exit. Zero prints, crossed books and ghost spikes are rejected.</p>
            </div>
            <div className="guard">
              <span className="num">60<small>s</small></span>
              <h3>Broker reconciliation</h3>
              <p>Every minute, Bison compares its positions with your live Shoonya positions and alerts you if they differ.</p>
            </div>
            <div className="guard">
              <span className="num">180<small>s</small></span>
              <h3>No hasty closes</h3>
              <p>Before a missing lot is marked closed, three reconcile passes must agree over at least three minutes. A flapping feed cannot close a real position.</p>
            </div>
            <div className="guard">
              <span className="num">1<small>s</small></span>
              <h3>Feed fallback</h3>
              <p>If the tick socket goes quiet, quotes are polled every second so targets and stops keep being checked. A 10-second heartbeat watches the feed.</p>
            </div>
            <div className="guard">
              <span className="num">₹<small>limit</small></span>
              <h3>Circuit breaker</h3>
              <p>When today's realised plus open P&amp;L drops past your limit, new buys on that instrument stop. A kill switch halts everything at once.</p>
            </div>
            <div className="guard">
              <span className="num">30<small>days</small></span>
              <h3>Full event log</h3>
              <p>Each signal, calculation, order and skipped trade is saved with an IST timestamp and its reason. You can always see why a trade happened.</p>
            </div>
          </div>
        </section>

        <section>
          <div className="split">
            <div>
              <span className="eyebrow">Backtester</span>
              <h2>Test on the same math you trade</h2>
              <p className="lede">The backtester replays price history through the exact target and stop functions the live engine uses. What you test is what will run.</p>
              <ul>
                <li><strong>Rollover included.</strong> At each contract switch, open lots shift by the basis, as they do live.</li>
                <li><strong>Circuit breaker included.</strong> The daily loss limit blocks new buys in the replay too.</li>
                <li><strong>Kept separate.</strong> Backtests never touch your live positions or settings.</li>
              </ul>
            </div>
            <div>
              <span className="eyebrow">Your hardware, your keys</span>
              <h2>Runs where you run it</h2>
              <p className="lede">Bison is self-hosted. Your broker login stays on your server, and so does your trade history.</p>
              <ul>
                <li><strong>One login, shared.</strong> Only the gateway talks to the broker. Strategies get short-lived tokens with set permissions.</li>
                <li><strong>Survives restarts.</strong> State is saved to the database and checked against the broker on boot.</li>
                <li><strong>Your database.</strong> Use SQLite for one machine, or MySQL or Postgres for a server.</li>
              </ul>
            </div>
          </div>
        </section>

        <section>
          <div className="closing">
            <div className="sec-head">
              <h2>Stop babysitting your stops</h2>
              <p className="lede">Keep finding the trades. Let Bison handle the exits.</p>
            </div>
            <div className="cta-row">
              <Link className="btn" to="/signin">Sign in</Link>
              <a className="btn ghost" href={GRID_URL}>Grid</a>
              <a className="btn ghost" href={REVERSAL_URL}>Reversal</a>
            </div>
          </div>
        </section>

        <footer>
          <div className="brand"><Brand /></div>
          <p className="risk">Bison is trading software. It does not give investment advice and does not promise returns. Trading futures and options carries a high risk of loss, and you can lose more than the margin you deposit. You are responsible for every order placed from your account. Shoonya and AmiBroker are products of their respective owners and are not affiliated with Bison.</p>
        </footer>
      </main>
    </div>
  )
}
