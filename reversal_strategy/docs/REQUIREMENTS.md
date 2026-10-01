# Reversal Strategy — System Requirements Specification

## 1. Overview

The Reversal Strategy project is an **independent, isolated strategy module** that operates as a **Signal Dashboard + Automated Exit Manager**.

**Core Pipeline:**
> **Signals (Auto from Amibroker)** → **User Reviews on Dashboard** → **Buy Entry (Manual by User)** → **Target / SL / Exit (Fully Automated by System)**

The system does **NOT** auto-buy. It receives live scan results from Amibroker, displays them as actionable signals on a dashboard, and waits for the user to manually decide and execute the buy. Once a position is entered, the system takes full control of exit management — auto-executing targets, stop-losses, and handling rollovers.

Primary instrument: **Futures Contracts (NSE F&O)**.

---

## 0. v2 Refinements (authoritative — supersede any conflicting text below)

1. **UI layout**: Journal/Blacklist/History/Logs live in a **collapsible right-side drawer** opened by a vertical tab button (not a permanent panel). Signals panel on top, Open Positions directly below.
2. **Top bar**: boxed stats — **Total Money (Shoonya cash)**, **Budget (this strategy)**, **Reserve (averaging)**, **Today's P&L**, **Overall P&L** — plus the bison logo, ⚙️ Config, and Kill Switch.
3. **Signals**: Amibroker sends **closed-candle signals only** → no "Candle Status" column. Add a **Search & Add stock/fut** box to manually track/buy a symbol with no Amibroker signal. Signals carry **ATR** (and optional resistance) from Amibroker.
4. **Execution**: **LIMIT orders only**, `limit = LTP ± slippage_ticks × tick_size`; auto-retry on rejection.
5. **Entry Target/SL (per-buy)**: no global target mode. The Buy modal has **independent MANUAL/AUTO toggles for Target and SL**; AUTO exposes a method dropdown (**ATR-based**, **Nearest Resistance**); the modal shows **live ₹ P&L projection** next to each; **Lot Info** (lot size / lots) is shown in the Buy modal and in both the Signals and Open-Positions tables.
6. **Budget = margin**: utilisation is measured in **SPAN margin** (via Shoonya SPAN calculator, refreshed daily), not notional.
7. **Exits**: **no cooldown** after SL. SL hit → full auto-exit. **Target hit → does NOT sell**; it arms a **Trailing SL** = peak × (1 − `trailing_buffer_pct`) that trails up and exits on the pullback.
8. **Manual exits**: an **Exit** submenu — **Complete Exit** or **Partial Exit** (type the number of lots; requires >1 lot). No hardcoded "Exit ½". Averaging + basis-adjusted rollover retained.
9. **Caps**: **Averaging Reserve = 100 − Hard Cap %** (linked). Soft cap warns; hard cap blocks new (non-averaging) buys; reserve is averaging-only.
10. **Theme**: clean, modern **white** UI.
11. **Resilience**: DB survives restarts; every action logged with timestamp; **60s reconciliation** vs live Shoonya positions with mismatch alerts.

---

## 2. Core Functional Requirements

### 2.1 Signal Dashboard (Amibroker Integration)

*   **Live Signal Feed**: The system must connect to Amibroker and receive real-time Buy/Sell scan results.
*   **Supported Time Frames**:
    *   1 Hour (1H)
    *   4 Hours (4H)
    *   Daily (1D)
*   **Signal Display**: Incoming signals must be displayed on the dashboard in a clean, sortable table showing:
    *   Stock Symbol
    *   Signal Type (Buy / Sell)
    *   Timeframe (1H / 4H / 1D)
    *   Signal Time
    *   Current Market Price (LTP)
    *   Auto-calculated Target & SL
    *   Blacklist Status (greyed out / hidden if blacklisted)
*   **Signal Deduplication**: If Amibroker sends the same Buy signal for the same stock within the same candle (scan glitch), the system must ignore the duplicate and not show it twice.
*   **Closed-Candle Only** *(v2)*: Amibroker emits signals on closed candles only. The UI shows **no** intra/closed "Candle Status" column.
*   **Search & Add** *(v2)*: a search box in the Signals panel lets the user manually add any stock/future to track & buy even without an Amibroker signal.
*   **Signal History**: All past signals (acted upon or ignored) must be stored and viewable in a Signal History tab.

### 2.2 Manual Entry Execution

*   **User-Driven Buying**: The user reviews the signals on the dashboard and manually decides which stock to buy. The system does NOT auto-buy.
*   **Buy Action from Dashboard**: The user can click a **"Buy"** button next to a signal. This opens an order form where the user can:
    *   Confirm/edit the lot size (quantity).
    *   Confirm/edit the order type (Limit / Market).
    *   Review the auto-calculated Target and SL before confirming.
*   **Position Registration**: Once the buy order is confirmed and executed at the broker, the system registers the position and takes over automated exit management.

### 2.3 Automated Exit Management (Target & SL)

*   **Auto Target Execution**: Once a position is open, the system must **automatically** monitor the market price and execute the sell order when the target price is hit. No user intervention required.
*   **Auto Stop-Loss Execution**: If the price falls to the SL level, the system must **automatically** square off the position.
*   **Per-Buy Target/SL** *(v2, supersedes global target mode)*: In the Buy modal, Target and SL each have an independent **MANUAL / AUTO** toggle. MANUAL = type the exact price. AUTO = pick a method (**ATR-based** = entry ± ATR×mult, or **Nearest Resistance**). The modal shows the live projected ₹ profit/loss for the chosen levels.
*   **Trailing SL after Target** *(v2, supersedes sell-at-target)*: When price reaches the Target the system does **not** sell — it arms a Trailing Stop at `peak × (1 − trailing_buffer_pct)` (buffer is a global config) that trails up with new highs and exits only on a pullback to the trail.
*   **Partial Exit**: The user can close a specific **number of lots** (requires more than 1 lot held) via the Exit submenu.

### 2.4 Position Management & Averaging (Pyramiding)

*   **Averaging on Re-Signal**: If Amibroker generates an additional Buy signal for a stock that is already held in the portfolio, the system will notify the user and handle it based on the configured mode:
    *   **Auto Mode**: The system automatically executes the averaging buy order.
    *   **Manual Mode**: The system highlights the re-signal on the dashboard and waits for user confirmation before executing.
*   **Max Averaging Limit**: Configurable **"Max Number of Averaging Buys"** per stock (e.g., max 3 buys). Once the limit is reached, further buy signals for that stock are shown but marked as "Averaging Limit Reached".
*   **Average Price Recalculation**: On each averaging buy, the system must automatically recalculate the weighted average entry price, and adjust the Target and SL accordingly.
*   **UI Representation**: Averaging trades must be displayed in a **separate, clearly identifiable column** on the dashboard, showing the averaging count and new average price.

### 2.5 Rollover Management

*   **Expiry Detection**: The system must track futures contract expiry dates and alert the user when a held position's contract is approaching expiry (e.g., 3 days before).
*   **Rollover Execution**: When the user (or system) rolls over to the next-month contract, the system must automatically:
    *   Adjust the Target price based on the new contract's premium/discount.
    *   Adjust the Stop-Loss level accordingly.
    *   Update the entry price reference for P&L tracking.
*   **Rollover Log**: Every rollover action must be logged with old contract details, new contract details, and the price adjustments made.

### 2.6 Filtering & Blacklisting

*   **Blacklist Filter**: Signals for blacklisted stocks must be automatically hidden or greyed out on the dashboard (not shown as actionable).
*   **Manual Blacklisting Workflow**:
    1.  User searches or clicks on a stock in the UI.
    2.  System displays "Stock Info" (basic fundamentals, sector, F&O lot size, etc.).
    3.  User clicks **"Add to Blacklist"** button.
    4.  Stock is immediately excluded from future signal displays.
*   **Blacklist Management**: A dedicated page to view, search, and remove stocks from the blacklist.
*   **Future Scope (Automated Blacklisting)**: Architecture should support plugging in automated blacklisting based on Fundamental, Technical, or Quantitative parameters.

---

## 3. Account Manager & Budget Module

### 3.1 Budget Allocation & Margin Checking

*   **Strategy Budget**: The user defines a total capital budget allocated to this strategy.
*   **Margin-Based Calculation**: The system must download **daily margin data** from the broker and calculate the available buying power based on actual margin requirements per stock.
*   **Budget Utilization Display**: The dashboard must always show:
    *   Total Budget
    *   Utilized Budget (margin locked in open positions)
    *   Available Budget
    *   Reserve Budget (locked for averaging)
*   **Soft Cap Warning**: When utilized budget reaches **80%** of total, show a yellow warning indicator on the dashboard.
*   **Hard Cap**: When utilized budget reaches or exceeds **90%** of total (remaining 10% is reserved), the system hides the "Buy" button on new signals and shows a "Budget Exhausted" badge. The user is informed but not blocked from viewing signals.

### 3.2 Reserve Budgeting

*   **Averaging Reserve (= 100 − Hard Cap %)** *(v2)*: The reserve is mathematically linked to the hard cap, not a separate number. If Hard Cap = 90%, the reserve is exactly 10% of total capital, forcibly held and usable **only** for Averaging Buys.
*   **Reserve Usage Tracking**: When averaging buys dip into the reserve, the dashboard must clearly indicate that reserve funds are being used.

---

## 4. Risk Management

### 4.1 Per-Stock Stop Loss

*   Every position must have a Stop-Loss (SL) set at the time of entry.
*   SL can be:
    *   **Fixed Percentage**: e.g., 2% below entry price.
    *   **Technical-Based**: e.g., below the nearest support level (auto-calculated or user-defined).
*   The system **auto-executes** the SL order when the price breaches the level.


### 4.2 Global Strategy Stop Loss

*   A portfolio-level safety net.
*   If the overall strategy's **Mark-to-Market (MTM) loss** exceeds a user-defined threshold (e.g., -5% of total budget), the system must:
    1.  **Auto square off ALL open positions**.
    2.  **Halt all new signal processing** for the day.
    3.  **Alert the user** (in-app alert).
*   This protects against catastrophic loss on black swan days.

### 4.3 Cooldown After SL Hit — REMOVED (v2)

*   Per v2, there is **no cooldown** after an SL hit. If Amibroker re-signals the same stock immediately, it is shown as a normal actionable signal.

---

## 5. Operational & Infrastructure Requirements

### 5.1 Architecture

*   **Isolated & Independent**: The system must be a standalone module, completely decoupled from the main Gateway. A crash in the gateway must not affect this strategy, and vice-versa.
*   **Modular Design**: Signal ingestion, exit management, budget tracking, and UI must be separate modules communicating via well-defined interfaces.

### 5.2 Persistence & Data Storage

*   **Database**: All positions, signals, orders, blacklists, configuration, and logs must be persisted in a database (SQLite for simplicity, or PostgreSQL for scale).
*   **Crash Recovery**: On system restart, the system must restore its exact pre-crash state — open positions, pending targets/SLs, and resume monitoring without requiring manual intervention.
*   **Data Backup**: Daily automatic backup of the database.

### 5.3 Logging & Audit Trail

*   **Every action must be logged** with a timestamp:
    *   Signal received
    *   Signal displayed to user
    *   User clicked Buy / Ignored signal
    *   Order sent to broker
    *   Order filled / rejected / modified
    *   Target hit → auto-sell executed
    *   SL hit → auto-sell executed
    *   Averaging buy triggered
    *   Rollover performed
    *   Blacklist add/remove
    *   Budget cap reached
    *   System errors / API disconnections
*   Logs must be searchable and filterable by date, stock, and action type.

### 5.4 Continuous & EOD Reconciliation

*   **Every-Minute Reconciliation**: While any position is open, the system must — **every 1 minute** — fetch the broker's positions, tradebook, and order statuses, and compare **trade, price, quantity, and execution details** against its internal state to remove any inconsistencies.
    *   On mismatch: immediately alert the user on the dashboard and auto-correct the internal state to match the broker (broker is the source of truth).
*   **EOD Reconciliation**: At end of each trading day (post 3:30 PM IST), the system must additionally run a full comparison of its internal position state with the broker's actual positions via API.
*   **Mismatch Alert**: If any discrepancy is found (e.g., system thinks a position is open but broker shows it as closed), immediately alert the user with full details.
*   This catches ghost orders, partial fills, slippage differences, and manual broker-side changes.

### 5.5 Market Hours & Holiday Handling

*   **Trading Window**: The system must only process signals and execute orders during NSE market hours: **9:15 AM – 3:30 PM IST**.
*   **Stale Signal Rejection**: Any signals received outside market hours (e.g., from a delayed Amibroker scan) must be flagged as "Stale" and not shown as actionable.
*   **Holiday Calendar**: Maintain an NSE holiday calendar. On holidays, the system should display a "Market Closed" status and disable all trading actions.

### 5.6 Execution & Order Management

*   **Smart Order Placement**: For auto-executed orders (targets, SLs), use **Limit Orders with Slippage Tolerance** instead of raw Market orders. If the limit order doesn't fill within *X* seconds, widen the price slightly to ensure execution.
*   **Order Retry**: If an order is rejected (API glitch, margin spike), the system retries a configurable number of times (e.g., 3 retries with 2-second gaps) before alerting the user.
*   **Order Status Tracking**: Every order must be tracked through its lifecycle: Placed → Pending → Filled / Rejected / Cancelled.

### 5.7 System Resilience

*   **Auto-Reconnection**: If the Amibroker connection or Broker API websocket drops, the system must detect within 5 seconds and auto-reconnect. If reconnection fails after 3 attempts, alert the user.
*   **Heartbeat Monitoring**: The dashboard must display live connection status indicators for both Amibroker and the Broker API (green = connected, red = disconnected).

---

## 6. Dashboard & UI Requirements

### 6.1 Signal Panel

*   Real-time table of incoming signals from Amibroker.
*   Columns: Symbol, Signal Type, Timeframe, Signal Time, LTP, Target, SL, Blacklist Status, Action (Buy button).
*   Sortable and filterable by timeframe, signal type, and time.

### 6.2 Open Positions Panel

*   All currently held positions with:
    *   Symbol, Entry Price, Current Price, Lots, Target, SL, Unrealized P&L (₹ and %), Averaging Count, Days Held, Contract Expiry Date.
*   Color-coded P&L (green for profit, red for loss).
*   Action buttons: Edit Target, Edit SL, Partial Exit, Full Exit, Rollover.

### 6.3 Portfolio Summary Bar

*   Always-visible top bar showing:
    *   Total Budget | Utilized | Available | Reserve
    *   Today's P&L | Overall P&L
    *   Open Positions Count
    *   Connection Status (Amibroker / Broker API)
*   **Global Config Button**: A ⚙️ Settings/Config button must be placed in the top bar, directly beside the P&L information. It opens the global configuration panel (budget, max averaging, cooldown, global SL %, target mode, order retries, etc.).

### 6.3.1 Per-Stock Configuration (Stock Click)

*   Clicking on any stock (in the Signal Panel or Open Positions Panel) opens a **Per-Stock Config modal** containing:
    *   Stock info (fundamentals, sector, F&O lot size).
    *   Custom Target / SL overrides for that stock.
    *   Averaging mode (Auto / Manual) and per-stock averaging limit.
    *   "Add to Blacklist" button.

### 6.4 Trade Journal & P&L Tracker

*   Historical log of all completed trades: Entry Date, Exit Date, Symbol, Entry Price, Exit Price, Quantity, P&L, Exit Reason (Target / SL / Manual / Global SL).
*   Summary stats: Win Rate, Average P&L per trade, Max Drawdown, Profit Factor.
*   Filterable by date range, stock, and exit reason.

### 6.5 Kill Switch (Panic Button)

*   A prominent, always-visible red button to instantly:


    1.  Square off ALL open positions at market price.
    2.  Halt all new signal processing.
*   Requires a confirmation dialog to prevent accidental activation.

---

## 7. Future Scope (Phase 2+)

### Delivered

*   **Fully Automated Buying** — the Execution **Manual / Auto** toggle in the top bar.
    Auto sizes each entry by risk (`auto_risk_pct` of budget to the signal's own stop),
    trades only the ⭐ Essential set, and is capped by `auto_max_lots` /
    `auto_max_positions`. It stands down entirely while Stoploss is Manual, while Paper
    Trading is on, or while signals are halted — the toggle shows which. See
    `app/services/auto_exec.py`.
*   **Paper Trading Mode** — simulated fills off the live Shoonya bid/ask, capped at the
    limit price a real order would have carried. See `app/services/paper.py`.

### Still outstanding

*   **Automated Blacklisting**: Based on fundamental/technical/quant filters.
*   **Multi-Strategy Support**: Run multiple independent strategies with separate budgets.
*   **Advanced Analytics**: Equity curve, sector-wise exposure heatmap, drawdown charts.
*   **Multi-Broker Support**: Pluggable broker adapters (Zerodha, Angel One, Fyers, Dhan, etc.).