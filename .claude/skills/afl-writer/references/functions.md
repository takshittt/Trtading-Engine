# Function reference (curated)

Signatures for the functions this project uses most. Arrays are marked `arr`.
Anything not listed: confirm against AmiBroker's AFL Function Reference
(Help → AFL Function Reference, or amibroker.com/guide/afl/<name>.html) before use.
The user can paste official pages into this file to extend it — pasted docs win over
anything written here.

## Price & time
| Function | Returns | Notes |
|---|---|---|
| `Open High Low Close Volume` (`O H L C V`) | arr | built-in arrays |
| `TimeNum()` | arr | HHMMSS, e.g. 92000 |
| `DateNum()` | arr | (YYYY-1900)*10000+MMDD |
| `DateTime()` | arr | full datetime, use with `DateTimeToStr` |
| `Day() Month() Year() DayOfWeek()` | arr | |
| `Interval()` | scalar | bar interval in seconds |
| `Name()` | string | current symbol |

## Array logic
| Function | Notes |
|---|---|
| `IIf(cond, a, b)` | array if/else |
| `Ref(arr, n)` | n < 0 = past bars. Never n > 0 in signals |
| `Cross(a, b)` | 1 on the bar a crosses above b |
| `ExRem(a, b)` | keeps first a after each b |
| `ExRemSpan(a, n)` | removes repeats of a within n bars |
| `ValueWhen(cond, arr, n=1)` | value of arr at nth most recent cond |
| `BarsSince(cond)` | bars since cond was last true |
| `HHV(arr, n)` / `LLV(arr, n)` | highest/lowest over n bars (includes current) |
| `Sum(arr, n)` | rolling sum |
| `Nz(x, default=0)` | replace Null |
| `LastValue(arr)` / `SelectedValue(arr)` | scalar from array |
| `Max(a, b)` / `Min(a, b)` | element-wise |
| `Prec(x, n)` | truncate to n decimals |

## Indicators
`MA(arr, n)`, `EMA(arr, n)`, `WMA(arr, n)`, `RSI(n)`, `RSIa(arr, n)`, `ATR(n)`,
`MACD(fast=12, slow=26)`, `Signal(fast=12, slow=26, sig=9)`, `BBandTop(arr, n, width)`,
`BBandBot(arr, n, width)`, `StDev(arr, n)`.
VWAP has no single built-in; compute with a day-reset cumulative sum:
```c
newDay = Day() != Ref(Day(), -1);
barsToday = BarsSince(newDay) + 1;
tp = (H + L + C) / 3;
vwap = Sum(tp * V, barsToday) / Sum(V, barsToday);
```

## Timeframes
`TimeFrameSet(interval)`, `TimeFrameRestore()`,
`TimeFrameExpand(arr, interval, mode=expandLast)`,
`TimeFrameGetPrice("C", interval, shift=0)`.
Intervals: `in1Minute in5Minute in15Minute inHourly inDaily inWeekly`, or `n * in1Minute`.

## Parameters
| Function | Notes |
|---|---|
| `Param("label", default, min, max, step)` | numeric |
| `ParamToggle("label", "Off\|On", default)` | 0/1 |
| `ParamList("label", "A\|B\|C", default)` | returns string |
| `ParamStr("label", "default")` | string |
| `ParamTime("label", "09:20:00")` | returns HHMMSS number |

## Backtest
| Function / var | Notes |
|---|---|
| `SetTradeDelays(buy, sell, short, cover)` | use 0,0,0,0 |
| `BuyPrice SellPrice ShortPrice CoverPrice` | fill price arrays |
| `SetPositionSize(size, method)` | `spsShares`, `spsValue`, `spsPercentOfEquity`, `spsPercentOfPosition`, `spsNoChange` |
| `SetOption("name", value)` | FuturesMode, InitialEquity, MaxOpenPositions, AllowSameBarExit … |
| `ApplyStop(type, mode, amount, exitatstop)` | types `stopTypeLoss/Profit/Trailing/NBar`; modes `stopModePoint/Percent` |
| `sigScaleIn` / `sigScaleOut` | values for Buy/Short arrays to scale |
| `RoundLotSize TickSize PointValue MarginDeposit` | per-symbol trade vars |

## Output / debug
`Plot(arr, "name", color, style)`, `PlotShapes(shapeArr, color, layer, yPos, offset)`,
`Filter`, `AddColumn(arr, "name", format)`, `AddTextColumn(str, "name")`,
`_TRACE("text")`, `_SECTION_BEGIN("name")` / `_SECTION_END()`,
`RequestTimedRefresh(seconds)`, `Status("action")`.

## Strings
`NumToStr(x, format=1.3, separator=True)`, `StrFormat("fmt", ...)`, `DateTimeToStr(dt)`,
`StrLen`, `StrFind`, `StrReplace`, `StrLeft`, `StrRight`, `StrMid`.

## Static variables (persist across runs of the formula)
`StaticVarSet("k", num)`, `StaticVarGet("k")`, `StaticVarSetText("k", str)`,
`StaticVarGetText("k")`, `StaticVarRemove("k")`.
