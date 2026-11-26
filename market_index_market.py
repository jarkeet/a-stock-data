"""On-demand index quotes and cumulative main fund flow for the valuation list."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import math
import re
from threading import Lock, Thread
from time import monotonic, sleep

import requests

from market_index_valuation import CNI_CODES, GOLD_ETF_CODE, HK_INDICES, INDICES, MARKET_TZ
from market_valuation import CSI_URL


TENCENT_URL = "https://qt.gtimg.cn/q="
EASTMONEY_URL = "https://push2.eastmoney.com/api/qt/stock/"
EASTMONEY_HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/"
NATIVE_HK = {"hsi": ("r_hkHSI", "100.HSI"), "hstech": ("r_hkHSTECH", "124.HSTECH")}
HKD_INDICES = {*NATIVE_HK, "931637", "931787"}
QUOTE_CACHE = {}
FLOW_CACHE = {}
_REFRESHING = set()
_NEXT_REFRESH = {"quotes": 0, "flows": 0}
_STATE_LOCK = Lock()
_EM_RATE_LOCK = Lock()
_em_next_request = 0.0


def _number(value):
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def _nonnegative(value):
    result = _number(value)
    return result if result is not None and result >= 0 else None


def _eastmoney_id(slug, code):
    if slug in NATIVE_HK:
        return NATIVE_HK[slug][1]
    market = "0" if code.startswith("399") else "2" if code.startswith(("9", "H")) else "1"
    return f"{market}.{code}"


def _eastmoney(path, params, *, base=EASTMONEY_URL):
    global _em_next_request
    with _EM_RATE_LOCK:
        sleep(max(0, _em_next_request - monotonic()))
        _em_next_request = monotonic() + 0.6
    response = requests.get(
        base + path, params=params, timeout=6,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
    )
    response.raise_for_status()
    return response.json().get("data") or {}


def _quote(price, change, amount, turnover, when, source, slug):
    if price is None or price <= 0:
        return None
    day = when.date().isoformat() if isinstance(when, datetime) else when.isoformat()
    return {
        "latestPrice": price, "changePct": change, "amount": amount, "turnoverRate": turnover,
        "quoteDate": day, "quoteTime": when.strftime("%H:%M:%S") if isinstance(when, datetime) else None,
        "quoteSource": source, "currency": "HKD" if slug in HKD_INDICES else "CNY",
    }


def _tencent_quotes(now):
    symbols = {}
    for slug, (code, _, _) in INDICES.items():
        if slug in NATIVE_HK:
            identifier = NATIVE_HK[slug][0]
            symbols[identifier] = (slug, identifier.removeprefix("r_hk"))
        elif slug not in HK_INDICES and (code in CNI_CODES or code == GOLD_ETF_CODE or code.startswith(("000", "399"))):
            market = "sz" if code in CNI_CODES or code.startswith("399") else "sh"
            identifier = market + code
            symbols[identifier] = (slug, code)
    response = requests.get(TENCENT_URL + ",".join(symbols), timeout=6)
    response.raise_for_status()
    for identifier, raw in re.findall(r'v_(\w+)="([^"]+)"', response.content.decode("gbk", errors="replace")):
        fields = raw.split("~")
        if identifier not in symbols or len(fields) <= 38:
            continue
        slug, expected_code = symbols[identifier]
        if fields[2] != expected_code:
            continue
        digits = re.sub(r"\D", "", fields[30])
        try:
            when = datetime.strptime(digits, "%Y%m%d%H%M%S").replace(tzinfo=MARKET_TZ)
        except ValueError:
            continue
        if when > now + timedelta(minutes=2):
            continue
        is_hk = slug in NATIVE_HK
        amount = None
        if not is_hk:
            amount_fields = fields[35].split("/")
            amount = _nonnegative(amount_fields[-1]) if len(amount_fields) == 3 else None
            if amount is None and (amount_wan := _nonnegative(fields[37])) is not None:
                amount = amount_wan * 10_000
        return_quote = _quote(
            _number(fields[3]), _number(fields[32]), amount,
            None if is_hk else _nonnegative(fields[38]), when, "tencent", slug,
        )
        if return_quote:
            yield slug, return_quote


def _eastmoney_quote(slug, code, now):
    secid = _eastmoney_id(slug, code)
    params = {"secid": secid, "fields": "f43,f48,f57,f59,f86,f168,f170", "invt": 2}
    try:
        data = _eastmoney("get", {**params, "fltt": 2})
        price_scale = rate_scale = 1
    except _UPSTREAM_ERRORS:
        data = _eastmoney("get", {
            **params, "fields": "f43,f47,f48,f57,f58,f59,f60,f62,f86,f168,f169,f170",
        })
        digits = _number(data.get("f59"))
        if digits is None or digits not in (0, 1, 2, 3, 4):
            return None
        price_scale, rate_scale = 10 ** int(digits), 100
    if data.get("f57") != secid.split(".")[1] or data.get("f86") in (None, 0):
        return None
    when = datetime.fromtimestamp(int(data["f86"]), MARKET_TZ)
    if when > now + timedelta(minutes=2):
        return None
    price, change, turnover = (_number(data.get(key)) for key in ("f43", "f170", "f168"))
    return _quote(price / price_scale if price is not None else None,
                  change / rate_scale if change is not None else None,
                  _nonnegative(data.get("f48")),
                  turnover / rate_scale if turnover is not None and turnover >= 0 else None,
                  when, "eastmoney", slug)


def _csi_close_quote(slug, code, now):
    today = now.date()
    response = requests.get(CSI_URL, params={
        "indexCode": code, "startDate": (today - timedelta(days=12)).strftime("%Y%m%d"),
        "endDate": today.strftime("%Y%m%d"),
    }, timeout=6)
    response.raise_for_status()
    limit = today if now.hour >= 17 else today - timedelta(days=1)
    rows = [row for row in response.json().get("data") or []
            if row.get("indexCode") == code and row["tradeDate"] <= limit.strftime("%Y%m%d")
            and (_number(row.get("tradingVol")) or 0) > 0 and (_number(row.get("close")) or 0) > 0]
    if not rows:
        return None
    row = max(rows, key=lambda item: item["tradeDate"])
    amount_yi = _nonnegative(row.get("tradingValue"))
    return _quote(_number(row["close"]), _number(row.get("changePct")),
                  amount_yi * 100_000_000 if amount_yi is not None else None, None,
                  datetime.strptime(row["tradeDate"], "%Y%m%d").date(), "csi-close", slug)


def _main_flow(slug, code, now):
    secid = _eastmoney_id(slug, code)
    for base, count, fields in (
        (EASTMONEY_URL, 1, "f51,f52"),
        (EASTMONEY_HISTORY_URL, 2, "f51,f52,f53,f54,f55,f56"),
    ):
        try:
            data = _eastmoney("fflow/kline/get", {
                "secid": secid, "klt": 101, "lmt": count, "fields1": "f1,f2,f3,f7",
                "fields2": fields, "invt": 2,
            }, base=base)
            if data.get("code") != secid.split(".")[1] or not data.get("klines"):
                continue
            day, net, *other = data["klines"][-1].split(",")
            trade_day = date.fromisoformat(day)
            main = _number(net)
            if trade_day > now.date() or main is None:
                continue
            if main == 0 and not any(_number(value) for value in other):
                continue
            return {"mainFlow": main, "flowDate": day}
        except _UPSTREAM_ERRORS:
            continue
    return None


_UPSTREAM_ERRORS = (requests.RequestException, ValueError, TypeError, KeyError, OverflowError, OSError)


def _update_cache(cache, slug, value, date_key):
    with _STATE_LOCK:
        existing = cache.get(slug, {})
        if (value[date_key], value.get("quoteTime") or "") >= (
            existing.get(date_key, ""), existing.get("quoteTime") or ""
        ):
            if date_key == "quoteDate" and value[date_key] == existing.get(date_key):
                for field in ("amount", "turnoverRate"):
                    if value.get(field) is None and existing.get(field) is not None:
                        source_key = field + "Source"
                        value = {
                            **value, field: existing[field],
                            source_key: existing.get(source_key, existing["quoteSource"]),
                        }
            cache[slug] = value


def _refresh_quotes(now):
    quotes = {}
    try:
        for slug, quote in _tencent_quotes(now):
            quotes[slug] = quote
            _update_cache(QUOTE_CACHE, slug, quote, "quoteDate")
    except _UPSTREAM_ERRORS:
        pass

    def supplement(slug, code):
        try:
            quote = _eastmoney_quote(slug, code, datetime.now(MARKET_TZ))
        except _UPSTREAM_ERRORS:
            quote = None
        if slug in quotes:
            if quote and quote["quoteDate"] == quotes[slug]["quoteDate"]:
                same_day = {**quotes[slug]}
                for field in ("amount", "turnoverRate"):
                    if same_day[field] is None and quote[field] is not None:
                        same_day[field] = quote[field]
                        same_day[field + "Source"] = "eastmoney"
                _update_cache(QUOTE_CACHE, slug, same_day, "quoteDate")
            return
        if quote is None and slug not in NATIVE_HK and code not in CNI_CODES and code != GOLD_ETF_CODE:
            try:
                quote = _csi_close_quote(slug, code, now)
            except _UPSTREAM_ERRORS:
                pass
        if quote:
            _update_cache(QUOTE_CACHE, slug, quote, "quoteDate")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda item: supplement(item[0], item[1][0]), (
            item for item in INDICES.items() if item[0] not in quotes or item[0] in NATIVE_HK
        )))


def _refresh_flows(now):
    def fetch(item):
        slug, (code, _, _) = item
        if flow := _main_flow(slug, code, now):
            _update_cache(FLOW_CACHE, slug, flow, "flowDate")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, ((slug, info) for slug, info in INDICES.items() if slug not in NATIVE_HK)))


def _refresh(kind, function, now, ttl):
    try:
        function(now)
    finally:
        with _STATE_LOCK:
            _REFRESHING.discard(kind)
            _NEXT_REFRESH[kind] = monotonic() + ttl


def index_market_data():
    """Serve cached rows immediately while independent quote and flow jobs update them."""
    now = datetime.now(MARKET_TZ)
    intraday = now.weekday() < 5 and 9 <= now.hour < 17
    with _STATE_LOCK:
        for kind, function, ttl in (
            ("quotes", _refresh_quotes, 20 if intraday else 120),
            ("flows", _refresh_flows, 80 if intraday else 300),
        ):
            if kind not in _REFRESHING and monotonic() >= _NEXT_REFRESH[kind]:
                _REFRESHING.add(kind)
                Thread(target=_refresh, args=(kind, function, now, ttl), daemon=True).start()
        return {"quotes": dict(QUOTE_CACHE), "flows": dict(FLOW_CACHE), "updating": bool(_REFRESHING)}
