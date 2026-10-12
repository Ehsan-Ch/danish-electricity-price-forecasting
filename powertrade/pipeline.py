from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
import csv
import json
import math
from .data import load, parse_time
from .forecast import predict_day
from .report import render
from .trading import Ledger, Limits, schedule


def save_csv(path, rows):
    if rows:
        with Path(path).open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def run(source, destination, test_days=30, limits=None):
    if test_days < 1:
        raise ValueError("test_days must be positive")
    prices = load(source)
    limits = limits or Limits()
    output = Path(destination)
    output.mkdir(parents=True, exist_ok=True)
    dbpath = output / "trades.sqlite"
    if dbpath.exists():
        raise ValueError("Output ledger already exists. Choose a fresh output directory.")
    latest = min(max(p) for p in prices.values())
    last_day = latest.replace(hour=0)
    if latest.hour != 23:
        last_day -= timedelta(days=1)
    first_day = last_day - timedelta(days=test_days - 1)
    # Preflight history before creating a ledger.
    for area in prices:
        predict_day(prices[area], first_day)
    ledger = Ledger(dbpath, limits)
    predictions, daily = [], []
    try:
        for n in range(test_days):
            day = first_day + timedelta(days=n)
            for area, series in prices.items():
                forecast = predict_day(series, day)
                actions = schedule(forecast, limits)
                ledger.propose(area, actions)
                status = ledger.approve_day(area, day.date().isoformat())
                if status == "APPROVED":
                    ledger.settle_day(area, day.date().isoformat(), {r["delivery"]: series[parse_time(r["delivery"])] for r in forecast})
                for row in forecast:
                    predictions.append({"area": area, **row, "actual": series[parse_time(row["delivery"])]})
                day_orders = [r for r in ledger.orders() if r["area"] == area and r["delivery"][:10] == day.date().isoformat()]
                daily.append({"day": day.date().isoformat(), "area": area,
                              "pnl_eur": sum(r["pnl"] or 0 for r in day_orders), "status": status,
                              "throughput_mwh": sum(abs(r["quantity"]) for r in day_orders if r["status"] == "FILLED")})
        orders = ledger.orders()
    finally:
        ledger.close()
    metrics = {"test_start": first_day.date().isoformat(), "test_end": last_day.date().isoformat(),
               "test_days": test_days, "data_kind": "historical Energinet observations",
               "evaluation": "daily rolling origin; 90-day window; final 14 eligible days held out for calibration",
               "issue_time": "09:00 UTC on the day before the target UTC day",
               "availability_rule": "delivery timestamp + 24h <= issue; training targets strictly earlier",
               "interval": "90% split calibration; temporal coverage not guaranteed",
               "limits": asdict(limits), "areas": {}}
    meta = Path(source).with_suffix('.metadata.json')
    if meta.exists():
        metrics['provenance'] = json.loads(meta.read_text(encoding='utf-8'))
    for area in prices:
        rows = [r for r in predictions if r['area'] == area]
        errors = [r['forecast'] - r['actual'] for r in rows]
        total, peak, dd = 0.0, 0.0, 0.0
        for r in [r for r in daily if r['area'] == area]:
            total += r['pnl_eur']
            peak = max(peak, total)
            dd = max(dd, peak - total)
        metrics['areas'][area] = {
            'observations': len(rows), 'mae': sum(abs(e) for e in errors) / len(errors),
            'rmse': math.sqrt(sum(e * e for e in errors) / len(errors)),
            'baseline_72h_mae': sum(abs(r['actual'] - r['baseline_72h']) for r in rows) / len(rows),
            'baseline_168h_mae': sum(abs(r['actual'] - r['baseline_168h']) for r in rows) / len(rows),
            'coverage': sum(r['lower'] <= r['actual'] <= r['upper'] for r in rows) / len(rows),
            'mean_interval_width': sum(r['upper'] - r['lower'] for r in rows) / len(rows),
            'paper_pnl_eur': total, 'max_drawdown_eur': dd,
            'filled_orders': sum(r['area'] == area and r['status'] == 'FILLED' for r in orders),
            'rejected_orders': sum(r['area'] == area and r['status'] == 'REJECTED' for r in orders),
        }
    (output / 'metrics.json').write_text(json.dumps(metrics, indent=2), encoding='utf-8')
    save_csv(output / 'predictions.csv', predictions)
    save_csv(output / 'daily_pnl.csv', daily)
    save_csv(output / 'orders.csv', orders)
    (output / 'report.html').write_text(render(metrics, predictions, daily, orders), encoding='utf-8')
    return metrics
