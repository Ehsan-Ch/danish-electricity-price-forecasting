"""Energinet adapter. No silent filling, partial hours, or timezone guesses."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import math
from urllib.parse import urlencode
from urllib.request import urlopen

UTC = timezone.utc
SOURCE = "https://api.energidataservice.dk/dataset/DayAheadPrices"


def parse_time(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def fetch(start, end, destination):
    """Fetch once and cache. API dates are Danish local dates, end exclusive."""
    if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
        raise ValueError("start must be before end")
    if start < "2025-10-01":
        raise ValueError("DayAheadPrices starts 2025-10-01; legacy Elspotprices is not supported")
    url = SOURCE + "?" + urlencode({
        "start": start, "end": end, "filter": json.dumps({"PriceArea": ["DK1", "DK2"]}),
        "sort": "TimeUTC,PriceArea", "limit": 0,
    })
    with urlopen(url, timeout=90) as response:
        raw = response.read()
    payload = json.loads(raw)
    hourly(payload)  # Validate before caching; failure leaves previous data intact.
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    meta = {"url": url, "retrieved_utc": datetime.now(UTC).isoformat(),
            "sha256": hashlib.sha256(raw).hexdigest(), "records": len(payload["records"]),
            "source": "Energinet / Energi Data Service", "unit": "EUR/MWh",
            "start_local_inclusive": start, "end_local_exclusive": end}
    path.with_suffix(".metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def hourly(payload):
    """Convert four quarter-hour delivery products to one equal-weight hour."""
    groups, seen = {}, set()
    for row in payload["records"]:
        area = row["PriceArea"]
        if area not in ("DK1", "DK2"):
            continue
        stamp = parse_time(row["TimeUTC"])
        price = float(row["DayAheadPriceEUR"])
        if not math.isfinite(price) or stamp.minute not in (0, 15, 30, 45) or stamp.second or stamp.microsecond:
            raise ValueError("Invalid quarter-hour price record")
        if (area, stamp) in seen:
            raise ValueError("Duplicate delivery interval")
        seen.add((area, stamp))
        groups.setdefault((area, stamp.replace(minute=0)), []).append(price)
    result = {"DK1": {}, "DK2": {}}
    for (area, stamp), values in groups.items():
        if len(values) != 4:
            raise ValueError(f"Incomplete hour: {area} {stamp}")
        result[area][stamp] = sum(values) / 4
    for area, prices in result.items():
        if not prices:
            raise ValueError(f"No observations for {area}")
        stamps = sorted(prices)
        if any(b - a != timedelta(hours=1) for a, b in zip(stamps, stamps[1:])):
            raise ValueError(f"Missing hour in {area}; repair source data explicitly")
    if result["DK1"].keys() != result["DK2"].keys():
        raise ValueError("Area coverage differs")
    return result


def load(path):
    return hourly(json.loads(Path(path).read_text(encoding="utf-8")))
