from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import math
import sqlite3
import tempfile
import unittest

from powertrade.data import hourly
from powertrade.forecast import features, predict_day
from powertrade.trading import Ledger, Limits, schedule
from powertrade.pipeline import run

UTC = timezone.utc


def synthetic(days=85):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return {start + timedelta(hours=i): 50 + 30 * math.sin(2 * math.pi * (i % 24) / 24) + 5 * math.cos(i / 60)
            for i in range(days * 24)}


def order(hour, quantity, delta):
    return {'delivery': f'2026-05-01T{hour:02d}:00:00+00:00', 'issue': '2026-04-30T09:00:00+00:00',
            'quantity_mwh': quantity, 'delta_soc': delta, 'forecast': 50., 'lower': 40., 'upper': 60.}


class DataTests(unittest.TestCase):
    def records(self):
        return {'records': [{'PriceArea': area, 'TimeUTC': f'2026-10-25T01:{minute:02d}:00',
                             'DayAheadPriceEUR': -20 + minute}
                            for area in ('DK1', 'DK2') for minute in (0, 15, 30, 45)]}

    def test_average_and_utc_dst(self):
        result = hourly(self.records())
        self.assertEqual(result['DK1'][datetime(2026, 10, 25, 1, tzinfo=UTC)], 2.5)

    def test_duplicate_rejected(self):
        data = self.records()
        data['records'].append(data['records'][0])
        with self.assertRaises(ValueError):
            hourly(data)

    def test_missing_quarter_rejected(self):
        data = self.records()
        data['records'].pop()
        with self.assertRaises(ValueError):
            hourly(data)

    def test_nonfinite_rejected(self):
        data = self.records()
        data['records'][0]['DayAheadPriceEUR'] = float('nan')
        with self.assertRaises(ValueError):
            hourly(data)


class ForecastTests(unittest.TestCase):
    def test_future_data_cannot_change_predictions_or_intervals(self):
        prices = synthetic()
        day = datetime(2026, 3, 20, tzinfo=UTC)
        original = predict_day(prices, day)
        cutoff = day - timedelta(hours=39)
        changed = {t: v if t < cutoff else v + 100000 for t, v in prices.items()}
        self.assertEqual(original, predict_day(changed, day))

    def test_availability_guard(self):
        prices = synthetic()
        day = datetime(2026, 3, 20, tzinfo=UTC)
        with self.assertRaises(ValueError):
            features(prices, day, day - timedelta(days=4))

    def test_forecast_without_future_actuals(self):
        prices = synthetic()
        day = max(prices).replace(hour=0) + timedelta(days=1)
        result = predict_day(prices, day)
        self.assertEqual(len(result), 24)
        self.assertTrue(all(r['lower'] <= r['forecast'] <= r['upper'] for r in result))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'ledger.sqlite'
        self.limits = Limits(efficiency=1.)
        self.ledger = Ledger(self.path, self.limits)

    def tearDown(self):
        self.ledger.close()
        self.directory.cleanup()

    def pair(self):
        return [order(1, 1, 1), order(2, -1, -1)]

    def test_complete_lifecycle_negative_prices_and_persistence(self):
        rows = self.pair()
        self.ledger.propose('DK1', rows)
        self.assertEqual(self.ledger.approve_day('DK1', '2026-05-01'), 'APPROVED')
        self.ledger.settle_day('DK1', '2026-05-01', {rows[0]['delivery']: -10., rows[1]['delivery']: 100.})
        self.assertAlmostEqual(sum(r['pnl'] for r in self.ledger.orders()), 105.)
        self.assertAlmostEqual(self.ledger.db.execute("SELECT soc FROM batteries WHERE area='DK1'").fetchone()[0], 0.)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 6)
        self.ledger.close()
        self.ledger = Ledger(self.path, self.limits)
        self.assertTrue(all(r['status'] == 'FILLED' for r in self.ledger.orders()))
        with self.assertRaises(ValueError):
            self.ledger.settle_day('DK1', '2026-05-01', {})

    def test_settlement_rolls_back_on_missing_price(self):
        rows = self.pair()
        self.ledger.propose('DK1', rows)
        self.ledger.approve_day('DK1', '2026-05-01')
        with self.assertRaises(KeyError):
            self.ledger.settle_day('DK1', '2026-05-01', {rows[0]['delivery']: 10.})
        self.assertTrue(all(r['status'] == 'APPROVED' for r in self.ledger.orders()))
        self.assertEqual(self.ledger.db.execute("SELECT soc FROM batteries WHERE area='DK1'").fetchone()[0], 0.)

    def test_duplicate_proposals_do_not_duplicate_events(self):
        self.ledger.propose('DK1', self.pair())
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.propose('DK1', self.pair())
        self.assertEqual(len(self.ledger.orders()), 2)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 2)

    def test_power_capacity_conversion_and_budget_rejections(self):
        cases = [([order(1, 3, 3), order(2, -3, -3)], Limits(efficiency=1)),
                 ([order(1, -1, -1), order(2, 1, 1)], Limits(efficiency=1)),
                 ([order(1, 1, 2), order(2, -1, -2)], Limits(efficiency=1)),
                 (self.pair(), Limits(efficiency=1, daily_gross_budget_eur=10))]
        for i, (rows, limits) in enumerate(cases):
            ledger = Ledger(Path(self.directory.name) / f'{i}.sqlite', limits)
            try:
                ledger.propose('DK1', rows)
                self.assertEqual(ledger.approve_day('DK1', '2026-05-01'), 'REJECTED')
            finally:
                ledger.close()

    def test_cancel_and_illegal_settlement(self):
        self.ledger.propose('DK1', self.pair())
        self.ledger.approve_day('DK1', '2026-05-01')
        self.ledger.cancel_day('DK1', '2026-05-01')
        with self.assertRaises(ValueError):
            self.ledger.settle_day('DK1', '2026-05-01', {})
        self.assertTrue(all(r['status'] == 'CANCELLED' for r in self.ledger.orders()))

    def test_loss_kill_switch(self):
        rows = self.pair()
        self.ledger.propose('DK1', rows)
        self.ledger.approve_day('DK1', '2026-05-01')
        self.ledger.settle_day('DK1', '2026-05-01', {rows[0]['delivery']: 2000., rows[1]['delivery']: 0.})
        later = [{**r, 'delivery': r['delivery'].replace('05-01', '05-02')} for r in rows]
        self.ledger.propose('DK1', later)
        self.assertEqual(self.ledger.approve_day('DK1', '2026-05-02'), 'REJECTED')

    def test_nonfinite_and_naive_timestamp_rejected(self):
        for field, value in [('quantity_mwh', float('inf')), ('delivery', '2026-05-01T01:00:00')]:
            bad = {**order(1, 1, 1), field: value}
            with self.assertRaises(ValueError):
                self.ledger.propose('DK1', [bad])


class SchedulerTests(unittest.TestCase):
    def test_flat_price_never_trades_and_spread_respects_constraints(self):
        limits = Limits()
        flat = [dict(order(h, 0, 0), lower=50., upper=50.) for h in range(24)]
        self.assertEqual(schedule(flat, limits), [])
        spread = [dict(r, lower=-20. if i < 12 else 150., upper=-20. if i < 12 else 150.) for i, r in enumerate(flat)]
        actions = schedule(spread, limits)
        self.assertGreater(len(actions), 0)
        soc = 0
        for r in actions:
            self.assertLessEqual(abs(r['quantity_mwh']), limits.power_mw + 1e-8)
            soc += r['delta_soc']
            self.assertGreaterEqual(soc, -1e-8)
            self.assertLessEqual(soc, limits.capacity_mwh + 1e-8)
        self.assertAlmostEqual(soc, 0.)


class PipelineTests(unittest.TestCase):
    def test_end_to_end_outputs_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            records = [{'PriceArea': area, 'TimeUTC': (t + timedelta(minutes=m)).isoformat(), 'DayAheadPriceEUR': p}
                       for t, p in synthetic(65).items() for m in (0, 15, 30, 45) for area in ('DK1', 'DK2')]
            (path / 'prices.json').write_text(json.dumps({'records': records}), encoding='utf-8')
            metrics = run(path / 'prices.json', path / 'report', test_days=2)
            self.assertEqual(metrics['areas']['DK1']['observations'], 48)
            self.assertTrue((path / 'report' / 'report.html').is_file())
            self.assertTrue((path / 'report' / 'trades.sqlite').is_file())
            with self.assertRaises(ValueError):
                run(path / 'prices.json', path / 'report', test_days=2)


if __name__ == '__main__':
    unittest.main()
