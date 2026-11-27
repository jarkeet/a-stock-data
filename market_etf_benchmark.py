"""Daily close series for the broad index that best tracked the ETF pool in validation."""
from datetime import date
import math

import requests

SYMBOL = "sh000300"
KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get"

# Compare candidate daily index returns with their weighted pool return.
# Use previous-session exchange shares times unadjusted ETF close as the weights.
REFERENCE = {
    "name": "沪深300",
    "code": "000300",
    "correlation": .9802,
    "sampleDays": 726,
    "first": "2023-11-27",
    "last": "2026-11-26",
}


def init_schema(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS etf_benchmark_closes (
            symbol TEXT, day TEXT, close REAL NOT NULL, PRIMARY KEY(symbol, day))
    """)


def recent_closes(through):
    response = requests.get(KLINE_URL, params={"param": f"{SYMBOL},day,,,400,none"}, timeout=8)
    response.raise_for_status()
    rows = response.json()["data"][SYMBOL]["day"]
    closes = []
    for day, _, close, *_ in rows:
        day = date.fromisoformat(day).isoformat()
        value = float(close)
        if day <= through and math.isfinite(value) and value > 0:
            closes.append((SYMBOL, day, value))
    return closes


def refresh(db, through):
    if db.execute("SELECT 1 FROM etf_benchmark_closes WHERE symbol=? AND day=?", (SYMBOL, through)).fetchone():
        return True
    closes = recent_closes(through)
    if not any(day == through for _, day, _ in closes):
        return False
    with db:
        db.executemany("INSERT OR REPLACE INTO etf_benchmark_closes VALUES (?,?,?)", closes)
    return True


def attach_closes(db, rows):
    if not rows:
        return
    values = dict(db.execute("""
        SELECT day,close FROM etf_benchmark_closes
        WHERE symbol=? AND day BETWEEN ? AND ?
    """, (SYMBOL, rows[0]["day"], rows[-1]["day"])))
    for row in rows:
        row["indexClose"] = values.get(row["day"])
