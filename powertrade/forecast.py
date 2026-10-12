"""Ridge forecasts with conservative availability and chronological calibration."""
from datetime import timedelta
import math
import numpy as np

LAGS = (72, 96, 168)


def features(prices, target, issue):
    # Assume a price is usable only 24 hours after delivery. Conservative by design:
    # archived data has no publication/revision history, so this is not an as-of feed.
    needed = [target - timedelta(hours=h) for h in LAGS]
    needed += [target - timedelta(hours=h) for h in range(72, 96)]
    if any(t + timedelta(hours=24) > issue for t in needed):
        raise ValueError("Feature not available at forecast issue time")
    window = [prices[t] for t in needed[3:]]
    h, w = target.hour, target.weekday()
    return [prices[t] for t in needed[:3]] + [
        float(np.mean(window)), float(np.std(window)),
        math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24),
        math.sin(4 * math.pi * h / 24), math.cos(4 * math.pi * h / 24),
        math.sin(2 * math.pi * w / 7), math.cos(2 * math.pi * w / 7), float(w >= 5),
    ]


class Ridge:
    def fit(self, x, y):
        x, y = np.asarray(x), np.asarray(y)
        self.mean, self.scale = x.mean(axis=0), x.std(axis=0)
        self.scale[self.scale < 1e-8] = 1
        z = np.column_stack((np.ones(len(x)), (x - self.mean) / self.scale))
        penalty = np.eye(z.shape[1]) * 10.0
        penalty[0, 0] = 0
        self.coef = np.linalg.solve(z.T @ z + penalty, z.T @ y)
        return self

    def predict(self, x):
        x = np.asarray(x)
        return np.column_stack((np.ones(len(x)), (x - self.mean) / self.scale)) @ self.coef


def predict_day(prices, day):
    """At 09:00 UTC on D-1, predict all 24 UTC hours of D.

    Fit on preceding 90 days, reserve final 14 eligible days for calibration.
    Neither fit nor calibration uses any price delivered after issue minus 24h.
    """
    issue = day - timedelta(hours=15)
    cutoff = issue - timedelta(hours=24)
    candidates = sorted(t for t in prices if cutoff - timedelta(days=90) <= t < cutoff)
    rows = []
    for t in candidates:
        historical_issue = t.replace(hour=0) - timedelta(hours=15)
        try:
            x = features(prices, t, historical_issue)
        except KeyError:
            continue
        rows.append((t, x, prices[t]))
    fit_rows = [r for r in rows if r[0] < cutoff - timedelta(days=14)]
    cal_rows = [r for r in rows if r[0] >= cutoff - timedelta(days=14)]
    if len(fit_rows) < 24 * 30 or len(cal_rows) < 24 * 7:
        raise ValueError("At least about 60 days of complete history required")
    model = Ridge().fit([r[1] for r in fit_rows], [r[2] for r in fit_rows])
    residuals = np.abs(np.asarray([r[2] for r in cal_rows]) - model.predict([r[1] for r in cal_rows]))
    # Finite-sample split conformal rank, with empirical coverage reported separately.
    rank = min(len(residuals), math.ceil((len(residuals) + 1) * 0.90))
    radius = float(np.sort(residuals)[rank - 1])
    targets = [day + timedelta(hours=h) for h in range(24)]
    estimates = model.predict([features(prices, t, issue) for t in targets])
    return [{"delivery": t.isoformat(), "issue": issue.isoformat(), "forecast": float(p),
             "lower": float(p - radius), "upper": float(p + radius),
             "baseline_72h": prices[t - timedelta(hours=72)],
             "baseline_168h": prices[t - timedelta(hours=168)]} for t, p in zip(targets, estimates)]
