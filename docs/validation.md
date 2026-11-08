# Did the screens work? Phase 10 verdict

**Both screens fail.** Over the windows they were judged on, neither `deep_value` nor
`cyclical_trough` beat the S&P 500 (SPY) after costs. Neither beat an equal-weight holding
of every stock they chose from, or the cheapest 10% by P/E. That held in every subperiod
and with large caps only, and no threshold variant came close to the pass bar.

This is an educational backtest of two rules-based screens, not investment advice. It
says how these rules would have done on past data, nothing about any stock today.

Run: 2026-11-08, `value-screener backtest`. Rules frozen and committed in `6d7e26e` before any
result was seen. Data: the point-in-time panel (`data/backtest/panel/`, 59 quarter ends
2012-2026); outputs in `data/backtest/results/`.

## The test

- Each quarter end, the screen is rebuilt from what had been filed by that date. Its members
  are bought at the next close, equal weight, and held to the next quarter. Returns include
  dividends (Yahoo's adjusted close).
- Costs one way by market cap: 10 bps over $10B, 30 bps $2-10B, 75 bps $300M-2B, 150 bps
  below. They are charged on every trade, rebalancing included. A stock needs a median of
  $1M a day in trading over the prior 20 days to be bought.
- Benchmarks over the same days:
  - **SPY**: the goal.
  - **RSP and IWD**: the equal-weight S&P and Russell 1000 Value.
  - **The same-universe equal weight**: every eligible stock in the panel. It shares the
    panel's survivorship bias, so it's the fairest test.
  - **The cheapest 10% by P/E**: the dumb alternative.
  
  The last two pay the same costs as a screen.
- **Pass** needed all of these, after costs:
  - beat SPY, the same-universe equal weight and the cheapest 10%
  - beat the equal weight in 2 of 3 subperiods
  - with $10B+ stocks only, beat SPY and the $10B+ equal weight
  - keep a lead over the equal weight after the survivorship drag from 10c
  - lead the equal weight by more than 3% a year

  All of those but a 0-3% lead would have been "inconclusive".

## Results

Annualized, after costs.

### Deep value, 2017-01-03 to 2026-11-02 (40 quarters)

| | Return a year | Max drawdown | Worst year | Stocks held | Costs a year |
|---|---|---|---|---|---|
| **Deep value** | **7.0%** | -49.0% | -13.7% | 67 (min 43) | 1.9% |
| SPY | 14.8% | -33.7% | -19.0% | | |
| RSP | 11.8% | -39.0% | -11.8% | | |
| IWD | 11.1% | -38.5% | -8.9% | | |
| Same-universe equal weight | 10.8% | -41.5% | -22.7% | 2,071 | 0.7% |
| Cheapest 10% by P/E | 10.7% | -53.4% | -17.7% | 207 | 1.6% |

Excess: -7.8% a year against SPY (t -1.25), -3.9% against the same-universe equal weight
(t -1.13), -3.7% against the cheapest 10% (t -1.46). The t-statistics use a 4-quarter
block bootstrap. None is far enough from zero to call the screen reliably *worse* either.
But a pass needs it to be better, and it was behind in every comparison.

| Year | Deep value | SPY | Same-universe EW | Cheapest 10% | Deep value vs EW |
|---|---|---|---|---|---|
| 2017 | +7.2% | +21.6% | +18.2% | +16.5% | -11.0% |
| 2018 | -10.0% | -5.1% | -8.6% | -14.6% | -1.5% |
| 2019 | +20.2% | +32.3% | +27.2% | +22.5% | -7.0% |
| 2020 | -4.0% | +15.6% | +25.5% | +9.2% | -29.5% |
| 2021 | +34.8% | +31.3% | +23.9% | +39.4% | +11.0% |
| 2022 | -13.7% | -19.0% | -22.7% | -17.7% | +9.0% |
| 2023 | +20.4% | +26.0% | +16.3% | +31.1% | +4.0% |
| 2024 | -8.8% | +25.3% | +13.5% | +8.6% | -22.3% |
| 2025 | +15.0% | +18.2% | +13.9% | +10.4% | +1.2% |
| 2026 to Nov 2 | +18.6% | +10.9% | +10.9% | +13.4% | +7.7% |

The screen did what a deep value screen is expected to do in the years value led (2021,
2022): it beat the market. It gave that back and more in the years growth led. The worst
were 2020 and 2024, when it held banks, insurers, utilities and commodity producers through
growth rallies. Behind the equal weight in all three subperiods: -6.1% a year in 2017-19,
-2.5% in 2020-22, -3.2% in 2023-26.

The longer check from 2012 (not judged, as the rules said) tells the same story: 9.4% a year
against SPY's 14.1% and the equal weight's 12.0%.

### Cyclical trough, 2019-01-02 to 2026-11-02 (32 quarters)

| | Return a year | Max drawdown | Worst year | Stocks held | Costs a year |
|---|---|---|---|---|---|
| **Cyclical trough** | **12.4%** | -46.7% | -5.5% | 47 (min 32) | 1.6% |
| SPY | 16.8% | -33.7% | -19.0% | | |
| RSP | 13.8% | -39.0% | -11.8% | | |
| IWD | 13.7% | -38.5% | -8.1% | | |
| Same-universe equal weight | 12.6% | -41.5% | -22.7% | 2,183 | 0.7% |
| Cheapest 10% by P/E | 13.7% | -53.4% | -17.7% | 218 | 1.7% |

Excess: -4.4% a year against SPY (t -0.40), -0.2% against the same-universe equal weight
(t -0.07), -1.3% against the cheapest 10% (t -0.73). Roughly a tie with the equal weight,
which isn't a pass.

| Year | Trough | SPY | Same-universe EW | Cheapest 10% | Trough vs EW |
|---|---|---|---|---|---|
| 2019 | +24.7% | +32.3% | +26.7% | +22.2% | -2.1% |
| 2020 | +11.0% | +15.6% | +25.5% | +9.2% | -14.5% |
| 2021 | +23.0% | +31.3% | +23.9% | +39.4% | -0.8% |
| 2022 | -5.5% | -19.0% | -22.7% | -17.7% | +17.2% |
| 2023 | +17.7% | +26.0% | +16.3% | +31.1% | +1.4% |
| 2024 | -0.4% | +25.3% | +13.5% | +8.6% | -13.9% |
| 2025 | +11.6% | +18.2% | +13.9% | +10.4% | -2.3% |
| 2026 to Nov 2 | +18.5% | +10.9% | +10.9% | +13.4% | +7.7% |

Its one strong showing was 2022, down 5.5% when the market fell 19-23%. Subperiods against
the equal weight: -8.4% a year in 2019-20, +10.0% in 2021-22, -2.1% in 2023-26. One of
three, not two.

### Pass checks

| Check | Deep value | Cyclical trough |
|---|---|---|
| Beats SPY | No (-7.8%) | No (-4.4%) |
| Beats same-universe equal weight | No (-3.9%) | No (-0.2%) |
| Beats cheapest 10% by P/E | No (-3.7%) | No (-1.3%) |
| Beats EW in 2 of 3 subperiods | No (0 of 3) | No (1 of 3) |
| $10B+ only beats SPY and $10B+ EW | No (-5.6%, -3.2%; 7 stocks) | No (-4.8%, -2.1%; 11 stocks) |
| Excess over EW less survivorship drag > 0 | No (-3.9% - 0.9%) | No (-0.2% - 0.6%) |
| Excess over EW above 3% | No | No |
| **Verdict** | **Fail** | **Fail** |

### Sensitivity: every variant tried

24 variants per screen, all fixed with the rules before the run:
- each threshold one step each way
- `turning` required
- 12- and 36-month holds
- large caps only
- without Financials
- each sector left out in turn

The full table is in `data/backtest/results/*_sensitivity.csv`.

- **Deep value:**
  - None of the 24 beat SPY or the same-universe equal weight.
  - One (the 36-month hold) beat the cheapest 10%, by 0.5%.
  - The closest to the equal weight were the 36-month hold (-1.0%) and an F-score bar of 5
    (-1.7%).
  - No sector, left out, turns the result: the edge isn't hiding in one sector.
- **Cyclical trough:**
  - 7 of 24 beat their equal weight, by 0.2-1.0% a year, all with t-statistics under 0.4.
  - These were the 12- and 36-month holds, an F-score bar of 3 or 5, and leaving out
    Energy, Materials or Technology.
  - None beat SPY and none came near 3%.
  - Requiring `turning` cut returns to 3.9% a year.
  - That's the profile of noise around zero, not a hidden edge.

## Data fixes made after the first run

The first run showed the same-universe equal weight up 34.9% in 2020 next to RSP's 10.7%.
That led to two data rules. Both were applied to every strategy alike, and neither touches
the screens' rules:

- Yahoo splices a company's old and new shares after a bankruptcy. Oasis Petroleum (CHRD)
  shows +18,470% in Q4 2020; its old shareholders were in fact wiped out. NFE's dividend
  adjustment pushes its adjusted close below zero, a -134% quarter. A stock whose adjusted
  close rises over 5x in a day or isn't positive is now left out of that quarter. That
  dropped 31 stock-quarters from the equal weight, 2 from deep value and none from the trough.
- The run ends at the stocks' last full close (Nov 2), not SPY's (Nov 5).

Effect: the equal weight fell from 11.6% to 10.8% a year, deep value rose from 6.7% to 7.0%,
and the trough was unchanged. No pass check flipped.

## What this test can't show

- **Survivorship.** The panel only has today's listed companies. In 2016-19 it held about
  half of what deep value would have screened; the rest were taken over, went bust or
  delisted. Failures flatter every strategy here, and cheap, distressed names most of all.
  So the true results are likely *worse* than shown, which only strengthens a fail. Takeovers
  cut the other way, since value stocks are bid for at a premium, and paid delisted prices
  would be needed to net the two. They wouldn't rescue a screen that lost to its own universe
  by 3.9% a year, as the 10c drag is about 1% a year.
- **Coverage.** Foreign filers (20-F, 40-F) and some multi-class companies get no past
  market cap, so they're absent. SIC codes and ticker fixes are today's, not those of the
  past. The panel matched the live screen's labels 90% of the time at today's date.
- **Short history.** About 10 years for deep value and 8 for the trough: two or three
  value/growth swings. Value as a factor has a long history of decade-long droughts. A screen
  that lags over 2017-2026 could still have worked over another decade, and this test can't
  tell which. It can only say these rules didn't work over this one.
- **Execution.** Closes, not the prices a real order would get. Costs are estimates. No taxes.

## What happens now

As the plan says: no tuning until it passes. Adjusting thresholds to this history would fit
the screens to one decade. Any change to the rules gets judged by forward tracking
(`value-screener track`, Phase 10a), which started on 2026-11-02 and is free of both
look-ahead and survivorship. It needs years before it means anything.

The screens stay useful as what they are: a way to find companies that are cheap on assets
or on through-the-cycle earnings, as a starting list for reading filings. This test gives no
evidence that buying them mechanically beats the market.
