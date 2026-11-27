"""Local ETF turnover monitor. Amounts use CNY and bar times mark interval ends."""

from argparse import ArgumentParser
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
from statistics import median
from threading import Event, Thread
import time
from zoneinfo import ZoneInfo

import requests

import market_etf_benchmark as benchmark
import market_etf_evidence as evidence
import market_etf_shares as shares
from market_etf_profiles import TurnoverProfiles, archive_minutes

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "market_etf_monitor.sqlite3"
TZ = ZoneInfo("Asia/Shanghai")
QUOTE_URL = "https://qt.gtimg.cn/q="
DAY_URL = "https://web.ifzq.gtimg.cn/appstock/app/day/query"
HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
MIN_DAYS = 30
HISTORY_DAYS = 60
AM_SESSION = (570, 690)
PM_SESSION = (780, 897)
SESSIONS = (AM_SESSION, PM_SESSION)
BOUNDARY_TOLERANCE = 22
CLOSE_MINUTE = 900
CLOSE_AMOUNT_TOLERANCE = 500
LOG = logging.getLogger(__name__)

HUIJIN_HISTORY = "https://www.pbcsf.tsinghua.edu.cn/__local/3/2E/DC/40695B706B16C86F79330D398EA_E275D40F_3E5ECA.pdf#page=25"
GUOXIN_REPORT = "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-31/560170_20260331_G58R.pdf"
CHENGTONG_REPORT = "https://static.cninfo.com.cn/finalpage/2026-03-27/1225036603.PDF"

# Per-fund floor, in CNY millions. Floors are initial alert parameters.
GROUPS = (
    ("hs300", "沪深300", "main", (("510300", "华泰柏瑞", 10), ("510310", "易方达", 5),
                                ("510330", "华夏", 5), ("159919", "嘉实", 5))),
    ("sh50", "上证50", "main", (("510050", "华夏", 5), ("510100", "易方达", 1))),
    ("zz500", "中证500", "main", (("510500", "南方", 8), ("512500", "华夏", 3), ("159922", "嘉实", 2))),
    ("zz1000", "中证1000", "main", (("512100", "南方", 8), ("159845", "华夏", 5),
                                  ("560010", "广发", 3), ("159629", "富国", 2))),
    ("chinext", "创业板指", "main", (("159915", "易方达", 8), ("159952", "广发", 3),
                                   ("159977", "天弘", 2))),
    ("star50", "科创50", "main", (("588080", "易方达", 5), ("588050", "工银瑞信", 2))),
    ("sh180", "上证180", "main", (("510180", "华安", 1),)),
    ("sz100", "深证100", "history", (("159901", "易方达", 1),)),
    ("zz800", "中证800", "history", (("515800", "汇添富", 1),)),
    ("msci50", "MSCI中国A50", "history", (("560050", "汇添富", 1),)),
    ("finance180", "上证180金融", "history", (("510230", "国泰", 1),)),
    ("star50", "科创50", "control", (("588000", "华夏", 5),)),
    ("a500", "中证A500", "control", (("512050", "华夏", 3), ("159361", "易方达", 5),
                                   ("159352", "南方", 5), ("159338", "国泰", 5))),
    ("guoxin", "央企科技引领", "theme", (("560170", "南方", 1),)),
    ("chengtong", "诚通央企科创", "theme", (("159335", "融通", 1),)),
)
EVIDENCE = {
    "main": ("中央汇金历史持仓", "2024-12-31", HUIJIN_HISTORY),
    "history": ("中央汇金历史持仓", "2024-12-31", HUIJIN_HISTORY),
    "guoxin": ("国新投资", "2025-12-31", GUOXIN_REPORT),
    "chengtong": ("中国诚通", "2025-12-31", CHENGTONG_REPORT),
    "510300": ("中央汇金及汇金资管", "2025-12-31",
               "https://static.howbuy.com/gkxx/ggfile/2026/2026-3/2026-03-31/5129309.pdf"),
    "588080": ("中央汇金及汇金资管", "2025-12-31",
               "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-03-31/588080_20260331_9RCR.pdf"),
}


@dataclass(frozen=True)
class Fund:
    code: str
    provider: str
    group: str
    index: str
    role: str
    minimum: int

    @property
    def symbol(self):
        return ("sz" if self.code.startswith("15") else "sh") + self.code


FUNDS = {code: Fund(code, provider, group, label, role, floor * 1_000_000)
         for group, label, role, rows in GROUPS for code, provider, floor in rows}
SYMBOLS = {fund.symbol: code for code, fund in FUNDS.items()}
PRIMARY_CODES = tuple(code for code, fund in FUNDS.items() if fund.role == "main")
CONTROL_CODES = tuple(code for code, fund in FUNDS.items() if fund.role == "control")
MIN_DIRECTION_FUNDS = math.ceil(len(PRIMARY_CODES) * .8)
PRIMARY_GROUP_COUNT = len({FUNDS[code].group for code in PRIMARY_CODES})
SCORE_RATINGS = ((20, "strong_sell"), (40, "lean_sell"), (59, "neutral"), (79, "lean_buy"), (100, "strong_buy"))


@dataclass(frozen=True)
class Quote:
    code: str
    stamp: datetime
    price: float
    change: float | None
    amount: int
    net_buy_estimate: int | None = None


def minute_of(stamp):
    return stamp.hour * 60 + stamp.minute


def session_start(minute):
    return next((start for start, end in SESSIONS if start < minute <= end), None)


def clock(minute):
    return f"{minute // 60:02d}:{minute % 60:02d}"


def finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def connect(path):
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def init_db(path):
    with closing(connect(path)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        shares.init_schema(db)
        benchmark.init_schema(db)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS quotes(
                code TEXT PRIMARY KEY, stamp TEXT, price REAL, change REAL, amount INTEGER);
            CREATE TABLE IF NOT EXISTS daily_direction(
                day TEXT, code TEXT, stamp TEXT, amount INTEGER, net_buy_estimate INTEGER,
                PRIMARY KEY(day, code));
            CREATE TABLE IF NOT EXISTS samples(
                day TEXT, minute INTEGER, code TEXT, seconds INTEGER, amount INTEGER, price REAL,
                PRIMARY KEY(day, minute, code));
            CREATE TABLE IF NOT EXISTS bars(
                span INTEGER, day TEXT, minute INTEGER, code TEXT, amount INTEGER, price REAL, source TEXT,
                PRIMARY KEY(span, day, minute, code));
            CREATE INDEX IF NOT EXISTS bars_clock ON bars(span, minute, day);
            CREATE INDEX IF NOT EXISTS bars_code ON bars(span, day, code, minute);
            CREATE TABLE IF NOT EXISTS events(
                group_id TEXT, day TEXT, first INTEGER, last INTEGER, peak INTEGER, kind TEXT,
                amount INTEGER, pct INTEGER, codes TEXT, basis INTEGER,
                PRIMARY KEY(group_id, day, first));
            CREATE INDEX IF NOT EXISTS events_day ON events(day, last);
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
        """)


def parse_quotes(body, now):
    for symbol, raw in re.findall(r'v_(\w+)="([^"]+)"', body.decode("gb18030", errors="replace")):
        fields = raw.split("~")
        code = SYMBOLS.get(symbol)
        if not code or len(fields) <= 37 or fields[2] != code:
            continue
        try:
            stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=TZ)
        except ValueError:
            continue
        if stamp > now + timedelta(seconds=5):
            continue
        price = finite(fields[3])
        parts = fields[35].split("/")
        amount = finite(parts[2]) if len(parts) == 3 else None
        if amount is None:
            amount_wan = finite(fields[37])
            amount = amount_wan * 10000 if amount_wan is not None else None
        if price and price > 0 and amount is not None and amount >= 0:
            # The quote reports volume in lots for total, outside (buy) and inside (sell).
            volume, outside, inside = map(finite, fields[6:9])
            net = None
            if (amount and volume and volume > 0 and outside is not None and inside is not None
                    and outside >= 0 and inside >= 0 and .8 <= (outside + inside) / volume <= 1.005):
                net = round(amount * (outside - inside) / max(volume, outside + inside))
            yield Quote(code, stamp, price, finite(fields[32]), round(amount), net)


def save_quotes(db, quotes, today):
    for quote in quotes:
        qday = quote.stamp.date().isoformat()
        stamp = quote.stamp.isoformat(timespec="seconds")
        db.execute("""
            INSERT INTO quotes VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET stamp=excluded.stamp, price=excluded.price,
                change=excluded.change, amount=excluded.amount WHERE excluded.stamp >= quotes.stamp
        """, (quote.code, stamp, quote.price, quote.change, quote.amount))
        if minute_of(quote.stamp) >= AM_SESSION[0]:
            db.execute("""
                INSERT INTO daily_direction VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(day,code) DO UPDATE SET stamp=excluded.stamp, amount=excluded.amount,
                    net_buy_estimate=excluded.net_buy_estimate
                WHERE excluded.stamp >= daily_direction.stamp AND NOT EXISTS (
                    SELECT 1 FROM samples closing
                    WHERE closing.day=excluded.day AND closing.code=excluded.code AND closing.minute=?
                      AND substr(daily_direction.stamp,12,5)>='15:00'
                      AND daily_direction.net_buy_estimate IS NOT NULL
                      AND abs(daily_direction.amount-closing.amount)<=?
                      AND (excluded.net_buy_estimate IS NULL OR abs(excluded.amount-closing.amount)>?)
                )
            """, (qday, quote.code, stamp, quote.amount, quote.net_buy_estimate, CLOSE_MINUTE,
                  CLOSE_AMOUNT_TOLERANCE, CLOSE_AMOUNT_TOLERANCE))
        minute = minute_of(quote.stamp)
        if quote.stamp.second:
            minute += 1
        if qday != today or (session_start(minute) is None and minute not in (570, 780)):
            continue
        seconds = minute_of(quote.stamp) * 60 + quote.stamp.second
        db.execute("""
            INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(day, minute, code) DO UPDATE SET seconds=excluded.seconds,
                amount=excluded.amount, price=excluded.price WHERE excluded.seconds >= samples.seconds
        """, (qday, minute, quote.code, seconds, quote.amount, quote.price))


def finalize_minute(db, day, minute):
    if session_start(minute) is None:
        return
    db.execute("""
        INSERT OR IGNORE INTO bars
        SELECT 1, a.day, a.minute, a.code, a.amount-b.amount, a.price, 'tencent_snapshot'
        FROM samples a JOIN samples b ON b.day=a.day AND b.code=a.code AND b.minute=a.minute-1
        WHERE a.day=? AND a.minute=? AND a.amount>=b.amount
          AND a.seconds>=a.minute*60-? AND b.seconds>=b.minute*60-?
    """, (day, minute, BOUNDARY_TOLERANCE, BOUNDARY_TOLERANCE))


def window_history(db, day, minute, span, days=None):
    """A five-minute window is eligible only when all constituent minutes exist."""
    if span == 1:
        start = session_start(minute)
        if start is None or minute < start + 5:
            return {}
    elif minute % 5 or session_start(minute) is None or minute == 575:
        # The provider's first morning five-minute bar can include the opening auction.
        return {}
    oldest = (date.fromisoformat(day) - timedelta(days=140)).isoformat()
    first = minute - 4 if span == 1 else minute
    if days is None:
        days = {row[0] for row in db.execute(
            "SELECT DISTINCT day FROM bars WHERE day<? AND day>=? ORDER BY day DESC LIMIT ?",
            (day, oldest, HISTORY_DAYS))}
    result = defaultdict(dict)
    for row in db.execute("""
        SELECT day,code,sum(amount) AS amount FROM bars
        WHERE span=? AND minute BETWEEN ? AND ? AND day BETWEEN ? AND ?
        GROUP BY day,code HAVING count(*)=?
    """, (span, first, minute, oldest, day, 5 if span == 1 else 1)):
        if row["day"] == day or row["day"] in days:
            result[row["day"]][row["code"]] = row["amount"]
    return dict(result)


def percentile(history, value):
    return round(100 * sum(previous <= value for previous in history) / len(history), 1)


def quantile(history, p):
    return sorted(history)[math.ceil(p * len(history)) - 1]


def score_windows(db, day, minute):
    rolling = window_history(db, day, minute, 1)
    alignment = minute - minute % 5
    # Use matched five-minute clocks while the rolling minute archive builds up.
    five = window_history(db, day, alignment, 5)
    aligned_live = (rolling if alignment == minute else window_history(db, day, alignment, 1)).get(day, {})
    history_by_mode = {1: rolling, 5: five}
    metrics = {}
    for code, fund in FUNDS.items():
        history = [rows[code] for d, rows in rolling.items() if d != day and code in rows]
        value = rolling.get(day, {}).get(code)
        basis = 1
        aligned = aligned_live.get(code, five.get(day, {}).get(code))
        aligned_history = [rows[code] for d, rows in five.items() if d != day and code in rows]
        if len(history) < MIN_DAYS and len(aligned_history) > len(history):
            history, value, basis = aligned_history, aligned, 5
        med = median(history) if history else 0
        ready = value is not None and med > 0 and len(history) >= MIN_DAYS
        metrics[code] = {
            "amount": value,
            "baselineDays": len(history),
            "basis": basis,
            "clock": clock(minute if basis == 1 else alignment),
            "percentile": percentile(history, value) if ready else None,
            "multiple": round(value / med, 2) if ready else None,
            "trigger": bool(ready and value >= max(quantile(history, .95), 2 * med, fund.minimum)),
        }
    return metrics, history_by_mode


def record_events(db, day, minute, metrics, history_by_mode):
    for group, _, role, rows in GROUPS:
        if role != "main":
            continue
        candidates = [code for code, _, _ in rows if metrics[code]["trigger"]
                      and (metrics[code]["basis"] == 1 or minute % 5 == 0)]
        if not candidates:
            continue
        # Compare group turnover with the same set of funds at the same clock.
        leader = max(candidates, key=lambda code: metrics[code]["amount"])
        event_codes = [leader]
        result = metrics[leader]
        amount, pct, basis = result["amount"], result["percentile"], result["basis"]
        kind = "single"
        for mode in (1, 5):
            matched = [code for code in candidates if metrics[code]["basis"] == mode]
            if len(matched) < 2:
                continue
            totals = [sum(values[code] for code in matched) for d, values in history_by_mode[mode].items()
                      if d != day and all(code in values for code in matched)]
            total = sum(metrics[code]["amount"] for code in matched)
            if len(totals) >= MIN_DAYS and total >= quantile(totals, .99):
                event_codes, amount, pct, basis, kind = matched, total, percentile(totals, total), mode, "group"
                break
        existing = db.execute("""
            SELECT * FROM events WHERE day=? AND group_id=? AND last BETWEEN ? AND ?
            ORDER BY last DESC LIMIT 1
        """, (day, group, max(session_start(minute), minute - 10), minute)).fetchone()
        if existing:
            peak = kind == "group" and existing["kind"] == "single"
            peak = peak or (kind == existing["kind"] and amount > existing["amount"])
            if peak:
                db.execute("""
                    UPDATE events SET last=?,peak=?,kind=?,amount=?,pct=?,codes=?,basis=?
                    WHERE group_id=? AND day=? AND first=?
                """, (minute, minute, kind, amount, pct, json.dumps(event_codes), basis,
                      group, day, existing["first"]))
            elif minute > existing["last"]:
                db.execute("UPDATE events SET last=? WHERE group_id=? AND day=? AND first=?",
                           (minute, group, day, existing["first"]))
        else:
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (group, day, minute, minute, minute, kind, amount, pct, json.dumps(event_codes), basis))


def update_state(db, now, phase, coverage):
    db.execute("INSERT OR REPLACE INTO state VALUES ('collector',?)",
               (json.dumps({"stamp": now.isoformat(timespec="seconds"), "phase": phase, "count": coverage}),))


def collect(path, stop):
    with closing(connect(path)) as db, requests.Session() as client:
        client.headers["User-Agent"] = "Mozilla/5.0"
        finished = None
        bootstrapped = False
        while not stop.is_set():
            now = datetime.now(TZ)
            day = now.date().isoformat()
            minute = minute_of(now)
            active = now.weekday() < 5 and (569 <= minute <= 690 or 779 <= minute <= CLOSE_MINUTE + 4)
            try:
                quotes = []
                if active or not bootstrapped:
                    response = client.get(QUOTE_URL + ",".join(SYMBOLS), timeout=5)
                    response.raise_for_status()
                    now = datetime.now(TZ)
                    quotes = list(parse_quotes(response.content, now))
                    if not quotes:
                        raise ValueError("empty ETF quote batch")
                    bootstrapped = True
                fresh = [q for q in quotes if -5 <= (now - q.stamp).total_seconds() <= 25]
                with db:
                    save_quotes(db, quotes, day)
                    target = minute_of(now) - (now.second < 10)
                    if active and session_start(target) is not None and (day, target) != finished:
                        finalize_minute(db, day, target)
                        if db.execute("SELECT 1 FROM bars WHERE span=1 AND day=? AND minute=? LIMIT 1",
                                      (day, target)).fetchone():
                            metrics, history = score_windows(db, day, target)
                            record_events(db, day, target, metrics, history)
                        finished = (day, target)
                    continuous = active and (570 <= minute < 690 or 780 <= minute < 897)
                    if active and PM_SESSION[1] <= minute < CLOSE_MINUTE:
                        phase = "auction"
                    else:
                        phase = ("live" if fresh else "waiting") if continuous else "closed"
                    update_state(db, now, phase, len(fresh))
            except (requests.RequestException, ValueError, sqlite3.Error) as error:
                LOG.warning("ETF collector: %s", error)
                try:
                    with db:
                        update_state(db, now, "waiting", 0)
                except sqlite3.Error:
                    LOG.exception("ETF collector could not update its status")
            # Wake for the afternoon boundary even after the quiet lunch heartbeat.
            stop.wait(7 if active else 15)


def start_collector(path):
    init_db(path)
    stop = Event()
    worker = Thread(target=collect, args=(path, stop), name="etf-quotes", daemon=True)
    catchup = Thread(target=warm_minute_history, args=(path, stop), name="etf-history", daemon=True)
    reference = Thread(target=refresh_reference_data, args=(path, stop), name="etf-reference", daemon=True)
    worker.start()
    catchup.start()
    reference.start()
    return stop, (worker, catchup, reference)


def refresh_reference_data(path, stop):
    refreshed = None
    archived = None
    benchmarked = None
    with closing(connect(path)) as db:
        while not stop.is_set():
            now = datetime.now(TZ)
            day = now.date().isoformat()
            # Use Shenzhen's following-morning official figures.
            cutoff = now.date() if minute_of(now) >= 525 else now.date() - timedelta(days=1)
            try:
                if cutoff != refreshed:
                    if shares.refresh(db, FUNDS, cutoff.isoformat(), stop):
                        refreshed = cutoff
                if archived != day and minute_of(now) >= CLOSE_MINUTE + 1:
                    if db.execute("SELECT 1 FROM bars WHERE span=1 AND day=? LIMIT 1", (day,)).fetchone():
                        warm_minute_history(path, stop)
                        coverage = db.execute("SELECT count(*) FROM samples WHERE day=? AND minute=?",
                                              (day, CLOSE_MINUTE)).fetchone()[0]
                        if coverage == len(FUNDS) or minute_of(now) >= CLOSE_MINUTE + 30:
                            archived = day
                    else:
                        archived = day
            except (ValueError, sqlite3.Error) as error:
                LOG.warning("ETF reference data: %s", error)
            try:
                close_day = db.execute("SELECT max(day) FROM bars WHERE span=1 AND minute=? AND day<=?",
                                       (CLOSE_MINUTE, day)).fetchone()[0]
                if close_day and close_day != benchmarked and benchmark.refresh(db, close_day):
                    benchmarked = close_day
            except (requests.RequestException, KeyError, TypeError, ValueError, sqlite3.Error) as error:
                LOG.warning("ETF benchmark close: %s", error)
            delay = 900 if cutoff != refreshed else 180
            current = datetime.now(TZ)
            if archived != day and PM_SESSION[0] <= minute_of(current) <= CLOSE_MINUTE:
                until_close = (CLOSE_MINUTE + 1 - minute_of(current)) * 60 - current.second
                delay = min(delay, until_close)
            stop.wait(delay)


def tencent_history(client, fund, now, closing_samples):
    response = client.get(DAY_URL, params={"code": fund.symbol}, timeout=6)
    response.raise_for_status()
    payload = response.json()["data"][fund.symbol]["data"]
    today = now.date().isoformat()
    for session in payload:
        day = datetime.strptime(session["date"], "%Y%m%d").date().isoformat()
        if day > today:
            continue
        previous = None
        for text in session["data"]:
            parts = text.split()
            if len(parts) < 4 or not re.fullmatch(r"\d{4}", parts[0]):
                continue
            minute = 60 * int(parts[0][:2]) + int(parts[0][2:])
            price, amount = finite(parts[1]), finite(parts[3])
            if not price or price <= 0 or amount is None or amount < 0:
                continue
            if minute > CLOSE_MINUTE or (day == today and minute >= minute_of(now)):
                break
            if minute == CLOSE_MINUTE:
                closing_samples.append((day, minute, fund.code, minute * 60, round(amount), price))
                if previous and previous[0] == PM_SESSION[1] and round(amount) >= previous[1]:
                    yield (1, day, minute, fund.code, round(amount) - previous[1], price, "tencent_close_auction")
                break
            if session_start(minute) is not None and previous and previous[0] == minute - 1:
                increment = round(amount) - previous[1]
                if increment >= 0:
                    yield (1, day, minute, fund.code, increment, price, "tencent_minute")
            if minute <= PM_SESSION[1]:
                previous = (minute, round(amount))


def eastmoney_history(client, fund, today, limit=3000):
    response = client.get(HISTORY_URL, params={
        "secid": ("1." if fund.symbol.startswith("sh") else "0.") + fund.code,
        "klt": 5, "fqt": 0, "lmt": limit, "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }, headers={"Referer": "https://quote.eastmoney.com/"}, timeout=8)
    response.raise_for_status()
    data = response.json()["data"]
    if data["code"] != fund.code:
        raise ValueError(f"Unexpected history code for {fund.code}")
    for raw in data["klines"]:
        parts = raw.split(",")
        stamp = datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
        minute = minute_of(stamp)
        price, amount = finite(parts[2]), finite(parts[6])
        if (stamp.date() < today and minute % 5 == 0 and session_start(minute) is not None
                and minute != 575 and price and price > 0 and amount is not None and amount >= 0):
            yield (5, stamp.date().isoformat(), minute, fund.code, round(amount), price, "eastmoney_5m")


def warm_minute_history(path, stop=None):
    as_of = datetime.now(TZ)
    history = []
    closing_samples = []
    with requests.Session() as client:
        client.headers["User-Agent"] = "Mozilla/5.0"
        for fund in FUNDS.values():
            if stop is not None and stop.is_set():
                break
            try:
                history.extend(tencent_history(client, fund, as_of, closing_samples))
            except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as error:
                LOG.warning("%s Tencent minute history: %s", fund.code, error)
    try:
        with closing(connect(path)) as db, db:
            db.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)", history)
            db.executemany("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?)", closing_samples)
            fold_through = as_of.date() if minute_of(as_of) > CLOSE_MINUTE else as_of.date() - timedelta(days=1)
            archive_minutes(db, (fold_through - timedelta(days=8)).isoformat(), fold_through.isoformat())
        LOG.info("ETF minute backfill: %s bars covering %s products", len(history), len({row[3] for row in history}))
    except sqlite3.Error:
        LOG.exception("ETF minute backfill could not be saved")


def backfill(path, five_only=False):
    init_db(path)
    if not five_only:
        warm_minute_history(path)
    with closing(connect(path)) as db, requests.Session() as client:
        client.headers["User-Agent"] = "Mozilla/5.0"
        today = datetime.now(TZ).date()
        for fund in FUNDS.values():
            existing = db.execute(
                "SELECT count(DISTINCT day) FROM bars WHERE span=5 AND code=? AND day>=?",
                (fund.code, (today - timedelta(days=50)).isoformat()),
            ).fetchone()[0]
            if existing >= MIN_DAYS:
                continue
            try:
                rows = list(eastmoney_history(client, fund, today))
                if not rows:
                    raise ValueError("Empty five-minute history")
                with db:
                    db.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)", rows)
                print(f"{fund.code} 5m: {len(rows)} bars, {len({row[1] for row in rows})} dates", flush=True)
            except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as error:
                LOG.warning("%s Eastmoney history: %s", fund.code, error)
            time.sleep(1.35)


def rebuild_events(path, day):
    day = date.fromisoformat(day).isoformat()
    init_db(path)
    with closing(connect(path)) as db, db:
        minutes = [row[0] for row in db.execute(
            "SELECT DISTINCT minute FROM bars WHERE day=? ORDER BY minute", (day,))]
        if not minutes:
            return 0
        db.execute("DELETE FROM events WHERE day=?", (day,))
        for minute in minutes:
            if session_start(minute) is not None:
                metrics, history = score_windows(db, day, minute)
                record_events(db, day, minute, metrics, history)
        return db.execute("SELECT count(*) FROM events WHERE day=?", (day,)).fetchone()[0]


def direction_metrics(rows):
    amount = sum(row["amount"] for row in rows)
    net = sum(row["net_buy_estimate"] for row in rows)
    return net, net / amount if amount else 0


def pressure_score(ratio, buy_groups, sell_groups, multiple):
    if ratio == 0:
        return 50

    same, opposite = (buy_groups, sell_groups) if ratio > 0 else (sell_groups, buy_groups)
    net_strength = 30 * min(abs(ratio) / .20, 1)
    breadth_strength = 10 * max(0, same - opposite) / PRIMARY_GROUP_COUNT
    volume_strength = 10 * min(max(0, (multiple or 1) - 1), 1)
    distance = net_strength + breadth_strength + volume_strength

    if abs(ratio) < .05 or same < 3 or same <= opposite:
        distance = min(distance, 9)
    elif abs(ratio) < .12 or same < 4 or (multiple or 0) < 1.5:
        distance = min(distance, 29)
    return 50 + (1 if ratio > 0 else -1) * round(distance)


def daily_assessment(db, day, window, now, profiles=None):
    direction = {"rating": "unknown", "score": None, "coverage": 0,
                 "universe": len(PRIMARY_CODES), "reason": "no_direction"}
    rows = list(db.execute("""
        SELECT d.*, q.price AS quote_price FROM daily_direction d
        LEFT JOIN quotes q ON q.code=d.code AND q.stamp=d.stamp WHERE d.day=?
    """, (day,)))
    primary_rows = [row for row in rows if row["code"] in PRIMARY_CODES]
    if not primary_rows:
        return direction
    latest = max(datetime.fromisoformat(row["stamp"]) for row in primary_rows)
    direction["asOf"] = latest.isoformat(timespec="seconds")
    today = now.date().isoformat()
    market_open = any(start <= minute_of(now) < end for start, end in SESSIONS)
    if day == today and market_open and now - latest > timedelta(seconds=45):
        return {**direction, "reason": "stale"}
    end_of_day = day < today or (day == today and minute_of(now) >= CLOSE_MINUTE)
    if end_of_day and (day == today or minute_of(latest) >= CLOSE_MINUTE):
        closes = {row["code"]: row for row in db.execute(
            "SELECT code,amount,price FROM samples WHERE day=? AND minute=?", (day, CLOSE_MINUTE))}
        matched = []
        for row in rows:
            close = closes.get(row["code"])
            if (close and minute_of(datetime.fromisoformat(row["stamp"])) >= CLOSE_MINUTE
                    and abs(row["amount"] - close["amount"]) <= CLOSE_AMOUNT_TOLERANCE
                    and (row["quote_price"] is None or abs(row["quote_price"] - close["price"]) < .0001)):
                matched.append(row)
        if sum(row["code"] in PRIMARY_CODES and row["net_buy_estimate"] is not None
               for row in matched) < MIN_DIRECTION_FUNDS:
            return {**direction, "reason": "closing_pending"}
        direction.update(asOf=day + "T15:00:00+08:00", quoteBasis="close")
    else:
        matched = [row for row in rows if abs(latest - datetime.fromisoformat(row["stamp"])) <= timedelta(seconds=30)]
    main = [row for row in matched if row["code"] in PRIMARY_CODES]
    valid = [row for row in main if row["net_buy_estimate"] is not None and row["amount"] > 0]
    turnover = sum(row["amount"] for row in main)
    direction.update(coverage=len(valid), turnover=turnover, turnoverCoverage=len(main))
    valid_turnover = sum(row["amount"] for row in valid)
    if len(valid) < MIN_DIRECTION_FUNDS or not turnover or valid_turnover < .8 * turnover:
        return {**direction, "reason": "limited_coverage"}
    net, ratio = direction_metrics(valid)
    group_flows = defaultdict(list)
    for row in valid:
        group_flows[FUNDS[row["code"]].group].append(row)
    group_ratios = [direction_metrics(group)[1] for group in group_flows.values()]
    buy_groups = sum(value >= .03 for value in group_ratios)
    sell_groups = sum(value <= -.03 for value in group_ratios)

    if window is not None:
        if profiles is None or profiles.minute > minute_of(latest):
            profiles = TurnoverProfiles(db, day, min(window, minute_of(latest)), minimum=MIN_DAYS, limit=HISTORY_DAYS)
        main_codes = [row["code"] for row in valid]
        direction.update(profiles.measure(main_codes))
        control = [row for row in matched if row["code"] in CONTROL_CODES
                   and row["net_buy_estimate"] is not None and row["amount"] > 0]
        control = [row for row in control if profiles.measure([row["code"]]).get("multiple") is not None]
        direction["controlCoverage"] = len(control)
        if len(control) >= 3 and len({FUNDS[row["code"]].group for row in control}) >= 2:
            control_codes = [row["code"] for row in control]
            common = profiles.common(main_codes + control_codes)
            same_sessions = profiles.matching_sessions(main_codes + control_codes, common)
            comparable_main = profiles.measure(main_codes, common, same_sessions)
            comparable_control = profiles.measure(control_codes, common, same_sessions)
            control_multiple = comparable_control.get("multiple")
            if comparable_main.get("multiple") and control_multiple:
                relative = comparable_main["multiple"] / control_multiple
                control_ratio = direction_metrics(control)[1]
                direction.update(controlMultiple=control_multiple, relativeMultiple=round(relative, 2),
                                 controlNetRatio=round(control_ratio * 100, 1))

    score = pressure_score(ratio, buy_groups, sell_groups, direction.get("multiple"))
    rating = next(label for limit, label in SCORE_RATINGS if score <= limit)
    direction.pop("reason")
    return {**direction, "rating": rating, "score": score, "netBuyEstimate": net, "netRatio": round(ratio * 100, 1),
            "buyGroups": buy_groups, "sellGroups": sell_groups}


def empty_dashboard(day):
    return {"day": day, "days": [], "collector": None, "events": [],
            "funds": [fund_payload(fund) for fund in FUNDS.values()], "window": None,
            "today": datetime.now(TZ).date().isoformat(), "assessment": {"rating": "unknown", "score": None},
            "shareSeries": []}


def fund_payload(fund):
    item = asdict(fund)
    evidence = EVIDENCE.get(fund.code) or EVIDENCE.get(fund.group) or EVIDENCE.get(fund.role)
    if evidence:
        label, day, url = evidence
        item["evidence"] = {"holder": label, "day": day, "url": url}
        published = {"510300": 73_513_073_833, "588080": 21_606_708_460,
                     "560170": 861_278_520, "159335": 121_843_522}
        if fund.code in published:
            item["evidence"]["shares"] = published[fund.code]
    return item


def dashboard_data(path, day=None):
    if day:
        day = date.fromisoformat(day).isoformat()
    if not path.is_file():
        return empty_dashboard(day)
    with closing(connect(path)) as db:
        days = [row[0] for row in db.execute("""
            SELECT day FROM (
                SELECT DISTINCT day FROM bars
                UNION SELECT DISTINCT substr(stamp,1,10) AS day FROM quotes
                UNION SELECT DISTINCT day FROM daily_direction
            ) ORDER BY day DESC LIMIT 90
        """)]
        day = day or (days[0] if days else None)
        data = empty_dashboard(day)
        data["days"] = days
        row = db.execute("SELECT value FROM state WHERE key='collector'").fetchone()
        if row:
            data["collector"] = json.loads(row[0])
        if day is None:
            return data
        quotes = {row["code"]: dict(row) for row in db.execute(
            "SELECT * FROM quotes WHERE substr(stamp,1,10)=?", (day,))}
        bars = {row["code"]: dict(row) for row in db.execute("""
            SELECT code,price,minute FROM (
                SELECT code,price,minute,row_number() OVER(PARTITION BY code ORDER BY span,minute DESC) AS n
                FROM bars WHERE day=?
            ) WHERE n=1
        """, (day,))}
        placeholders = ",".join("?" for _ in PRIMARY_CODES)
        common_window = db.execute(f"""
            SELECT minute FROM bars WHERE span=1 AND day=? AND code IN ({placeholders})
            GROUP BY minute HAVING count(*)>=? ORDER BY minute DESC LIMIT 1
        """, (day, *PRIMARY_CODES, MIN_DIRECTION_FUNDS)).fetchone()
        window = common_window[0] if common_window else max((bar["minute"] for bar in bars.values()), default=None)
        continuous_window = min(window, PM_SESSION[1]) if window is not None else None
        metrics = score_windows(db, day, continuous_window)[0] if continuous_window is not None else {}
        profiles = TurnoverProfiles(db, day, continuous_window or 0, minimum=MIN_DAYS, limit=HISTORY_DAYS)
        disclosures, data["shareSeries"] = shares.disclosure_view(db, day, PRIMARY_CODES)
        recorded = {row["code"]: dict(row) for row in db.execute("SELECT * FROM daily_direction WHERE day=?", (day,))}
        for item in data["funds"]:
            code = item["code"]
            item["quote"] = quotes.get(code)
            item["bar"] = bars.get(code)
            item["metrics"] = metrics.get(code)
            item["dayTurnover"] = profiles.measure([code])
            item["recentTurnover"] = profiles.spark(code)
            item["direction"] = recorded.get(code)
            item["share"] = disclosures.get(code)
        data["window"] = clock(window) if window is not None else None
        data["assessment"] = daily_assessment(db, day, continuous_window, datetime.now(TZ), profiles)
        for event in db.execute("SELECT * FROM events WHERE day=? ORDER BY last DESC, amount DESC", (day,)):
            data["events"].append({
                "group": event["group_id"], "first": clock(event["first"]), "last": clock(event["last"]),
                "peak": clock(event["peak"]), "kind": event["kind"], "amount": event["amount"],
                "percentile": event["pct"], "codes": json.loads(event["codes"]), "basis": event["basis"],
            })
        return data


def evidence_data(path):
    rows = []
    if path.is_file():
        with closing(connect(path)) as db:
            rows = evidence.daily_history(db, datetime.now(TZ).date(), PRIMARY_CODES)
            benchmark.attach_closes(db, rows)
    return {"holdings": evidence.holdings(FUNDS), "shares": rows, "fundCount": len(PRIMARY_CODES),
            "benchmark": benchmark.REFERENCE}


def replay_data(path, day, code):
    day = date.fromisoformat(day).isoformat()
    fund = FUNDS.get(code)
    if not fund:
        raise ValueError("未知 ETF 代码")
    points = []
    close_auction = False
    span = 1
    if path.is_file():
        with closing(connect(path)) as db:
            span = db.execute("SELECT min(span) FROM bars WHERE day=? AND code=?", (day, code)).fetchone()[0] or 1
            bars = list(db.execute("SELECT minute,amount,price,source FROM bars WHERE span=? AND day=? AND code=? ORDER BY minute",
                                   (span, day, code)))
            points = [[clock(row["minute"]), row["amount"], row["price"]] for row in bars]
            close_auction = bool(bars and bars[-1]["source"] == "tencent_close_auction")
    return {"code": code, "day": day, "span": span, "points": points, "closeAuction": close_auction}


if __name__ == "__main__":
    parser = ArgumentParser(description="Backfill local ETF minute bars and five-minute baselines")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--five-only", action="store_true", help="Only import five-minute reference bars")
    parser.add_argument("--replay", metavar="YYYY-MM-DD", help="Recompute saved events from local minute bars")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.replay:
        print(f"{args.replay}: {rebuild_events(args.db, args.replay)} replay events")
    else:
        backfill(args.db, args.five_only)
