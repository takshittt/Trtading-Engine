# Reversal Strategy — System Architecture & Pipeline

Strategy module: **Reversal Strategy**
Phase 1: Auto signals → Manual Buy → Fully-automated exits.
The Shoonya order gateway is a **dedicated instance for this strategy**, completely isolated from the main trading gateway at `/Users/mac/gateway_system/Gateway` (that codebase is only reused as the UI/gateway template).

> **v2 deltas (see REQUIREMENTS §0):** closed-candle signals only (no candle-status) + Search & Add · per-buy MANUAL/AUTO Target & SL (ATR / resistance) with ₹ projection · LIMIT-only orders · **Target → Trailing SL** (not sell-at-target) · no post-SL cooldown · reserve = 100 − hard cap · budget on SPAN margin · Shoonya via isolated REST/WS gateway · Amibroker via CSV-bridge webhook · white UI.

---

## 1. Full System Architecture (End-to-End Pipeline)

```mermaid
flowchart TD

%% ================= SIGNAL SOURCE =================
subgraph AMI["📡 AMIBROKER — Signal Source"]
    AFL["AFL Strategy Scans<br/>1H · 4H · 1D timeframes"]
    SCAN["Automated Scanner<br/>(runs continuously)"]
    AFL --> SCAN
end

SCAN -->|"Buy / Sell signals<br/>(HTTP push / file bridge)"| INGEST

%% ================= SIGNAL INGESTION =================
subgraph SIG["🔍 SIGNAL INGESTION SERVICE"]
    INGEST["Signal Receiver API"]
    DEDUP["Deduplication<br/>(same stock + same candle → ignore)"]
    CANDLE["Candle-Close Tagger<br/>(intra-candle vs confirmed)"]
    HOURS["Market Hours Guard<br/>9:15–15:30 IST · NSE holiday calendar<br/>outside hours → mark STALE"]
    BLFILTER["Blacklist Filter<br/>(hide / grey-out blacklisted stocks)"]
    COOLDOWN["Cooldown Check<br/>(block re-entry after SL hit)"]
    INGEST --> DEDUP --> CANDLE --> HOURS --> BLFILTER --> COOLDOWN
end

COOLDOWN -->|"validated signal"| CORE

%% ================= STRATEGY CORE =================
subgraph CORE["🧠 REVERSAL STRATEGY CORE (isolated backend)"]
    SIGENG["Signal Engine<br/>enrich with LTP, lot size, expiry"]
    TCALC["Target / SL Calculator<br/>• Dynamic (indicator-based)<br/>• Resistance-based (user-editable)<br/>• Fixed % or Technical SL"]
    BUDGET["Account Manager<br/>Total / Utilized / Available budget<br/>Daily margin download<br/>80% soft-cap ⚠ · 90% hard-cap ⛔<br/>10% averaging reserve"]
    POSMGR["Position Manager<br/>• Weighted avg price recalc<br/>• Averaging / pyramiding (max-N limit)<br/>• Days held · expiry tracking"]
    EXIT["Automated Exit Engine<br/>• Auto Target execution<br/>• Auto SL execution<br/>• Partial exit (50% T1, rest T2)<br/>• Global strategy SL → square off ALL<br/>• Kill switch handler"]
    ROLL["Rollover Manager<br/>expiry alert T-3 days<br/>premium/discount adjusted Target & SL<br/>rollover log"]
    RECON["🔄 RECONCILIATION ENGINE<br/>runs EVERY 1 MINUTE<br/>compare system vs broker:<br/>• trades (fills) • prices • qty<br/>• avg price • order status<br/>mismatch → alert + auto-correct<br/>+ full EOD reconciliation 15:30"]
    RETRY["Smart Order Module<br/>Limit + slippage tolerance<br/>auto-widen if unfilled<br/>3 retries on rejection"]

    SIGENG --> TCALC
    SIGENG --> BUDGET
    POSMGR --> EXIT
    POSMGR --> ROLL
    EXIT --> RETRY
    ROLL --> RETRY
end

%% ================= PERSISTENCE =================
subgraph DATA["💾 PERSISTENCE LAYER"]
    DB[("SQLite / PostgreSQL<br/>signals · positions · orders<br/>blacklist · config · logs")]
    AUDIT["Audit Logger<br/>every action timestamped<br/>searchable / filterable"]
    BACKUP["Daily Auto-Backup"]
    RECOVER["Crash Recovery<br/>restore exact state on restart"]
    DB --- AUDIT
    DB --- BACKUP
    DB --- RECOVER
end

CORE <--> DATA

%% ================= UI =================
subgraph UI["🖥️ REVERSAL STRATEGY DASHBOARD (separate UI, Gateway-style look)"]
    TOPBAR["Portfolio Summary Bar (always visible)<br/>Budget: Total | Utilized | Available | Reserve<br/>Today's P&L | Overall P&L | Open Positions<br/>🟢 Amibroker · 🟢 Broker heartbeat<br/>⚙️ CONFIG BUTTON (next to P&L info)"]
    GCONFIG["Global Config Panel<br/>budget · max averaging · cooldown<br/>global SL % · target mode · retries"]
    SIGPANEL["Signal Panel (live table)<br/>Symbol · Type · TF · Time · LTP<br/>Target · SL · candle status · BUY btn"]
    ORDFORM["Buy Order Form<br/>edit lots · Limit/Market<br/>review Target & SL → CONFIRM"]
    POSPANEL["Open Positions Panel<br/>entry · LTP · P&L ₹/% (color-coded)<br/>avg count column · expiry<br/>Edit Tgt/SL · Partial · Full Exit · Rollover"]
    STKCONFIG["Per-Stock Config Modal<br/>(opens on STOCK CLICK)<br/>stock info · sector · lot size<br/>custom Target/SL · averaging mode<br/>➕ Add to Blacklist"]
    BLPAGE["Blacklist Manager Page<br/>view · search · remove"]
    JOURNAL["Trade Journal & P&L Tracker<br/>win rate · avg P&L · drawdown<br/>profit factor · exit reasons"]
    HIST["Signal History Tab<br/>acted / ignored signals"]
    KILL["🔴 KILL SWITCH<br/>square off ALL + halt signals<br/>(confirmation dialog)"]

    TOPBAR --> GCONFIG
    SIGPANEL --> ORDFORM
    POSPANEL --> STKCONFIG
    SIGPANEL --> STKCONFIG
    STKCONFIG --> BLPAGE
end

CORE -->|"WebSocket: signals, positions,<br/>P&L, alerts, connection status"| UI
ORDFORM -->|"👤 USER MANUAL BUY<br/>(Phase 1: user decides)"| CORE
KILL --> EXIT

%% ================= GATEWAY =================
subgraph GW["🚪 SHOONYA ORDER GATEWAY (dedicated · isolated from main gateway)"]
    GAPI["Order API<br/>place · modify · cancel"]
    GSTAT["Order Status Tracker<br/>Placed → Pending → Filled/Rejected"]
    GMARGIN["Margin Data Service<br/>daily margin download"]
    GPOS["Broker Positions & Tradebook API<br/>(feeds 1-min reconciliation)"]
    GWS["Live Price Feed (WebSocket)<br/>auto-reconnect < 5s, 3 attempts"]
end

RETRY --> GAPI
GAPI --> GSTAT
GSTAT -->|"fill confirmations"| POSMGR
GMARGIN --> BUDGET
GPOS -->|"every 1 min"| RECON
GWS -->|"LTP stream"| EXIT
GWS -->|"LTP stream"| SIGENG

GW <-->|"Shoonya REST + WebSocket"| BROKER["🏦 SHOONYA BROKER<br/>(NSE F&O)"]

RECON -->|"mismatch alert"| UI

style AMI fill:#1a237e,color:#fff
style SIG fill:#4a148c,color:#fff
style CORE fill:#004d40,color:#fff
style GW fill:#b71c1c,color:#fff
style UI fill:#e65100,color:#fff
style DATA fill:#37474f,color:#fff
style RECON fill:#ff6f00,color:#000
style KILL fill:#d50000,color:#fff
style TOPBAR fill:#fb8c00,color:#000
```

---

## 2. Trade Lifecycle (Sequence — signal to exit, with 1-minute reconciliation)

```mermaid
sequenceDiagram
    autonumber
    participant AB as Amibroker
    participant SS as Reversal Strategy Core
    participant UI as Dashboard UI
    participant U as 👤 User
    participant GW as Shoonya Gateway
    participant BR as Shoonya Broker

    AB->>SS: Buy signal (symbol, TF, time)
    SS->>SS: Dedup · candle-close tag · market-hours · blacklist · cooldown
    SS->>SS: Calc Target & SL · check budget caps (80%/90%)
    SS->>UI: Show signal in Signal Panel
    U->>UI: Reviews signal → clicks BUY
    UI->>U: Order form (lots, Limit/Market, Target/SL preview)
    U->>UI: Confirms order
    UI->>SS: Manual buy request
    SS->>GW: Place order (smart limit + slippage tolerance)
    GW->>BR: Order placed
    BR-->>GW: Fill confirmation
    GW-->>SS: Order filled → register position
    SS->>UI: Position appears in Open Positions Panel

    loop ⏱ EVERY 1 MINUTE (while position open)
        SS->>GW: Fetch broker positions + tradebook + order status
        GW->>BR: Query
        BR-->>GW: Actual trades / prices / qty
        GW-->>SS: Broker truth
        SS->>SS: Compare trade · price · execution vs internal state
        alt Mismatch found
            SS->>UI: ⚠ Inconsistency alert + auto-correct state
        end
    end

    loop Continuous price monitoring (WebSocket LTP)
        BR-->>SS: Live tick
        alt Target hit
            SS->>GW: AUTO SELL (target)
        else SL hit
            SS->>GW: AUTO SQUARE-OFF (SL) → start cooldown
        else Global MTM SL breached
            SS->>GW: SQUARE OFF ALL + halt signals
        else Re-signal on held stock
            SS->>UI: Averaging prompt (auto/manual mode, max-N limit)
        else Expiry T-3 days
            SS->>UI: Rollover alert → adjust Tgt/SL on roll
        end
    end

    GW-->>SS: Exit fill confirmation
    SS->>UI: Trade closed → Journal updated (P&L, exit reason)
    Note over SS,BR: 15:30 IST — Full EOD reconciliation + daily DB backup
```

---

## 3. Key UI placement rules (as specified)

| Element | Placement |
|---|---|
| ⚙️ Config (global settings) button | Top bar, directly beside the P&L / budget information |
| Per-stock configuration | Modal that opens when the user **clicks a stock** (in Signal Panel or Positions Panel) — includes stock info, custom Target/SL, averaging mode, and Add-to-Blacklist |
| 🔴 Kill Switch | Always visible, prominent, confirmation dialog |
| Averaging trades | Separate clearly-identifiable column (count + new avg price) |
| Budget Exhausted (≥90%) | Buy button hidden, badge shown — signals still visible |

## 4. Isolation guarantee

- **Reversal Strategy backend + its Shoonya gateway** run as their own processes with their own DB — a crash in the main Gateway (`/Users/mac/gateway_system/Gateway`) cannot affect this strategy, and vice-versa.
- The existing Gateway codebase is used **only as the visual/structural template** for the new UI and the Shoonya order-placement layer.

## 5. Phase 2+ (already accommodated in the architecture)

Auto-buy toggle · automated blacklisting plug-ins · multi-strategy budgets · paper-trading mode · multi-broker adapters · advanced analytics.
