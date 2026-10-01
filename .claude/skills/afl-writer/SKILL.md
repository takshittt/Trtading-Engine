---
name: afl-writer
description: Write and fix AmiBroker Formula Language (AFL) code — strategies, indicators, explorations, backtest settings and multi-file #include setups. The user pastes the code into AmiBroker themselves and reports back any error; this skill writes the code and fixes it from those error reports. Use it whenever the user mentions AmiBroker, AFL, .afl files, turning strategy rules or a strategy document into AmiBroker code, backtesting or scanning in AmiBroker, or pastes an AmiBroker error message (e.g. "Error 6", "Error 29") — even if they don't say "AFL".
---

# AFL Writer

The user doesn't know AFL well. Claude writes the code; the user pastes it into
AmiBroker (running in a Windows VM), runs it, and pastes back any error. Every round
trip is manual, so the goal is code that works on the first paste and fixes that
work on the first try.

AFL looks like C but is a vectorised array language — most bugs come from treating
it like C. Before writing, skim `references/gotchas.md`.

## References (read as needed)
- `references/fundamentals.md` — array model, bar timestamps, trade delays, the
  no-lookahead entry pattern, loops for stateful exits, futures settings,
  multi-timeframe, explorations. Read for any new strategy.
- `references/gotchas.md` — common mistakes and AmiBroker error numbers. Read
  before writing and whenever fixing an error.
- `references/functions.md` — function signatures. If a function isn't listed and
  you're not sure of its signature, say so rather than guessing. Anything the user
  has pasted into this file from the official AmiBroker docs is authoritative.
- `templates/` — starting points: `main.afl`, `indicators.afl`,
  `setup_template.afl`, `risk_management.afl` (two-phase profit booking).

## Writing new code
1. **Pin down the rules first.** Restate the strategy as exact conditions:
   timeframe, entry candle, what is compared to what, long/short, stop, targets,
   time exit. If the strategy text is ambiguous, ask — never invent a rule.
   Plausible invented logic backtests fine and is silently wrong.
2. Build from the templates, keeping the file layout below.
3. Before replying, re-read your code against the hard rules.
4. Reply with each file in its own ```c code block, headed by the file name, so it
   can be copied straight into AmiBroker. Add a one-line note on where it goes
   (include folder vs. the formula applied to the chart/analysis).
5. End with the paste-and-test steps (below).

## Fixing an error the user reports
AmiBroker shows `Error <n>. <message>` with the offending line. The user may paste
just the message, or the message plus the line.
1. Identify the error with `gotchas.md` and find the **root cause** — e.g. Error 6
   means an array was used in `if`; fix the logic, don't wrap it in `LastValue()`
   unless a scalar is actually what the rule means.
2. If the message alone isn't enough to locate the problem, ask for the exact line
   the error points to.
3. Return the **complete corrected file** (easier to paste than a patch), plus one or
   two lines saying what changed and why.
4. Change only what the error requires. Don't refactor, rename, or "improve" other
   parts — the user needs to trust that working code stays as it was.
5. If one mistake likely repeats elsewhere (same pattern in another setup), fix those
   too and say so.

If the code runs but the results look wrong (wrong trades, wrong times), ask for a
couple of example bars from the exploration — date, time, what happened, what
should have happened — and debug from those.

## File layout (user's convention)
| File | Holds |
|---|---|
| `main.afl` | Params, backtest options, `#include_once` lines, final Buy/Sell/Short/Cover, plots, exploration |
| `indicators.afl` | Shared indicator arrays only — no signals |
| `setup_N.afl` | One setup each; defines `SN_Long`, `SN_Short`, `SN_LongSL`, `SN_ShortSL` |
| `risk_management.afl` | Position size, stops, partial exit, breakeven, time exit |

Prefix variables per setup (`S3_`, `S7_`) — included files share one namespace.
`#include_once <file.afl>` resolves from Tools → Preferences → AFL → standard include
folder; mention this the first time multi-file code is delivered.

## Project conventions
- NSE futures (Nifty). `TickSize = 0.05;` hard-coded.
- `LotSize = Param("Lot Size", 65, 1, 5000, 1);` — lot size is a Param, default 65.
- 5-min intraday: the 9:15 candle closes at 9:20; **the 9:20 candle is the entry
  candle** (`TimeNum() == 92000` with start-of-interval timestamps). Evaluate the
  9:15 candle with `Ref(..., -1)`, enter at the 9:20 open.
- Profit booking is two-phase: partial exit at target 1, SL to breakeven, final exit
  at a user-set time. Targets/percentages/times are Params.
- Lean code that matches the strategy document exactly. No extra filters,
  indicators, or features unless asked.
- Every adjustable number is a `Param`/`ParamToggle`/`ParamTime` with a clear label,
  grouped with `_SECTION_BEGIN`/`_SECTION_END`.

## Hard rules
- No arrays in `if`/`while`/`for` conditions — `IIf()`, `[i]` in loops, or a real scalar.
- No lookahead: no `Ref(x, +n)`, no deciding an entry at a bar's Open from that
  bar's High/Low/Close, `expandLast` for higher timeframes, `ValueWhen` n ≥ 1.
- `SetTradeDelays(0, 0, 0, 0)` set explicitly; every `*Price` array you rely on is set.
- Clean signals: `ExRem()` for simple on/off logic, a loop for stateful exits.
- Initialise arrays (`Sell = 0;`) before assigning `Sell[i]` in a loop.
- Loops run `for (i = 0; i < BarCount; i++)`.
- Time constants are HHMMSS numbers (`92000`), never `9:20`.

## Paste-and-test steps (end every code delivery with these)
1. Paste into the Formula Editor → **Verify Syntax**. Send any error message and the
   line it points to.
2. Run an **Exploration** over a few days you know; check the listed signals and
   prices against the strategy doc.
3. Then run the **Backtest** and look at the trade list.
