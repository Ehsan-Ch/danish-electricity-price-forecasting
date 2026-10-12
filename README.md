# PowerTrade — Danish electricity forecasting & trade management

A complete Python research project connecting **DK1/DK2 electricity-price forecasts** to **battery positions and a persistent paper-trade management system**. It downloads public Energinet prices, evaluates forecasts in chronological order, schedules an energy-constrained battery and records every order transition in SQLite.

The included benchmark uses real historical prices. All orders and financial results are **simulated**, with no exchange connection or live execution.

## Included

- Energinet `DayAheadPrices` download, cached source hash and retrieval metadata.
- Strict quarter-hour validation and complete-hour aggregation in UTC.
- Standardized ridge regression; 72-hour and weekly seasonal baselines.
- Daily forward evaluation and independently held-out calibration intervals.
- Dynamic-programming battery schedule using adverse prediction bounds.
- Trade management: `PROPOSED → APPROVED → FILLED`, plus `REJECTED` and `CANCELLED` states.
- Power/capacity controls, efficiency losses, gross cash-flow budget, prior-day loss kill switch, duplicate protection and atomic settlement.
- Portable HTML report, forecast CSV, order blotter, daily cash flows and SQLite audit trail.
- 16 automated tests and GitHub Actions for Python 3.11–3.13.

## Quick start

Python 3.11+ is required; NumPy is the only application dependency.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e .

python -m powertrade.cli download --start 2026-04-01 --end 2026-10-01 --output data/prices.json
python -m powertrade.cli backtest --data data/prices.json --output reports/my-run --days 30
python -m powertrade.cli ledger reports/my-run/trades.sqlite
python -m unittest discover -s tests -v
```

Open `reports/my-run/report.html` in a browser. A checked-in report is available at [reports/benchmark/report.html](reports/benchmark/report.html); download the repository to view it locally. Machine-readable results are in [metrics.json](reports/benchmark/metrics.json), [predictions.csv](reports/benchmark/predictions.csv), [daily_pnl.csv](reports/benchmark/daily_pnl.csv) and [orders.csv](reports/benchmark/orders.csv).

Each backtest requires a **fresh output directory**, preventing accidental deletion of an existing ledger. The raw cache and SQLite files stay local and are excluded from Git. Download requires network access; forecasting, simulation, reports and tests run offline after data is cached.

To produce 24-hour forecasts and proposed positions without execution:

```bash
python -m powertrade.cli forecast --data data/prices.json --day 2026-10-01 --output reports/new-forecast
```

This produces 48 forecast rows, plus a JSON schedule. An empty proposal list means the uncertainty-adjusted opportunity does not justify a trade. The included October 1 example has no qualifying trades. This is a historical example, not a current market signal; download an appropriate recent history for a different target date.

## Recorded benchmark

Source window: April 1–October 1, 2026 (Danish local time, end exclusive), **35,136 quarter-hour observations**. Evaluation: **August 31–September 29, 2026**, 720 hourly predictions per zone. The incomplete final UTC day is excluded.

| Metric | DK1 | DK2 |
|---|---:|---:|
| Ridge MAE, EUR/MWh | 51.24 | 51.20 |
| Weekly baseline MAE, EUR/MWh | 67.26 | 64.73 |
| 72-hour baseline MAE, EUR/MWh | 71.04 | 70.82 |
| Ridge RMSE, EUR/MWh | 67.73 | 66.60 |
| Observed coverage of nominal 90% intervals | 79.44% | 79.44% |
| Paper battery P&L, EUR | 11,147.69 | 9,841.21 |
| Filled orders | 87 | 78 |

The ridge model improves MAE over the weekly baseline by about 23.8% in DK1 and 20.9% in DK2 **on this specific window**. Interval coverage misses the nominal target materially. P&L is a research cash-flow result for two separate hypothetical batteries, not evidence of a deployable or profitable strategy. Daily-close drawdown is zero in this short simulation; this does not imply zero intraday cash requirement or market risk. Parameters were fixed before this run, with no test-period tuning.

## Forecast protocol

1. Forecast UTC day D at **09:00 UTC on D−1**; the horizon spans 15–38 hours ahead. This is a research UTC block, not a precise exchange auction-calendar implementation.
2. Treat an archived price as usable only **24 hours after delivery**. This deliberately conservative assumption avoids relying on unrecorded publication timestamps. Archived revisions still prevent a true point-in-time reconstruction.
3. Features: 72/96/168-hour price lags, mean and standard deviation over hours 72–95 before the target, daily harmonics, weekday harmonics and a weekend flag. No realized future weather or target-day prices.
4. Consider at most 90 eligible history days, reserving the final 14 for calibration. Fit means/scales and ridge coefficients only on the earlier fitting portion. Fixed ridge penalty: 10.
5. Use the finite-sample 90th-percentile absolute calibration error for symmetric intervals. Do not refit on calibration rows. Temporal dependence and regime shifts mean nominal coverage is not guaranteed.
6. Repeat daily, scoring only unseen targets. MAPE is intentionally omitted because electricity prices can be zero or negative.

## Trade management system

Positive grid MWh is a **BUY/charge** position; negative MWh is a **SELL/discharge** position. A delivery hour is one fixed-power block across its four underlying quarter-hour products. Two independent batteries are modeled, one in each zone.

Default limits per battery:

| Control | Setting |
|---|---:|
| Grid power | 2 MW |
| Stored energy capacity | 8 MWh |
| SoC scheduling increment | 0.5 MWh |
| Efficiency per direction | 95% (90.25% round trip) |
| Fees + degradation assumption | EUR 2/grid MWh throughput |
| Adverse fill slippage | EUR 0.50/grid MWh |
| Forecast gross daily cash-flow budget | EUR 10,000 |
| Prior settled day loss threshold | EUR 1,500 |

The scheduler starts and ends each day empty, eliminating unsold terminal inventory. It buys against the upper forecast bound and sells against the lower bound. A dynamic program maximizes this conservative objective over feasible SoC paths. The ledger independently validates the entire schedule; a failed check rejects the whole day. The budget uses absolute prices, so negative-price exposure is included. A budget rejection does not trigger automatic rescheduling.

Paper fills use realized hourly clearing prices plus adverse slippage, assuming all scheduled quantities execute. `pnl` on an individual order is its **cash flow after fees**. Summing a complete empty-to-empty day gives realized battery P&L:

```text
grid_MWh = delta_SoC / efficiency       when charging
grid_MWh = delta_SoC * efficiency       when discharging (both negative)
fill = clearing_price + sign(grid_MWh) * slippage
cash_flow = -grid_MWh * fill - abs(grid_MWh) * cost
```

SQLite stores orders, SoC positions, risk settings and lifecycle events. Repeated proposals fail on unique area/delivery keys. Settlement is all-or-none and cannot execute twice. Pending positions must be cleared before another day is approved. Risk settings cannot silently change when reopening a ledger. Cancelling requires a complete unfilled schedule. The loss switch blocks subsequent approvals after a breach and deliberately has no automatic reset; review the run and start a separate reviewed simulation.

For custom research, import `Limits`, `Ledger`, `schedule`, and `pipeline.run`. For example:

```python
from powertrade.pipeline import run
from powertrade.trading import Limits

run("data/prices.json", "reports/smaller-battery", test_days=30,
    limits=Limits(capacity_mwh=4, power_mw=1, daily_gross_budget_eur=5000))
```

## Boundaries and next steps

This is an auditable portfolio/research implementation, not a production ETRM or market-access system. It excludes battery capex, taxes, grid tariffs, balancing obligations, outages, auction bid limits, partial fills and liquidity. The loss switch reacts after settlement; it cannot stop a previously committed day-ahead schedule or cap realized losses. The forecast-based budget can be exceeded by realized clearing-price spikes. Intervals are marginal, not a simultaneous guarantee over a 24-hour schedule.

Before operational use, add point-in-time data, an actual auction calendar, forecast weather/load, native quarter-hour optimization, a separately validated cost model, longer multi-regime evaluation and execution/reconciliation adapters. The current ledger assumes a single process; no multi-user access control or concurrent-writer service is provided.

## Data provenance

- [Energinet DayAheadPrices dataset](https://www.energidataservice.dk/tso-electricity/DayAheadPrices)
- [Official API guide](https://www.energidataservice.dk/guides/api-guides)

The adapter uses `TimeUTC`, `PriceArea`, and `DayAheadPriceEUR`; API start/end parameters use Danish local time. Duplicate observations, missing intervals, non-finite values and incomplete hours cause an explicit failure. Source records are not silently interpolated. This adapter supports the quarter-hour dataset from October 1, 2025 onward, not the older hourly `Elspotprices` dataset. Source hashes and retrieval timestamps are preserved in the benchmark metadata. Source data remains subject to its provider's terms; the license below applies to this project's code.

## License

MIT — see [LICENSE](LICENSE).
