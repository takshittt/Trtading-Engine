# AFL gotchas

Each entry: the wrong code, why it's wrong, the fix.

## Arrays in `if`
```c
if (Close > Open) Buy = 1;          // WRONG: Error 6 — condition is an array
Buy = Close > Open;                 // right: array assignment
x = IIf(Close > Open, High, Low);   // right: array if/else
if (LastValue(Close) > 100) ...     // right: scalar
```
Inside a loop use `Close[i] > Open[i]`.

## `IIf` is not control flow
Both branches are always evaluated as whole arrays. It cannot assign variables:
`IIf(c, x = 1, x = 2)` does nothing useful. Write `x = IIf(c, 1, 2);`.

## Lookahead
- `Ref(x, 1)` reads the NEXT bar. Never in a signal.
- Deciding an entry at a bar's Open with that bar's Close/High/Low.
- `TimeFrameExpand(..., expandFirst)` on higher timeframes.
- `ValueWhen(cond, x, 0)`: n=0 is a future occurrence. Use n ≥ 1.
- `Zig()`/`Peak()`/`Trough()` repaint — never in signals.

## Repeated signals
Without `ExRem` (or loop state), `Buy = Close > MA20` is 1 on every bar above the MA.
Backtester copes; explorations and charts will not.

## Uninitialised variables (Error 29)
Assigning `Sell[i] = 1` or reading `x` before `x = ...;` exists. Initialise arrays
before loops: `Sell = 0; SellPrice = Close;`.

## `=` vs `==`
`if (a = b)` assigns. Comparisons use `==`. Logical: `AND`, `OR`, `NOT`.

## Integer-looking time comparisons
`TimeNum() == 9:20` is invalid; use `92000`. `TimeNum() == 92000` only matches if
that bar exists (missing ticks → no bar). For robust "first bar at or after", use
`tn >= 92000 AND Ref(tn, -1) < 92000` combined with a same-day check.

## Day boundaries
`Ref(x, -1)` on the first bar of the day reads yesterday's last bar. When logic is
"previous candle of today", guard with `newDay = Day() != Ref(Day(), -1);`.
`ValueWhen(tn == 91500, High)` naturally resets each day.

## Partial exits and lot rounding
With `RoundLotSize = LotSize`, a 50% scale-out of 1 lot rounds to 0 or 1 lot. Partial
booking needs ≥ 2 lots (or the user must accept rounding). Say this when relevant.

## Scale-out sizing
`SetPositionSize(50, spsPercentOfPosition * (Buy == sigScaleOut));` only affects
scale-out bars because the method becomes 0 (`spsNoChange`) elsewhere. Call the
normal entry `SetPositionSize(...)` BEFORE this line.

## Stops: ApplyStop vs loop
`ApplyStop` is simple but can't do "SL to breakeven after T1" cleanly and doesn't set
`Sell` for explorations. For two-phase logic use the loop in
`templates/risk_management.afl`.

## #include paths
`#include_once <file.afl>` resolves from the standard include folder
(Tools → Preferences → AFL). Point that at the Mac-shared folder so edits on the Mac
are picked up. Included files share one namespace — prefix setup variables (`S1_`,
`S7_`) to avoid collisions.

## Functions and scope
Variables inside `function`/`procedure` are local unless declared `global`. Params
must be declared at top level (not inside functions) or they misbehave.

## Strings
Concatenate with `+`. Numbers need `NumToStr(x, 1.2, False)` (False = no thousands
separator — important for JSON). Escape quotes as `\"`.

## Common error numbers
| Error | Meaning | Usual cause |
|---|---|---|
| 6 | Condition in if/while/for must be numeric/boolean | array used in `if` |
| 29 | Variable used without having been initialized | missing `x = 0;` before loop or typo |
| 30/31 | Syntax error | missing `;`, unbalanced `()` `{}`, `=` vs `==` |
| 10 | Array subscript out of range | `arr[i-1]` with i=0, or `i <= BarCount` |
| 16 | Too many arguments | wrong function signature — check functions.md |

If an error number isn't here, ask the user for the full message text; don't guess.
