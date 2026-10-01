# AFL fundamentals

## 1. Everything is an array

A formula runs once per symbol over all loaded bars at the same time. `Close` is an
array of BarCount values; `x = Close > Open;` produces an array of 0/1. Operators and
most functions work element-by-element.

```c
bull   = Close > Open;               // array
avg    = MA(Close, 20);              // array
signal = Cross(Close, avg);          // array: 1 only on the crossing bar
level  = IIf(bull, High, Low);       // array-wise if/else
```

Scalars: numbers, `Param()` results, `BarCount`, `LastValue(arr)`.
`if (...)` needs a scalar — see gotchas.md.

## 2. Bar timestamps

AmiBroker's default (Tools → Preferences → Intraday) labels compressed bars with the
**start** of the interval. On 5-min bars:

| Label (`TimeNum()`) | Covers | Closes at |
|---|---|---|
| 91500 | 9:15–9:20 | 9:20 |
| 92000 | 9:20–9:25 | 9:25 |

`TimeNum()` returns HHMMSS as a number (91500, 152500). `DateNum()` returns
(year-1900)*10000 + month*100 + day. New trading day:

```c
newDay = Day() != Ref(Day(), -1);
```

If the user's Preferences use END-of-interval timestamps, every time constant shifts —
ask if timings look off.

## 3. Trade delays and prices — the no-lookahead entry pattern

With `SetTradeDelays(0, 0, 0, 0)` a signal on bar *i* trades on bar *i* at `BuyPrice[i]`.
To enter at the 9:20 open based on the 9:15 candle:

```c
SetTradeDelays(0, 0, 0, 0);
tn = TimeNum();
firstBull = Ref(Close, -1) > Ref(Open, -1);   // 9:15 candle, already closed
Buy      = tn == 92000 AND firstBull;          // decided at 9:20 open
BuyPrice = Open;                               // filled at 9:20 open
```

Everything on the right-hand side of `Buy` must be known at the moment of fill.
Using `Close` of the entry bar to decide an entry at its `Open` is lookahead.

Stop/limit style entry (e.g. breakout above 9:15 high during the day):

```c
trig     = ValueWhen(tn == 91500, High);             // 9:15 high, held for the day
Buy      = tn >= 92000 AND High > trig AND Ref(High, -1) <= trig;
BuyPrice = Max(Open, trig + TickSize);                // gap-through fills at Open
```

## 4. Signal arrays and cleaning

`Buy`, `Sell`, `Short`, `Cover` are the four signal arrays. The backtester ignores a
Buy while already long, but charts and explorations will show every repeat, so:

```c
Buy  = ExRem(Buy, Sell);
Sell = ExRem(Sell, Buy);
```

`ExRem` is only correct for simple on/off logic. Once exits depend on state (entry
price, whether target 1 was hit, breakeven stop), use a loop.

## 5. Loops for stateful logic

Loops operate on scalars `arr[i]`. Pattern (see templates/risk_management.afl for the
full two-phase version):

```c
Sell = 0; inPos = False; entry = 0;
for (i = 1; i < BarCount; i++)
{
    if (!inPos)
    {
        if (Buy[i]) { inPos = True; entry = BuyPrice[i]; }
    }
    else
    {
        Buy[i] = 0;                               // no repeat entries while in trade
        if (Low[i] <= entry - SL) { Sell[i] = 1; SellPrice[i] = Min(Open[i], entry - SL); inPos = False; }
    }
}
```

Assigning `Sell[i]` requires `Sell` to exist first (`Sell = 0;`), otherwise Error 29.

## 6. Futures and position sizing (NSE)

```c
SetOption("FuturesMode", True);
SetOption("InitialEquity", 1000000);
SetOption("MaxOpenPositions", 1);
SetOption("CommissionMode", 3);           // 3 = $ per share/contract; confirm in docs
SetOption("CommissionAmount", 0);
RoundLotSize = LotSize;                   // quantities are multiples of a lot
TickSize     = 0.05;
PointValue   = 1;                         // P&L = points * shares
MarginDeposit = 0;                        // set if you want margin-based sizing
SetPositionSize(LotSize * Lots, spsShares);
```

Pick one convention and state it in comments: shares with `RoundLotSize = LotSize`
(above), OR contracts with `PointValue = LotSize` and `SetPositionSize(Lots, spsShares)`.
Don't mix.

## 7. Multiple timeframes

```c
TimeFrameSet(in15Minute);
ma15 = MA(Close, 20);
TimeFrameRestore();
ma15 = TimeFrameExpand(ma15, in15Minute);   // default expandLast: value of the last COMPLETED 15m bar
```

`expandLast` avoids lookahead; `expandFirst` leaks the future on intraday bars.

## 8. Explorations (the verification tool)

```c
Filter = Buy OR Sell OR Short OR Cover;
AddColumn(Buy,  "Buy",  1.0);
AddColumn(BuyPrice, "BuyPx", 1.2);
AddColumn(Sell, "Sell", 1.0);
AddColumn(SellPrice, "SellPx", 1.2);
```

Run over a date range the user can compare with their strategy doc/chart.
