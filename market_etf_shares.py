"""Collect dated ETF shares from exchange disclosures, in individual shares."""
from datetime import date
from decimal import Decimal, InvalidOperation
import logging

import requests

SSE_PAGE = "https://www.sse.com.cn/market/funddata/volumn/etfvolumn/"
SZSE_PAGE = "https://www.szse.cn/market/fund/volume/etf/index.html"
SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_URL = "https://www.szse.cn/api/report/ShowReport/data"
LOG = logging.getLogger(__name__)


def init_schema(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS etf_share_disclosures(
            day TEXT, code TEXT, shares INTEGER, source TEXT, PRIMARY KEY(day, code));
    """)


def individual_shares(value):
    try:
        shares = Decimal(str(value).replace(",", "")) * 10000
    except InvalidOperation:
        return None
    return round(shares) if shares.is_finite() and shares > 0 else None


def fetch_sse(client, day, codes):
    response = client.get(SSE_URL, params={
        "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L", "isPagination": "false", "STAT_DATE": day,
    }, headers={"Referer": SSE_PAGE}, timeout=7)
    response.raise_for_status()
    for row in response.json()["result"]:
        code = row["SEC_CODE"]
        if row["STAT_DATE"] == day and code in codes and (shares := individual_shares(row["TOT_VOL"])):
            yield day, code, shares, "SSE"


def fetch_szse(client, code, first, last):
    response = client.get(SZSE_URL, params={
        "SHOWTYPE": "JSON", "CATALOGID": "scsj_fund_jjgm", "jjlb": "ETF", "txtDm": code,
        "txtStart": first, "txtEnd": last, "PAGENO": 1,
    }, headers={"Referer": SZSE_PAGE}, timeout=7)
    response.raise_for_status()
    payload = response.json()[0]
    if payload["metadata"]["pagecount"] > 1:
        raise ValueError("ETF share query returned multiple pages")
    for row in payload["data"]:
        day = date.fromisoformat(row["size_date"]).isoformat()
        if first <= day <= last and row["fund_code"] == code and (shares := individual_shares(row["current_size"])):
            yield day, code, shares, "SZSE"


def refresh(db, codes, today, stop):
    # Use completed prior dates, because Shenzhen can revise the evening publication overnight.
    days = [r[0] for r in db.execute(
        "SELECT DISTINCT day FROM bars WHERE day<? ORDER BY day DESC LIMIT 10", (today,))]
    if not days:
        return False
    sh_codes = {code for code in codes if not code.startswith("15")}
    sz_codes = set(codes) - sh_codes

    def store(request, args):
        try:
            results = list(request(client, *args))
        except (requests.RequestException, ValueError, TypeError, KeyError, IndexError) as error:
            LOG.warning("ETF disclosures %s: %s", args[0], error)
            return
        with db:
            db.executemany("INSERT OR REPLACE INTO etf_share_disclosures VALUES (?,?,?,?)", results)

    with requests.Session() as client:
        client.headers["User-Agent"] = "Mozilla/5.0"
        for day in days:
            if stop.is_set():
                return False
            covered = db.execute("SELECT count(*) FROM etf_share_disclosures WHERE day=? AND source='SSE'",
                                 (day,)).fetchone()[0]
            if covered < len(sh_codes):
                store(fetch_sse, (day, sh_codes))
                stop.wait(1.1)
        for code in sorted(sz_codes):
            if stop.is_set():
                return False
            if not db.execute("SELECT 1 FROM etf_share_disclosures WHERE day=? AND code=?",
                              (days[0], code)).fetchone():
                store(fetch_szse, (code, days[-1], days[0]))
                stop.wait(1.1)
    covered = db.execute("SELECT count(*) FROM etf_share_disclosures WHERE day=?", (days[0],)).fetchone()[0]
    return covered == len(codes)


def disclosure_view(db, day, primary):
    trading_days = [row[0] for row in db.execute("""
        SELECT day FROM (
            SELECT DISTINCT day FROM bars WHERE day<=?
            UNION SELECT DISTINCT day FROM etf_share_disclosures WHERE day<=?
        ) ORDER BY day DESC LIMIT 16
    """, (day, day))]
    if not trading_days:
        return {}, []
    by_day = {}
    for row in db.execute("SELECT * FROM etf_share_disclosures WHERE day BETWEEN ? AND ? ORDER BY day DESC",
                          (trading_days[-1], day)):
        by_day.setdefault(row["day"], {})[row["code"]] = row
    latest = {}
    series = []
    for current, previous in zip(trading_days, trading_days[1:] + [None]):
        entries = by_day.get(current, {})
        if not entries:
            continue
        changes = {}
        for code, row in entries.items():
            prior = by_day.get(previous, {}).get(code)
            change = row["shares"] - prior["shares"] if prior else None
            unusual = change is not None and abs(change) >= prior["shares"] * .35
            latest.setdefault(code, {
                "day": current, "shares": row["shares"], "source": row["source"],
                "change": change, "previousDay": previous if prior else None, "unusual": unusual,
            })
            if change is not None and not unusual:
                changes[code] = change
        if previous:
            coverage = sum(code in changes for code in primary)
            net = sum(changes[code] for code in primary) if coverage == len(primary) else None
            series.append({"day": current, "net": net, "coverage": coverage})
    return latest, list(reversed(series[-12:]))
