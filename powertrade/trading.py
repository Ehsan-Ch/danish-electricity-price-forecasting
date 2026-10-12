"""SQLite paper trade lifecycle and physically constrained battery scheduling."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import sqlite3


@dataclass(frozen=True)
class Limits:
    capacity_mwh: float = 8.0
    power_mw: float = 2.0
    efficiency: float = 0.95  # each direction; round trip = 90.25%
    soc_step_mwh: float = 0.5
    cost_eur_mwh: float = 2.0  # degradation + fees per grid MWh
    slippage_eur_mwh: float = 0.5
    daily_gross_budget_eur: float = 10000.0
    daily_loss_limit_eur: float = 1500.0

    def __post_init__(self):
        if not all(math.isfinite(x) for x in asdict(self).values()):
            raise ValueError("Limits must be finite")
        if min(self.capacity_mwh, self.power_mw, self.soc_step_mwh,
               self.daily_gross_budget_eur, self.daily_loss_limit_eur) <= 0:
            raise ValueError("Capacity, power and risk limits must be positive")
        if not 0 < self.efficiency <= 1 or min(self.cost_eur_mwh, self.slippage_eur_mwh) < 0:
            raise ValueError("Invalid efficiency or costs")
        if abs(self.capacity_mwh / self.soc_step_mwh - round(self.capacity_mwh / self.soc_step_mwh)) > 1e-8:
            raise ValueError("Capacity must be an integer multiple of SoC step")


def grid_energy(delta_soc, limits):
    return delta_soc / limits.efficiency if delta_soc >= 0 else delta_soc * limits.efficiency


def schedule(forecasts, limits):
    """24-hour dynamic program, empty-to-empty battery, downside price bounds.

    Positive quantity buys energy from the grid; negative sells. Each order is
    a one-hour constant-power block (four equal quarter-hour allocations).
    """
    if len(forecasts) != 24:
        raise ValueError("A full 24-hour UTC forecast is required")
    states = round(limits.capacity_mwh / limits.soc_step_mwh) + 1
    values = {0: (0.0, [])}
    for row in forecasts:
        nxt = {}
        for old, (profit, actions) in values.items():
            for new in range(states):
                delta = (new - old) * limits.soc_step_mwh
                quantity = grid_energy(delta, limits)
                if abs(quantity) > limits.power_mw + 1e-8:
                    continue
                price = row["upper"] if quantity > 0 else row["lower"]
                reward = -quantity * price - abs(quantity) * (limits.cost_eur_mwh + limits.slippage_eur_mwh)
                candidate = profit + reward
                if new not in nxt or candidate > nxt[new][0]:
                    nxt[new] = (candidate, actions + [{**row, "quantity_mwh": quantity, "delta_soc": delta}])
        values = nxt
    return [r for r in values[0][1] if abs(r["quantity_mwh"]) > 1e-8]


class Ledger:
    """Persistent one-battery-per-area ledger with append-only lifecycle events.

    This models a paper day-ahead fill at clearing price, with explicit adverse
    slippage. It does not represent a limit-order book, broker, or live exchange.
    """
    def __init__(self, path, limits=None):
        self.limits = limits or Limits()
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS batteries (area TEXT PRIMARY KEY, soc REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY, area TEXT, delivery TEXT, issue TEXT, quantity REAL,
          delta_soc REAL, forecast REAL, lower REAL, upper REAL, status TEXT,
          fill REAL, pnl REAL, reason TEXT,
          UNIQUE(area, delivery));
        CREATE TABLE IF NOT EXISTS events (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, recorded_utc TEXT,
          order_id TEXT, status TEXT, detail TEXT);
        """)
        settings = json.dumps(asdict(self.limits), sort_keys=True)
        prior = self.db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        if prior and prior[0] != settings:
            self.db.close()
            raise ValueError("Existing ledger has different risk limits")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)", (settings,))
            for area in ("DK1", "DK2"):
                self.db.execute("INSERT OR IGNORE INTO batteries VALUES (?,0)", (area,))

    def close(self):
        self.db.close()

    def event(self, oid, status, detail):
        self.db.execute("INSERT INTO events(recorded_utc,order_id,status,detail) VALUES(?,?,?,?)",
                        (datetime.now(timezone.utc).isoformat(), oid, status, detail))

    def propose(self, area, rows):
        if area not in ("DK1", "DK2"):
            raise ValueError("Unsupported price area")
        with self.db:
            for r in rows:
                fields = ("quantity_mwh", "delta_soc", "forecast", "lower", "upper")
                if not all(math.isfinite(r[k]) for k in fields) or r["lower"] > r["forecast"] or r["forecast"] > r["upper"]:
                    raise ValueError("Invalid order values or price bounds")
                delivery, issue = datetime.fromisoformat(r["delivery"]), datetime.fromisoformat(r["issue"])
                if delivery.utcoffset() != timezone.utc.utcoffset(delivery) or issue.utcoffset() != timezone.utc.utcoffset(issue):
                    raise ValueError("Order timestamps must be aware UTC")
                if issue >= delivery or delivery.minute or delivery.second or delivery.microsecond:
                    raise ValueError("Issue must precede hourly delivery")
                oid = area + ":" + delivery.isoformat()
                self.db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,'PROPOSED',NULL,NULL,NULL)",
                                (oid, area, delivery.isoformat(), issue.isoformat(), r["quantity_mwh"],
                                 r["delta_soc"], r["forecast"], r["lower"], r["upper"]))
                self.event(oid, "PROPOSED", "Forecast-driven battery schedule")

    def approve_day(self, area, day):
        """All-or-none approval; validate the complete physical path and gross cost."""
        with self.db:
            rows = self.db.execute("SELECT * FROM orders WHERE area=? AND substr(delivery,1,10)=? ORDER BY delivery", (area, day)).fetchall()
            if not rows:
                return "NO_TRADES"
            if any(r["status"] != "PROPOSED" for r in rows):
                raise ValueError("Approval requires all orders to be proposed")
            pending = self.db.execute("SELECT COUNT(*) FROM orders WHERE area=? AND status='APPROVED'", (area,)).fetchone()[0]
            if pending:
                raise ValueError("Settle or cancel existing approved positions first")
            soc = self.db.execute("SELECT soc FROM batteries WHERE area=?", (area,)).fetchone()[0]
            last = self.db.execute("SELECT MAX(delivery) FROM orders WHERE area=? AND status='FILLED'", (area,)).fetchone()[0]
            if last and rows[0]["delivery"] <= last:
                raise ValueError("Cannot approve past deliveries after settled trades")
            budget, reason = 0.0, ""
            for r in rows:
                q, delta = r["quantity"], r["delta_soc"]
                soc += delta
                budget += abs(q) * (max(abs(r["lower"]), abs(r["upper"])) + self.limits.cost_eur_mwh + self.limits.slippage_eur_mwh)
                if abs(q) > self.limits.power_mw + 1e-8 or abs(q - grid_energy(delta, self.limits)) > 1e-8 or abs(q) < 1e-8:
                    reason = "Power or energy conversion limit"
                if soc < -1e-8 or soc > self.limits.capacity_mwh + 1e-8:
                    reason = "Battery capacity limit"
            if abs(soc) > 1e-8:
                reason = "Schedule must end with an empty battery"
            if budget > self.limits.daily_gross_budget_eur:
                reason = "Forecast gross cash-flow budget exceeded"
            previous = self.db.execute("SELECT substr(delivery,1,10),SUM(pnl) FROM orders WHERE area=? AND status='FILLED' GROUP BY 1 ORDER BY 1 DESC LIMIT 1", (area,)).fetchone()
            if previous and previous[1] <= -self.limits.daily_loss_limit_eur:
                reason = "Prior settled day's loss breached kill switch; manual review required"
            status = "REJECTED" if reason else "APPROVED"
            for r in rows:
                self.db.execute("UPDATE orders SET status=?,reason=? WHERE id=?", (status, reason, r["id"]))
                self.event(r["id"], status, reason or "Physical and pre-trade risk checks passed")
            return status

    def cancel_day(self, area, day):
        with self.db:
            rows = self.db.execute("SELECT * FROM orders WHERE area=? AND substr(delivery,1,10)=?", (area, day)).fetchall()
            if not rows or any(r["status"] not in ("PROPOSED", "APPROVED") for r in rows):
                raise ValueError("Only a complete unfilled schedule can be cancelled")
            for r in rows:
                self.db.execute("UPDATE orders SET status='CANCELLED' WHERE id=?", (r["id"],))
                self.event(r["id"], "CANCELLED", "Whole schedule cancelled before paper execution")

    def settle_day(self, area, day, actual):
        """Settle the entire approved schedule atomically, in delivery order."""
        with self.db:
            rows = self.db.execute("SELECT * FROM orders WHERE area=? AND substr(delivery,1,10)=? ORDER BY delivery", (area, day)).fetchall()
            if not rows or any(r["status"] != "APPROVED" for r in rows):
                raise ValueError("Settlement requires a complete approved schedule")
            for r in rows:
                price = actual[r["delivery"]]
                if not math.isfinite(price):
                    raise ValueError("Non-finite settlement price")
                fill = price + math.copysign(self.limits.slippage_eur_mwh, r["quantity"])
                pnl = -r["quantity"] * fill - abs(r["quantity"]) * self.limits.cost_eur_mwh
                self.db.execute("UPDATE orders SET status='FILLED',fill=?,pnl=? WHERE id=?", (fill, pnl, r["id"]))
                self.db.execute("UPDATE batteries SET soc=soc+? WHERE area=?", (r["delta_soc"], area))
                self.event(r["id"], "FILLED", json.dumps({"clearing_price": price, "fill": fill, "cash_flow": pnl}))

    def orders(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM orders ORDER BY delivery,area")]
