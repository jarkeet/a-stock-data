"""Public-API index valuations with explicit source dates and estimate coverage."""
from bisect import bisect_left
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
    "hsi": ("800000", "恒生指数", "港股"),
    "hstech": ("800700", "恒生科技指数", "港股"),
    "h30533": ("H30533", "中概互联", "港股"),
    "931787": ("931787", "港股创新药", "港股"),
}
DJ_URL = "https://danjuanfunds.com/djapi/index_eva/detail/"
EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HSTECH_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
BALANCED_URL = "https://xushilu.com/api/valuation/{symbol}/history"
TENCENT_HK_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"
CSI_DETAIL_URL = "https://www.csindex.com.cn/#/indices/family/detail?indexCode="
HK_INDICES = {
    "hsi": {"symbol": "HSI", "snapshot": "HKHSI", "start": "1969-11-24", "source":
            "https://www.hsi.com.hk/en-hk/indexes/all-indexes/index-details/?seriesCode=hsi"},
    "hstech": {"symbol": "HSTECH", "snapshot": "HKHSTECH", "start": "2020-07-27", "source":
               "https://www.hsi.com.hk/eng/indexes/all-indexes/hstech"},
    "h30533": {"symbol": "H30533", "snapshot": "CSIH30533", "start": "2014-12-29", "source":
               CSI_DETAIL_URL + "H30533", "etf": "513050", "benchmark": "中证海外中国互联网50指数（人民币）"},
    "931787": {"symbol": "931787", "start": "2018-08-14", "source": CSI_DETAIL_URL + "931787",
               "etf": "513120", "benchmark": "中证香港创新药指数（港元）"},
}
BALANCED_METRICS = {"hsi": ("pe", "pb"), "hstech": ("pe", "pb"), "h30533": ("pb",)}
WEIGHT_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/closeweight/"
)
CACHE = ROOT / "market_index_valuations.json"
CACHE_VERSION = 4
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


def _hk_valuation_snapshot(symbol):
    data = _danjuan_snapshot(symbol)
    if not data:
        raise ValueError(f"No valuation snapshot for {symbol}")
    pe, pb = _number(data.get("pe")), _number(data.get("pb"))
    return {
        "date": _quote_date(data["ts"]), "start": _quote_date(data["begin_at"]) if data.get("begin_at") else None,
        "pe": pe, "pb": pb,
        "pePct": _danjuan_percentile(data.get("pe_percentile")) if pe is not None else None,
        "pbPct": _danjuan_percentile(data.get("pb_percentile")) if pb is not None else None,
    }


def _balanced_valuation_history(spec, metric, today):
    """Read the public valuation series in its original Ping An Index methodology."""
    field = "pe_ttm" if metric == "pe" else "pb"
    payload = _get(BALANCED_URL.format(symbol=spec["symbol"]), params={"metric": field, "window": "all"}).json()
    meta = payload["meta"]
    if meta.get("metric") != field:
        raise ValueError(f"Unexpected metric for {spec['symbol']}")
    start = date.fromisoformat(spec["start"])
    end = min(today, date.fromisoformat(meta["as_of"]))
    rows = {}
    for row in payload["data"]["series"]:
        if row.get("calc_src") != "pingan_index":
            raise ValueError(f"Unexpected valuation methodology for {spec['symbol']}")
        day = date.fromisoformat(row["trade_date"])
        if start <= day <= end and day.weekday() < 5 and (value := _number(row["value"])) is not None:
            rows[day.isoformat()] = value
    if not rows:
        raise ValueError(f"No valid {field} history for {spec['symbol']}")
    return rows


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


def _hsi_closes(now):
    closes = {}
    for interval, count in (("week", 640), ("day", 10)):
        payload = _get(TENCENT_HK_KLINE_URL, params={"param": f"hkHSI,{interval},,,{count},qfq"}).json()
        for day, _, close, *_ in payload["data"]["hkHSI"][interval]:
            trade_date = date.fromisoformat(day)
            if (trade_date < now.date() or (trade_date == now.date() and now.hour >= 17)):
                if (value := _number(close)) is not None:
                    closes[day] = value
    if not closes:
        raise ValueError("No Hang Seng Index close history")
    return closes


def _csi_hk_history(code, start, now):
    today = now.date().isoformat()
    rows = _get(CSI_URL, params={
        "indexCode": code, "startDate": start.replace("-", ""), "endDate": today.replace("-", ""),
    }).json()["data"]
    if not rows or any(row["indexCode"] != code for row in rows):
        raise ValueError(f"Unexpected CSI history for {code}")
    closes, pe = {}, {}
    for row in rows:
        day = datetime.strptime(row["tradeDate"], "%Y%m%d").date().isoformat()
        if day < start or day > today or (day == today and now.hour < 17):
            continue
        if _number(row.get("tradingVol")) is None or (close := _number(row["close"])) is None:
            continue
        closes[day] = close
        pe[day] = _number(row.get("peg"))
    return {"closes": closes, "officialPe": pe}


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


def _stock_pb(date):
    """Fetch same-day stock PB for indices sharing this weight date."""
    params = {
        "reportName": "RPT_VALUEANALYSIS_DET",
        "columns": "SECURITY_CODE,TRADE_DATE,PB_MRQ",
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
            values[code] = _number(row.get("PB_MRQ"))
        if page >= result["pages"]:
            return values
        page += 1


def _estimate_pb(weights, values):
    covered = [(weight, values.get(code)) for code, weight in weights.items()]
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


def _refresh_hk(cached, now, slot):
    records = cached["records"]
    for slug in HK_INDICES:
        records.setdefault(slug, {})
    with ThreadPoolExecutor(max_workers=5) as pool:
        valuation_jobs = {slug: pool.submit(_hk_valuation_snapshot, spec["snapshot"])
                          for slug, spec in HK_INDICES.items() if "snapshot" in spec}
        price_jobs = {"hsi": pool.submit(_hsi_closes, now), "hstech": pool.submit(_hstech_closes, now)}
        csi_jobs = {slug: pool.submit(_csi_hk_history, spec["symbol"], spec["start"], now)
                    for slug, spec in HK_INDICES.items() if "etf" in spec}
        history_jobs = {(slug, metric): pool.submit(_balanced_valuation_history, HK_INDICES[slug], metric, now.date())
                        for slug, metrics in BALANCED_METRICS.items() for metric in metrics}
        for slug, job in valuation_jobs.items():
            if observation := _result(job, f"{slug} valuation snapshot"):
                records[slug].setdefault("valuations", {})[observation["date"]] = observation
        for slug, job in price_jobs.items():
            if closes := _result(job, f"{slug} close history"):
                records[slug].setdefault("closes", {}).update(closes)
        for slug, job in csi_jobs.items():
            if data := _result(job, f"{slug} CSI history"):
                for key, rows in data.items():
                    records[slug].setdefault(key, {}).update(rows)
        for (slug, metric), job in history_jobs.items():
            if rows := _result(job, f"{slug} {metric} history"):
                records[slug].setdefault("balancedRows", {}).setdefault(metric, {}).update(rows)
    cached["hkSlot"] = slot


def _refresh(cached, now, slot):
    records = cached.get("records", {})
    mainland = {slug: code for slug, (code, _, category) in INDICES.items() if category != "港股"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        pe_jobs = {slug: pool.submit(_official_pe, code, now.date()) for slug, code in mainland.items()}
        pb_jobs = {slug: pool.submit(_pb_snapshot, code) for slug, code in mainland.items()}
        weight_jobs = {slug: pool.submit(_weights, code) for slug, code in mainland.items()}
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
        stock_jobs = {date: pool.submit(_stock_pb, date) for date in {s[0] for s in snapshots.values()}}
        stock_values = {date: _result(job, f"Eastmoney stock PB {date}") for date, job in stock_jobs.items()}
    for slug, (date, weights) in snapshots.items():
        if values := stock_values.get(date):
            if pb := _estimate_pb(weights, values):
                records[slug].setdefault("estimates", {})[date] = {"pb": pb}
    return {**cached, "version": CACHE_VERSION, "slot": slot, "records": records}


def load_index_valuations():
    """Refresh each source at most once per Shanghai day session and retain last good data."""
    now = datetime.now(MARKET_TZ)
    slot = f"{now:%Y-%m-%d}-{'evening' if now.hour >= 18 else 'day'}"
    with _LOCK:
        try:
            cached = _load_payload(CACHE)
        except (FileNotFoundError, ValueError):
            cached = {}
        cache_version = cached.get("version")
        changed = cache_version != CACHE_VERSION
        if cache_version == 2:
            cached.pop("hkSlot", None)
            cached.pop("hkHistoryEtag", None)
            for record in cached.get("records", {}).values():
                record.pop("history", None)
                record.pop("historySlot", None)
        elif changed and cache_version != 3:
            cached = {"records": {
                slug: {"official": entry["official"]}
                for slug, entry in cached.get("records", {}).items() if "official" in entry
            }}
        if cache_version in (2, 3):
            for record in cached.get("records", {}).values():
                record.pop("psRows", None)
                if "estimates" in record:
                    record["estimates"] = {day: {"pb": values["pb"]}
                                           for day, values in record["estimates"].items() if values.get("pb")}
        cached["version"] = CACHE_VERSION
        if cached.get("slot") != slot:
            cached = _refresh(cached, now, slot)
            changed = True
        if cached.get("hkSlot") != slot or not HK_INDICES.keys() <= cached["records"].keys():
            _refresh_hk(cached, now, slot)
            changed = True
        if changed:
            _atomic_json(cached, CACHE)
    return cached["records"]


def _estimated_pb_series(record):
    observations = sorted((date, values["pb"]) for date, values in record.get("estimates", {}).items() if values.get("pb"))
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


def _weekly_points(values):
    weeks = {}
    for day, value in sorted(values.items()):
        weeks[date.fromisoformat(day).isocalendar()[:2]] = [day, value]
    return list(weeks.values())


def _weekly_valuation_percentiles(values):
    dates = [pd.Timestamp(day) for day, _ in values]
    ratios = [value for _, value in values]
    points = []
    minimum = 52
    window = pd.DateOffset(years=10)
    for i in range(minimum, len(values)):
        start = bisect_left(dates, dates[i] - window)
        prior = [ratio for ratio in ratios[start:i] if ratio is not None]
        day, value = values[i]
        if len(prior) >= minimum:
            pct = round(100 * sum(ratio < value for ratio in prior) / len(prior), 2) if value is not None else None
            points.append([day, pct])
    return points


def _hk_entry(slug, record, include_history):
    spec = HK_INDICES[slug]
    observations = [row for _, row in sorted(record.get("valuations", {}).items())]
    closes = record.get("closes", {})
    entry = {
        "indexSymbol": spec["symbol"], "etfCode": spec.get("etf"), "benchmark": spec.get("benchmark"),
    }
    series = {metric: (rows, "balanced") for metric, rows in record.get("balancedRows", {}).items()}
    official_pe = record.get("officialPe", {})
    if any(value is not None for value in official_pe.values()):
        series["pe"] = (official_pe, "csi")
    close_latest = max(closes, default=None)
    close_start = min(closes, default=None)
    for metric in ("pe", "pb"):
        rows, source = series.get(metric, ({}, None))
        if rows and source == "balanced" and close_latest and slug in ("h30533", "hstech"):
            rows = {day: value for day, value in rows.items()
                    if day in closes or day < close_start or day > close_latest}
        if rows:
            values = _weekly_points(rows)
            history = _weekly_valuation_percentiles(values)
        else:
            values = [[row["date"], row[metric]] for row in observations if row.get(metric) is not None]
            history = [[row["date"], row[metric + "Pct"]] for row in observations if row.get(metric + "Pct") is not None]
            source = "danjuan" if values else None
        day, value = next(((day, value) for day, value in reversed(values) if value is not None), (None, None))
        entry.update({metric: value, metric + "Date": day, metric + "Pct": dict(history).get(day),
                      metric + "Source": source})
        if include_history:
            entry.update({metric + "Values": values, metric + "History": history, metric + "HistorySource": source})
    pe_date = entry["peDate"]
    close_date = pe_date if pe_date in closes else close_latest
    entry.update(close=closes.get(close_date), closeDate=close_date)
    if include_history:
        entry.update({
            "start": entry["peValues"][0][0] if entry["peValues"] else None,
            "csiSource": spec["source"], "snapshotSymbol": spec.get("snapshot"),
            "closeHistory": [[day, round(value, 2)] for day, value in _weekly_points(closes)],
            "valuationHistorySource": entry["peSource"],
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
        if key in HK_INDICES:
            entries.append({**index, **_hk_entry(key, record, slug is not None)})
            continue
        pe = record.get("official", {})
        date = pe.get("date")
        pe_pct = pe.get("pe_pct", [])
        pb_history = record.get("pb_api", {})
        pb = pb_history[max(pb_history)] if pb_history else {}
        if pb:
            pb_points = [[day, obs["pct"]] for day, obs in sorted(pb_history.items()) if obs["pct"] is not None]
        else:
            pb, pb_points = _estimated_pb_series(record)
        entry = {
            **index,
            "peDate": date, "close": pe.get("close"),
            "pe": pe.get("pe"), "pePct": pe_pct[-1][1] if pe_pct and pe_pct[-1][0] == date else None,
            "pb": pb.get("value"), "pbPct": pb.get("pct"), "pbDate": pb.get("date"),
            "pbSource": "danjuan" if pb_history else ("estimate" if pb else None),
            "pbCoverage": pb.get("coverage"),
        }
        if slug is not None:
            entry.update({
                "start": pe.get("start"), "peHistory": pe_pct, "peValues": pe.get("values", []),
                "closeHistory": pe.get("closes", []), "pbHistory": pb_points,
                "csiSource": f"https://www.csindex.com.cn/#/indices/family/detail?indexCode={code}",
            })
        entries.append(entry)
    return {"indices": entries}


if __name__ == "__main__":
    print(json.dumps(index_valuation_data(), ensure_ascii=False, indent=2))
