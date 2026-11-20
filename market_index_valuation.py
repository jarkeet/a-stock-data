"""Public-API index valuations with explicit source dates and estimate coverage."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from io import BytesIO
import json
import logging
import math
from threading import Lock
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from market_valuation import CSI_URL, ROOT, USER_AGENT, _atomic_json, _load_payload, _rolling_percentile, _weekly_series


INDICES = {
    "hs300": ("000300", "沪深300", "宽基"),
    "zz500": ("000905", "中证500", "宽基"),
    "zz800": ("000906", "中证800", "宽基"),
    "zz1000": ("000852", "中证1000", "宽基"),
    "sz50": ("000016", "上证50", "宽基"),
    "kc50": ("000688", "科创50", "宽基"),
    "zzhl": ("000922", "中证红利", "风格"),
    "zzyy": ("000933", "中证医药", "行业"),
    "hstech": ("800700", "恒生科技指数", "港股"),
}
DJ_URL = "https://danjuanfunds.com/djapi/index_eva/detail/"
EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HSTECH_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
HSTECH_SOURCE = "https://www.hsi.com.hk/eng/indexes/all-indexes/hstech"
HSTECH_HISTORY_URL = "https://etf.whatsid.me/api/valuation/history"
WEIGHT_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/closeweight/"
)
CACHE = ROOT / "market_index_valuations.json"
MARKET_TZ = ZoneInfo("Asia/Shanghai")
_LOCK = Lock()
_LOG = logging.getLogger(__name__)


def _get(url, *, user_agent=USER_AGENT, **kwargs):
    response = requests.get(url, headers={"User-Agent": user_agent}, timeout=12, **kwargs)
    response.raise_for_status()
    return response


def _number(value, *, percentile=False):
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(number):
        return None
    valid = 0 <= number <= 100 if percentile else number > 0
    return number if valid else None


def _date_rows(points):
    return [[pd.Timestamp(stamp, unit="ms").strftime("%Y-%m-%d"), value] for stamp, value in points]


def _official_pe(code, today):
    rows = _get(CSI_URL, params={
        "indexCode": code, "startDate": "20050101", "endDate": today.strftime("%Y%m%d"),
    }).json()["data"]
    frame = pd.DataFrame(
        ((row["tradeDate"], row["close"], row["peg"]) for row in rows),
        columns=["date", "close", "pe"],
    )
    frame["date"] = pd.to_datetime(frame["date"], format="%Y%m%d", errors="coerce")
    for field in ("close", "pe"):
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    frame = (
        frame.dropna().query("pe > 0 and close > 0")
        .sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    )
    if frame.empty:
        raise ValueError(f"No official PE data for {code}")
    last = frame.iloc[-1]
    cutoff = (last.date - pd.DateOffset(years=10)).strftime("%Y-%m-%d")
    return {
        "date": last.date.strftime("%Y-%m-%d"),
        "start": frame.date.iloc[0].strftime("%Y-%m-%d"),
        "pe": float(last.pe),
        "close": float(last.close),
        "pe_pct": [
            row for row in _date_rows(_rolling_percentile(frame)) if row[0] >= cutoff
        ],
        "values": _date_rows(_weekly_series(frame[frame.date.ge(cutoff)])),
        "closes": _date_rows(_weekly_series(frame[frame.date.ge(cutoff)], "close")),
    }


def _quote_date(timestamp):
    return datetime.fromtimestamp(int(timestamp) / 1000, MARKET_TZ).date().isoformat()


def _danjuan_percentile(value):
    percentile = _number(value, percentile=True)
    return round(100 * percentile, 6) if percentile is not None and percentile <= 1 else None


def _danjuan_snapshot(symbol):
    data = _get(DJ_URL + symbol, user_agent="Mozilla/5.0").json().get("data") or {}
    if data.get("index_code") != symbol:
        return {}
    return data


def _pb_snapshot(code):
    data = _danjuan_snapshot("SH" + code)
    if (value := _number(data.get("pb"))) is None:
        return None
    return {
        "date": _quote_date(data["ts"]), "value": value,
        "pct": _danjuan_percentile(data.get("pb_percentile")),
    }


def _hstech_valuation():
    data = _danjuan_snapshot("HKHSTECH")
    if not data:
        raise ValueError("No Hang Seng TECH valuation snapshot")
    pe, pb = _number(data.get("pe")), _number(data.get("pb"))
    return {
        "date": _quote_date(data["ts"]), "start": _quote_date(data["begin_at"]) if data.get("begin_at") else None,
        "pe": pe, "pb": pb,
        "pePct": _danjuan_percentile(data.get("pe_percentile")) if pe is not None else None,
        "pbPct": _danjuan_percentile(data.get("pb_percentile")) if pb is not None else None,
    }


def _hstech_history(previous, today):
    """Read the provider's published cap-weighted PE-TTM/PB series for HK indices."""
    headers = {"User-Agent": USER_AGENT}
    if previous.get("etag"):
        headers["If-None-Match"] = previous["etag"]
    response = requests.get(HSTECH_HISTORY_URL, headers=headers, timeout=12)
    if response.status_code == 304:
        return previous
    response.raise_for_status()
    payload = response.json()
    table = "display_history"
    if (payload.get("success") is not True or payload.get("encoding") != "columnar-v1"
            or payload.get("fields", {}).get(table) != ["pe", "pb"]):
        raise ValueError("Unexpected Hang Seng TECH valuation history format")
    calendar = payload["calendar"]
    series = payload[table]["HSTECH"]
    indices = series["d"]
    pe, pb = series["v"]
    if (not indices or len(indices) != len(pe) or len(indices) != len(pb)
            or any(type(i) is not int or not 0 <= i < len(calendar) for i in indices)):
        raise ValueError("Invalid Hang Seng TECH history columns")
    days = [date.fromisoformat(calendar[i]) for i in indices]
    last = days[-1]
    if (days != sorted(set(days)) or days[0] < date(2020, 7, 27)
            or last > min(today, date.fromisoformat(payload["dataAsOf"]))):
        raise ValueError("Invalid Hang Seng TECH history dates")
    rows = [[day.isoformat(), _number(p), _number(b)] for day, p, b in zip(days, pe, pb)]
    if any(sum(row[column] is not None for row in rows) < 53 for column in (1, 2)):
        raise ValueError("Insufficient Hang Seng TECH valuation history")
    if previous.get("rows") and rows[-1][0] < previous["rows"][-1][0]:
        return previous
    return {"etag": response.headers.get("ETag"), "rows": rows}


def _hstech_closes(now):
    today = now.date()
    params = {
        "secid": "124.HSTECH", "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53", "klt": 101, "fqt": 0,
        "beg": "20200727", "end": today.strftime("%Y%m%d"), "lmt": 2500,
    }
    data = _get(HSTECH_KLINE_URL, params=params).json()["data"]
    if not data or data.get("code") != "HSTECH" or not data.get("klines"):
        raise ValueError("No Hang Seng TECH matching price history")
    closes = {}
    for row in data["klines"]:
        date, _, close = row.split(",")
        date = datetime.strptime(date, "%Y-%m-%d").date()
        if (date < today or (date == today and now.hour >= 17)) and (value := _number(close)) is not None:
            closes[date.isoformat()] = value
    return closes


def _weights(code):
    columns = ["日期Date", "指数代码 Index Code", "成份券代码Constituent Code", "权重(%)weight"]
    response = _get(f"{WEIGHT_URL}{code}closeweight.xls")
    frame = pd.read_excel(BytesIO(response.content), engine="xlrd", dtype=str, usecols=columns)
    date, codes, stocks, weights = (frame[column] for column in columns)
    if date.empty or date.nunique() != 1 or not codes.str.zfill(6).eq(code).all():
        raise ValueError(f"Unexpected weight snapshot for {code}")
    stocks = stocks.str.zfill(6)
    weights = pd.to_numeric(weights, errors="coerce")
    if (not stocks.str.fullmatch(r"\d{6}", na=False).all() or stocks.duplicated().any()
            or not weights.gt(0).all() or not 99 <= weights.sum() <= 101):
        raise ValueError(f"Invalid constituent weights for {code}")
    return datetime.strptime(date.iloc[0], "%Y%m%d").date().isoformat(), dict(zip(stocks, weights))


def _stock_valuations(date):
    """Fetch one same-day valuation cross-section for all indices sharing this weight date."""
    params = {
        "reportName": "RPT_VALUEANALYSIS_DET",
        "columns": "SECURITY_CODE,TRADE_DATE,PB_MRQ,PS_TTM",
        "filter": f"(TRADE_DATE='{date}')", "pageSize": 4000,
        "sortColumns": "SECURITY_CODE", "sortTypes": "1",
    }
    values = {}
    page = 1
    while True:
        result = _get(EM_URL, params={**params, "pageNumber": page}).json().get("result")
        if not result or not result.get("data"):
            raise ValueError(f"No Eastmoney stock valuations for {date} page {page}")
        for row in result["data"]:
            code = row["SECURITY_CODE"]
            if row["TRADE_DATE"][:10] != date or code in values:
                raise ValueError(f"Invalid valuation date or duplicate stock {code}")
            values[code] = {"pb": _number(row.get("PB_MRQ")), "ps": _number(row.get("PS_TTM"))}
        if page >= result["pages"]:
            return values
        page += 1


def _estimate(weights, values, key):
    covered = [(weight, values.get(code, {}).get(key)) for code, weight in weights.items()]
    eligible = [(weight, value) for weight, value in covered if value is not None]
    matched = sum(weight for weight, _ in eligible)
    coverage = matched / sum(weights.values()) * 100
    if coverage < 95:
        return None
    return {
        "value": round(matched / sum(weight / value for weight, value in eligible), 4),
        "coverage": round(coverage, 1),
    }


def _result(job, label):
    try:
        return job.result()
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError, ImportError) as error:
        _LOG.warning("%s: %s", label, error)
        return None


def _refresh(cached, now, slot):
    records = cached.get("records", {})
    mainland = {slug: code for slug, (code, _, category) in INDICES.items() if category != "港股"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        hk_valuation_job = pool.submit(_hstech_valuation)
        hk_closes_job = pool.submit(_hstech_closes, now)
        pe_jobs = {slug: pool.submit(_official_pe, code, now.date()) for slug, code in mainland.items()}
        pb_jobs = {slug: pool.submit(_pb_snapshot, code) for slug, code in mainland.items()}
        weight_jobs = {slug: pool.submit(_weights, code) for slug, code in mainland.items()}
        hk = records.setdefault("hstech", {})
        if observation := _result(hk_valuation_job, "Hang Seng TECH valuation"):
            hk.setdefault("valuations", {})[observation["date"]] = observation
        if closes := _result(hk_closes_job, "Hang Seng TECH close history"):
            hk.setdefault("closes", {}).update(closes)
        for slug, job in pe_jobs.items():
            record = records.setdefault(slug, {})
            pe = _result(job, f"{slug} official PE")
            if pe and pe["date"] >= record.get("official", {}).get("date", ""):
                record["official"] = pe
            pb = _result(pb_jobs[slug], f"{slug} Xueqiu PB")
            if pb:
                record.setdefault("pb_api", {})[pb["date"]] = pb
        snapshots = {}
        for slug, job in weight_jobs.items():
            if snapshot := _result(job, f"{slug} official weights"):
                date, weights = snapshot
                if date not in records[slug].get("estimates", {}):
                    snapshots[slug] = (date, weights)
        stock_jobs = {date: pool.submit(_stock_valuations, date) for date in {s[0] for s in snapshots.values()}}
        stock_values = {date: _result(job, f"Eastmoney stock valuations {date}") for date, job in stock_jobs.items()}
    for slug, (date, weights) in snapshots.items():
        if values := stock_values.get(date):
            estimates = {key: _estimate(weights, values, key) for key in ("pb", "ps")}
            if any(estimates.values()):
                records[slug].setdefault("estimates", {})[date] = estimates
    return {"version": 2, "slot": slot, "records": records}


def load_index_valuations():
    """Refresh each source at most once per Shanghai day session and retain last good data."""
    now = datetime.now(MARKET_TZ)
    slot = f"{now:%Y-%m-%d}-{'evening' if now.hour >= 18 else 'day'}"
    with _LOCK:
        try:
            cached = _load_payload(CACHE)
        except (FileNotFoundError, ValueError):
            cached = {}
        if cached.get("version") != 2:
            cached = {"records": {
                slug: {"official": entry["official"]}
                for slug, entry in cached.get("records", {}).items() if "official" in entry
            }}
        changed = cached.get("slot") != slot or "hstech" not in cached.get("records", {})
        if changed:
            cached = _refresh(cached, now, slot)
        hk = cached["records"]["hstech"]
        if hk.get("historySlot") != slot:
            try:
                hk["history"] = _hstech_history(hk.get("history", {}), now.date())
            except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as error:
                _LOG.warning("Hang Seng TECH valuation history: %s", error)
            hk["historySlot"] = slot
            changed = True
        if changed:
            _atomic_json(cached, CACHE)
    return cached["records"]


def _estimated_series(record, key):
    observations = sorted((date, values[key]) for date, values in record.get("estimates", {}).items() if values[key])
    if not observations:
        return {}, []
    date, latest = observations[-1]
    series = pd.Series(
        [row["value"] for _, row in observations],
        index=pd.to_datetime([day for day, _ in observations]),
    )
    points = []
    for end, value in series.items():
        prior = series[(series.index < end) & (series.index >= end - pd.DateOffset(years=10))]
        if len(prior) >= 71:
            points.append([end.strftime("%Y-%m-%d"), round(float((prior < value).mean() * 100), 1)])
    pct = points[-1][1] if points and points[-1][0] == date else None
    return {**latest, "date": date, "pct": pct}, points


def _weekly_valuation_percentiles(frame, column):
    series = frame.set_index("date")[column]
    points = []
    for end, value in series.items():
        prior = series[(series.index < end) & (series.index >= end - pd.DateOffset(years=10))].dropna()
        if len(prior) >= 52:
            pct = round(float((prior < value).mean() * 100), 2) if pd.notna(value) else None
            points.append([end.date().isoformat(), pct])
    return points


def _hstech_entry(record, include_history):
    observations = [row for _, row in sorted(record.get("valuations", {}).items())]
    latest = observations[-1] if observations else {}
    date = latest.get("date")
    closes = record.get("closes", {})
    entry = {
        "close": closes.get(date),
        "pe": latest.get("pe"), "pePct": latest.get("pePct"), "peDate": date if latest.get("pe") is not None else None,
        "pb": latest.get("pb"), "pbPct": latest.get("pbPct"), "pbDate": date if latest.get("pb") is not None else None,
        "pbSource": "danjuan" if latest.get("pb") is not None else None,
        "ps": None, "psPct": None, "psDate": None,
    }
    if include_history:
        price_rows = [[day, value] for day, value in closes.items() if date is None or day <= date]
        close_frame = pd.DataFrame(price_rows, columns=["date", "close"]).sort_values("date")
        close_frame["date"] = pd.to_datetime(close_frame["date"])
        entry.update({
            "start": latest.get("start"), "csiSource": HSTECH_SOURCE,
            "peHistory": [[row["date"], row["pePct"]] for row in observations if row["pePct"] is not None],
            "peValues": [[row["date"], row["pe"]] for row in observations if row["pe"] is not None],
            "pbHistory": [[row["date"], row["pbPct"]] for row in observations if row["pbPct"] is not None],
            "psHistory": [], "closeHistory": _date_rows(_weekly_series(close_frame, "close")),
            "valuationHistorySource": "danjuan" if observations else None,
        })
        if rows := record.get("history", {}).get("rows"):
            frame = pd.DataFrame(rows, columns=["date", "pe", "pb"])
            frame["date"] = pd.to_datetime(frame["date"], format="%Y-%m-%d")
            entry.update({
                "valuationHistorySource": "investmentWorkbench",
                "valuationHistoryStart": rows[0][0],
                "peHistory": _weekly_valuation_percentiles(frame, "pe"),
                "pbHistory": _weekly_valuation_percentiles(frame, "pb"),
                "peValues": [[day, pe] for day, pe, _ in rows],
            })
    return entry


def index_valuation_data(slug=None):
    raw = load_index_valuations()
    entries = []
    for key, (code, name, category) in INDICES.items():
        if slug is not None and key != slug:
            continue
        record = raw.get(key, {})
        index = {"slug": key, "code": code, "name": name, "category": category}
        if key == "hstech":
            entries.append({**index, **_hstech_entry(record, slug is not None)})
            continue
        pe = record.get("official", {})
        date = pe.get("date")
        pe_pct = pe.get("pe_pct", [])
        pb_history = record.get("pb_api", {})
        pb = pb_history[max(pb_history)] if pb_history else {}
        if pb:
            pb_points = [[day, obs["pct"]] for day, obs in sorted(pb_history.items()) if obs["pct"] is not None]
        else:
            pb, pb_points = _estimated_series(record, "pb")
        ps, ps_points = _estimated_series(record, "ps")
        entry = {
            **index,
            "peDate": date, "close": pe.get("close"),
            "pe": pe.get("pe"), "pePct": pe_pct[-1][1] if pe_pct and pe_pct[-1][0] == date else None,
            "pb": pb.get("value"), "pbPct": pb.get("pct"), "pbDate": pb.get("date"),
            "pbSource": "danjuan" if pb_history else ("estimate" if pb else None),
            "pbCoverage": pb.get("coverage"),
            "ps": ps.get("value"), "psPct": ps.get("pct"), "psDate": ps.get("date"),
            "psCoverage": ps.get("coverage"),
        }
        if slug is not None:
            entry.update({
                "start": pe.get("start"), "peHistory": pe_pct, "peValues": pe.get("values", []),
                "closeHistory": pe.get("closes", []), "pbHistory": pb_points, "psHistory": ps_points,
                "csiSource": f"https://www.csindex.com.cn/#/indices/family/detail?indexCode={code}",
            })
        entries.append(entry)
    return {"indices": entries}


if __name__ == "__main__":
    print(json.dumps(index_valuation_data(), ensure_ascii=False, indent=2))
