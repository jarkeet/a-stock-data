"""为市场情绪仪表盘提供本地静态页面和跨市场自选股行情 API。"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


THS_TIME_URL = "https://d.10jqka.com.cn/v2/time/{identifier}/last.js"
THS_REALHEAD_URL = "https://d.10jqka.com.cn/v2/realhead/{identifier}/last.js"
THS_SEARCH_URL = (
    "https://news.10jqka.com.cn/app/headline/"
    "mobi-stockdict/v1/search/"
)
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={identifier}"
TENCENT_MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
NAVER_BASIC_URL = "https://m.stock.naver.com/api/stock/{code}/basic"
NAVER_MINUTE_URL = "https://api.stock.naver.com/chart/domestic/item/{code}/minute"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://stockpage.10jqka.com.cn/",
}
MARKET_LABELS = {"A": "A股", "HK": "港股", "US": "美股", "KR": "韩股"}
MARKET_SESSIONS = {
    "A": ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)),
    "HK": ((9 * 60 + 30, 12 * 60), (13 * 60, 16 * 60)),
    "US": ((9 * 60 + 30, 16 * 60),),
    "KR": ((9 * 60, 15 * 60 + 30),),
}
MARKET_TIMEZONES = {
    "A": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "US": "America/New_York",
    "KR": "Asia/Seoul",
}
# 韩股行情源通常只返回韩文或英文名称。这里维护经过确认的常用中文名，
# 并在所有行情源、搜索结果之上统一覆盖，避免切换备用源后名称发生变化。
KR_CHINESE_NAMES = {
    "000660": "SK海力士",
    "005930": "三星电子",
}
MAX_WATCHLIST_SIZE = 30
MAX_QUOTE_WORKERS = 8
CACHE_TTL_SECONDS = 20
VOLUME_AVERAGE_CACHE_TTL_SECONDS = 6 * 60 * 60
_CACHE: dict[str, tuple[float, dict]] = {}
_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}
_VOLUME_AVERAGE_CACHE: dict[str, tuple[float, float]] = {}
_CACHE_LOCK = threading.Lock()
_WATCHLIST_STORAGE_LOCK = threading.Lock()
_LOCAL = threading.local()
_QUOTE_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_QUOTE_WORKERS,
    thread_name_prefix="watchlist-quote",
)


def preferred_security_name(symbol: dict, upstream_name) -> str:
    """返回面向中文界面的证券名；没有中文映射时保留行情源名称。"""
    if symbol.get("market") == "KR":
        base_code = str(symbol.get("code") or "").split(".", 1)[0]
        chinese_name = KR_CHINESE_NAMES.get(base_code)
        if chinese_name:
            return chinese_name
    return str(upstream_name or symbol.get("code") or "")


def normalize_watchlist_items(items) -> list[dict[str, str]]:
    """校验、归一化并去重持久化的自选股列表。"""
    if not isinstance(items, list):
        raise ValueError("items 必须为数组")
    if len(items) > MAX_WATCHLIST_SIZE:
        raise ValueError(f"自选股最多 {MAX_WATCHLIST_SIZE} 只")

    normalized = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("自选股条目格式无效")
        symbol = normalize_watch_symbol(item.get("market"), item.get("code"))
        if symbol["key"] in seen:
            continue
        seen.add(symbol["key"])
        normalized.append({"market": symbol["market"], "code": symbol["code"]})
    return normalized


def load_persisted_watchlist(path: Path) -> tuple[bool, list[dict[str, str]]]:
    """读取服务端自选股；文件不存在表示尚未完成旧数据迁移。"""
    with _WATCHLIST_STORAGE_LOCK:
        if not path.exists():
            return False, []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw_items = payload.get("items", []) if isinstance(payload, dict) else payload
            return True, normalize_watchlist_items(raw_items)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise ValueError(f"自选股存储文件损坏：{error}") from error


def save_persisted_watchlist(path: Path, items) -> list[dict[str, str]]:
    """原子写入自选股，避免刷新或进程退出时留下半个 JSON 文件。"""
    normalized = normalize_watchlist_items(items)
    payload = json.dumps(
        {"version": 1, "items": normalized},
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    with _WATCHLIST_STORAGE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary_path.write_text(payload, encoding="utf-8")
            temporary_path.replace(path)
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return normalized


def _session() -> requests.Session:
    if not hasattr(_LOCAL, "session"):
        _LOCAL.session = requests.Session()
        _LOCAL.session.headers.update(HEADERS)
        # Windows 上复用到被上游关闭的 Keep-Alive 连接时，requests 会抛
        # ConnectionResetError(10054)。行情查询都是幂等 GET，允许 urllib3
        # 丢弃坏连接并短退避重试，避免一次瞬态断连直接击穿自选股整行。
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.25,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        _LOCAL.session.mount("https://", adapter)
        _LOCAL.session.mount("http://", adapter)
    return _LOCAL.session


def normalize_watch_symbol(market: str, raw_code: str) -> dict:
    """归一化用户输入，并生成各行情源所需的证券标识。"""
    market = str(market or "").strip().upper()
    code = re.sub(r"\s+", "", str(raw_code or "")).upper()
    if market not in MARKET_LABELS:
        raise ValueError("不支持的市场")

    if market == "A":
        code = re.sub(r"^(SH|SZ|BJ)", "", code)
        code = re.sub(r"\.(SH|SS|SZ|BJ)$", "", code)
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("A股代码应为6位数字")
        exchange = "SH" if code.startswith(("6", "9")) else (
            "BJ" if code.startswith(("4", "8")) else "SZ"
        )
        yahoo_suffix = {"SH": "SS", "SZ": "SZ", "BJ": "BJ"}[exchange]
        return {
            "market": market,
            "market_label": MARKET_LABELS[market],
            "code": code,
            "key": f"{market}:{code}",
            "ths_identifier": f"hs_{code}",
            "tencent_identifier": f"{exchange.lower()}{code}",
            "yahoo_symbol": f"{code}.{yahoo_suffix}",
        }

    if market == "HK":
        code = re.sub(r"^HK", "", code)
        code = re.sub(r"\.HK$", "", code)
        if not re.fullmatch(r"\d{1,5}", code):
            raise ValueError("港股代码应为1至5位数字")
        number = int(code)
        if number <= 0:
            raise ValueError("港股代码无效")
        display_code = f"{number:05d}"
        short_code = f"{number:04d}"
        return {
            "market": market,
            "market_label": MARKET_LABELS[market],
            "code": display_code,
            "key": f"{market}:{display_code}",
            "ths_identifier": f"hk_HK{short_code}",
            "tencent_identifier": f"hk{display_code}",
            "yahoo_symbol": f"{short_code}.HK",
        }

    if market == "US":
        code = re.sub(r"\.US$", "", code)
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", code):
            raise ValueError("美股代码格式无效")
        return {
            "market": market,
            "market_label": MARKET_LABELS[market],
            "code": code,
            "key": f"{market}:{code}",
            "ths_identifier": f"usa_{code}",
            "tencent_identifier": None,
            "yahoo_symbol": code.replace(".", "-"),
        }

    code = re.sub(r"^KR", "", code)
    suffix_match = re.search(r"\.(KS|KQ)$", code)
    suffix = suffix_match.group(1) if suffix_match else "KS"
    code = re.sub(r"\.(KS|KQ)$", "", code)
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("韩股代码应为6位数字，可附 .KS 或 .KQ")
    display_code = f"{code}.{suffix}"
    return {
        "market": market,
        "market_label": MARKET_LABELS[market],
        "code": display_code,
        "key": f"{market}:{display_code}",
        # 同花顺公开分时端点目前未覆盖韩股，直接使用备用源。
        "ths_identifier": None,
        "tencent_identifier": None,
        "yahoo_symbol": display_code,
    }


def _unwrap_jsonp(text: str) -> dict:
    start, end = text.find("("), text.rfind(")")
    if start < 0 or end <= start:
        raise ValueError("行情响应格式异常")
    return json.loads(text[start + 1:end])


def _session_progress(market: str, minute_of_day: int) -> float:
    """按各市场实际交易分钟计算当日分时横轴进度，午休不计时。"""
    sessions = MARKET_SESSIONS[market]
    total = sum(end - start for start, end in sessions)
    elapsed = 0
    for start, end in sessions:
        if minute_of_day <= start:
            break
        elapsed += min(minute_of_day, end) - start
        if minute_of_day < end:
            break
    return max(0.0, min(1.0, elapsed / total))


def _progress_from_clock(market: str, value: str) -> float:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 4:
        return 1.0
    hour, minute = int(digits[-4:-2]), int(digits[-2:])
    if hour > 23 or minute > 59:
        return 1.0
    return _session_progress(market, hour * 60 + minute)


def _progress_from_timestamp(market: str, timestamp: int | float | None) -> float:
    if not timestamp:
        return 1.0
    local_time = datetime.fromtimestamp(
        int(timestamp), ZoneInfo(MARKET_TIMEZONES[market])
    )
    return _session_progress(market, local_time.hour * 60 + local_time.minute)


def _safe_positive_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _yahoo_average_daily_volume(symbol: dict, quote_timestamp=None) -> float | None:
    """取当前交易日前最近 5 个完整交易日均量，供无直接量比的市场计算。"""
    cache_key = symbol["yahoo_symbol"]
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _VOLUME_AVERAGE_CACHE.get(cache_key)
        if cached and now - cached[0] < VOLUME_AVERAGE_CACHE_TTL_SECONDS:
            return cached[1]

    response = _session().get(
        YAHOO_CHART_URL.format(symbol=cache_key),
        params={"interval": "1d", "range": "1mo"},
        headers={"User-Agent": HEADERS["User-Agent"]},
        timeout=12,
    )
    response.raise_for_status()
    result = ((response.json().get("chart") or {}).get("result") or [None])[0]
    if not result:
        return None
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [{}])[0])
    volumes = quote.get("volume") or []
    current_date = None
    if quote_timestamp:
        current_date = datetime.fromtimestamp(
            int(quote_timestamp), ZoneInfo(MARKET_TIMEZONES[symbol["market"]])
        ).date()
    completed = []
    timezone = ZoneInfo(MARKET_TIMEZONES[symbol["market"]])
    for timestamp, volume in zip(timestamps, volumes):
        number = _safe_positive_float(volume)
        if number is None:
            continue
        if current_date and datetime.fromtimestamp(int(timestamp), timezone).date() >= current_date:
            continue
        completed.append(number)
    if not completed:
        return None
    average = sum(completed[-5:]) / len(completed[-5:])
    with _CACHE_LOCK:
        _VOLUME_AVERAGE_CACHE[cache_key] = (now, average)
    return average


def _calculated_volume_ratio(
    symbol: dict,
    current_volume,
    progress: float,
    quote_timestamp=None,
) -> float | None:
    volume = _safe_positive_float(current_volume)
    if volume is None or progress <= 0:
        return None
    try:
        average = _yahoo_average_daily_volume(symbol, quote_timestamp)
    except Exception:
        return None
    if not average:
        return None
    return volume / (average * min(1.0, progress))


def _fetch_tencent_quote(symbol: dict) -> dict:
    """获取腾讯实时价，并在分钟线可用时附上同交易日走势。"""
    identifier = symbol.get("tencent_identifier")
    if not identifier:
        raise LookupError("腾讯行情端点未覆盖该市场")

    quote_identifier = (
        f"r_{identifier}" if symbol["market"] == "HK" else identifier
    )
    fields = []
    for _ in range(2):
        response = _session().get(
            TENCENT_QUOTE_URL.format(identifier=quote_identifier), timeout=12
        )
        response.raise_for_status()
        quote_text = response.content.decode("gbk", errors="replace")
        match = re.search(r'="([^"]*)"', quote_text)
        fields = match.group(1).split("~") if match else []
        if len(fields) > 32:
            break
    if len(fields) <= 32:
        raise ValueError(f"腾讯实时行情字段不完整（{len(fields)} 项）")

    try:
        price = float(fields[3])
        previous_close = float(fields[4])
    except (TypeError, ValueError) as error:
        raise ValueError("腾讯实时行情价格字段异常") from error
    if price <= 0:
        raise ValueError("腾讯实时行情未返回有效最新价")
    change_pct = (
        (price / previous_close - 1) * 100 if previous_close > 0 else None
    )
    quote_time = str(fields[30] or "")
    quote_date = re.sub(r"\D", "", quote_time)[:8]

    # 分钟线只是迷你走势图的增强数据。停牌/退市整理股票可能仍有有效实时价，
    # 但分钟端点会返回空或被上游重置连接；此时不能让辅助数据拖垮整条报价。
    minute_payload = {}
    try:
        minute_response = _session().get(
            TENCENT_MINUTE_URL,
            params={"code": identifier},
            timeout=12,
        )
        minute_response.raise_for_status()
        minute_payload = (
            ((minute_response.json().get("data") or {}).get(identifier) or {})
            .get("data") or {}
        )
    except (requests.RequestException, ValueError):
        minute_payload = {}
    minute_date = str(minute_payload.get("date") or "")

    rows = []
    if not quote_date or minute_date == quote_date:
        for item in minute_payload.get("data") or []:
            parts = str(item).split()
            if len(parts) < 2:
                continue
            try:
                rows.append((parts[0], float(parts[1])))
            except (TypeError, ValueError):
                continue

    # 分钟线最后一笔应与实时价处于同一尺度；超过 3% 通常意味着缓存串日。
    if rows:
        last_minute_price = rows[-1][1]
        if last_minute_price <= 0 or abs(last_minute_price / price - 1) > 0.03:
            rows = []
    if rows:
        sparkline = [value for _, value in rows]
        if sparkline[-1] != price:
            sparkline.append(price)
        sparkline_progress = _progress_from_clock(symbol["market"], rows[-1][0])
    else:
        sparkline = [price]
        quote_digits = re.sub(r"\D", "", quote_time)
        quote_clock = quote_digits[8:12] if len(quote_digits) >= 12 else quote_digits
        sparkline_progress = _progress_from_clock(symbol["market"], quote_clock)
    volume_ratio_index = 49 if symbol["market"] == "A" else 50
    volume_ratio = (
        _safe_positive_float(fields[volume_ratio_index])
        if len(fields) > volume_ratio_index else None
    )
    turnover = _safe_positive_float(fields[37]) if len(fields) > 37 else None
    # 腾讯 A 股成交额字段单位为万元，港股字段直接为港元。
    if turnover is not None and symbol["market"] == "A":
        turnover *= 10_000

    return {
        **{key: symbol[key] for key in ("key", "market", "market_label", "code")},
        "name": str(fields[1] or symbol["code"]),
        "price": price,
        "change_pct": change_pct,
        "turnover": turnover,
        "volume_ratio": volume_ratio,
        "previous_close": previous_close or None,
        "sparkline": sparkline,
        "sparkline_progress": sparkline_progress,
        "quote_time": quote_time,
        "currency": {"A": "CNY", "HK": "HKD"}[symbol["market"]],
        "source": "腾讯财经",
        "is_fallback": False,
    }


def _fetch_ths_quote(symbol: dict) -> dict:
    identifier = symbol.get("ths_identifier")
    if not identifier:
        raise LookupError("同花顺公开分时端点未覆盖该市场")
    response = _session().get(
        THS_TIME_URL.format(identifier=identifier), timeout=12
    )
    response.raise_for_status()
    payload = _unwrap_jsonp(response.text).get(identifier) or {}
    rows = []
    for item in str(payload.get("data") or "").split(";"):
        parts = item.split(",")
        if len(parts) >= 2:
            try:
                rows.append((parts[0], float(parts[1])))
            except (TypeError, ValueError):
                continue
    if not rows:
        raise ValueError("同花顺未返回分时行情")
    price = rows[-1][1]
    previous_close = float(payload.get("pre") or 0)
    change_pct = (
        (price / previous_close - 1) * 100 if previous_close > 0 else None
    )
    quote_time = f"{payload.get('date', '')} {rows[-1][0]}"
    # 分时端点偶尔会短暂返回缓存切片；最新价和涨跌幅再用 realhead 校准。
    head_response = _session().get(
        THS_REALHEAD_URL.format(identifier=identifier), timeout=12
    )
    head_response.raise_for_status()
    head = _unwrap_jsonp(head_response.text)
    head_items = head.get("items") or {}
    head_price = (
        float(head_items["10"])
        if head_items.get("10") not in (None, "") else None
    )
    head_change = (
        float(head_items["199112"])
        if head_items.get("199112") not in (None, "") else None
    )
    # 港股 realhead 偶发出现价格与昨收/涨幅互相矛盾的缓存。只有三者
    # 能自洽时才用它覆盖分时末值，避免把错误缓存当成最新价。
    head_is_consistent = False
    if head_price is not None and previous_close > 0:
        recalculated = (head_price / previous_close - 1) * 100
        head_is_consistent = (
            head_change is not None and abs(recalculated - head_change) <= 0.25
        )
    elif head_price is not None:
        head_is_consistent = abs(head_price / price - 1) <= 0.02
    if head_is_consistent:
        price = head_price
        change_pct = head_change
        if head.get("updateTime"):
            quote_time = str(head["updateTime"])
    sparkline_progress = _progress_from_clock(symbol["market"], rows[-1][0])
    quote_timestamp = None
    try:
        local_quote_time = datetime.strptime(
            f"{payload.get('date', '')}{rows[-1][0]}", "%Y%m%d%H%M"
        ).replace(tzinfo=ZoneInfo(MARKET_TIMEZONES[symbol["market"]]))
        quote_timestamp = int(local_quote_time.timestamp())
    except (TypeError, ValueError):
        pass
    volume_ratio = _calculated_volume_ratio(
        symbol, head_items.get("13"), sparkline_progress, quote_timestamp
    )
    turnover = _safe_positive_float(head_items.get("19"))
    return {
        **{key: symbol[key] for key in ("key", "market", "market_label", "code")},
        "name": str(payload.get("name") or symbol["code"]),
        "price": price,
        "change_pct": change_pct,
        "turnover": turnover,
        "volume_ratio": volume_ratio,
        "previous_close": previous_close or None,
        "sparkline": [value for _, value in rows],
        "sparkline_progress": sparkline_progress,
        "quote_time": quote_time,
        "currency": {"A": "CNY", "HK": "HKD", "US": "USD"}.get(symbol["market"], ""),
        "source": "同花顺",
        "is_fallback": False,
    }


def _fetch_yahoo_quote(
    symbol: dict, fallback_reason: str = "", is_fallback: bool = True
) -> dict:
    response = _session().get(
        YAHOO_CHART_URL.format(symbol=symbol["yahoo_symbol"]),
        params={"interval": "5m", "range": "1d"},
        headers={"User-Agent": HEADERS["User-Agent"]},
        timeout=12,
    )
    response.raise_for_status()
    result = ((response.json().get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise ValueError("备用行情源未返回数据")
    meta = result.get("meta") or {}
    quote = (((result.get("indicators") or {}).get("quote") or [{}])[0])
    closes = [
        float(value) for value in (quote.get("close") or []) if value is not None
    ]
    price = meta.get("regularMarketPrice")
    if price is None and closes:
        price = closes[-1]
    if price is None:
        raise ValueError("备用行情源未返回最新价")
    previous_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    change_pct = (
        (float(price) / float(previous_close) - 1) * 100
        if previous_close not in (None, 0) else None
    )
    quote_timestamp = meta.get("regularMarketTime")
    quote_time = (
        datetime.fromtimestamp(int(quote_timestamp)).strftime("%Y-%m-%d %H:%M")
        if quote_timestamp else ""
    )
    sparkline_progress = _progress_from_timestamp(
        symbol["market"], quote_timestamp
    )
    current_volume = meta.get("regularMarketVolume")
    if current_volume in (None, 0):
        current_volume = sum(
            float(value) for value in (quote.get("volume") or [])
            if value is not None
        )
    volume_ratio = _calculated_volume_ratio(
        symbol, current_volume, sparkline_progress, quote_timestamp
    )
    turnover = sum(
        float(close) * float(volume)
        for close, volume in zip(
            quote.get("close") or [], quote.get("volume") or []
        )
        if close is not None and volume is not None
    )
    if turnover <= 0:
        volume_number = _safe_positive_float(current_volume)
        turnover = (
            float(price) * volume_number if volume_number is not None else None
        )
    return {
        **{key: symbol[key] for key in ("key", "market", "market_label", "code")},
        "name": str(
            meta.get("longName") or meta.get("shortName") or symbol["code"]
        ),
        "price": float(price),
        "change_pct": change_pct,
        "turnover": turnover,
        "volume_ratio": volume_ratio,
        "previous_close": (
            float(previous_close) if previous_close not in (None, 0) else None
        ),
        "sparkline": closes or [float(price)],
        "sparkline_progress": sparkline_progress,
        "quote_time": quote_time,
        "currency": str(meta.get("currency") or ""),
        "source": "Yahoo Finance",
        "is_fallback": is_fallback,
        "fallback_reason": fallback_reason,
    }


def _naver_number(value) -> float | None:
    """把 Naver 行情中的千分位字符串转换为数字。"""
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _fetch_naver_quote(symbol: dict) -> dict:
    """获取韩国交易所行情；Naver 覆盖 KOSPI/KOSDAQ 且无需 Yahoo 后缀。"""
    code = str(symbol["code"]).split(".", 1)[0]
    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": "https://m.stock.naver.com/",
    }
    response = _session().get(
        NAVER_BASIC_URL.format(code=code), headers=headers, timeout=12
    )
    response.raise_for_status()
    payload = response.json()

    price = _naver_number(payload.get("closePrice"))
    if price is None or price <= 0:
        raise ValueError("Naver 未返回有效最新价")
    change = _naver_number(payload.get("compareToPreviousClosePrice"))
    previous_close = price - change if change is not None else None
    change_pct = _naver_number(payload.get("fluctuationsRatio"))
    if change_pct is None and previous_close not in (None, 0):
        change_pct = (price / previous_close - 1) * 100

    exchange_info = payload.get("stockExchangeType") or {}
    suffix = str(exchange_info.get("code") or "").upper()
    if suffix not in {"KS", "KQ"}:
        suffix = str(symbol["code"]).rsplit(".", 1)[-1]
    display_code = f"{code}.{suffix}"
    quote_time = str(payload.get("localTradedAt") or "")

    sparkline = []
    turnover = None
    trade_date = re.sub(r"\D", "", quote_time)[:8]
    if not trade_date:
        trade_date = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    try:
        minute_response = _session().get(
            NAVER_MINUTE_URL.format(code=code),
            params={
                "startTime": f"{trade_date}0900",
                "endTime": f"{trade_date}1530",
            },
            headers=headers,
            timeout=12,
        )
        minute_response.raise_for_status()
        minute_rows = minute_response.json()
        if isinstance(minute_rows, list):
            prices_and_volumes = []
            for row in minute_rows:
                row_price = _naver_number(row.get("currentPrice"))
                row_volume = _naver_number(row.get("accumulatedTradingVolume"))
                if row_price is not None and row_price > 0:
                    sparkline.append(row_price)
                    if row_volume is not None and row_volume >= 0:
                        prices_and_volumes.append((row_price, row_volume))
            if prices_and_volumes:
                turnover = sum(
                    row_price * volume for row_price, volume in prices_and_volumes
                )
    except (requests.RequestException, ValueError, TypeError):
        # 分钟线是走势图增强数据，不能因其瞬时失败拖垮有效的最新价。
        sparkline = []

    if not sparkline:
        sparkline = [price]
    elif sparkline[-1] != price:
        sparkline.append(price)
    quote_timestamp = None
    try:
        quote_timestamp = int(datetime.fromisoformat(quote_time).timestamp())
    except (TypeError, ValueError):
        pass
    sparkline_progress = _progress_from_timestamp("KR", quote_timestamp)
    # 不再为了量比反向请求 Yahoo；Yahoo 正是韩股本次故障源。
    # Naver 当前公开响应没有近 5 日均量，量比留空比阻塞整行行情更可靠。
    volume_ratio = None

    return {
        "key": f"KR:{display_code}",
        "market": "KR",
        "market_label": MARKET_LABELS["KR"],
        "code": display_code,
        "name": str(payload.get("stockName") or display_code),
        "price": price,
        "change_pct": change_pct,
        "turnover": turnover,
        "volume_ratio": volume_ratio,
        "previous_close": previous_close,
        "sparkline": sparkline,
        "sparkline_progress": sparkline_progress,
        "quote_time": quote_time,
        "currency": "KRW",
        "source": "Naver Finance",
        "is_fallback": False,
    }


def fetch_watch_quote(market: str, code: str) -> dict:
    symbol = normalize_watch_symbol(market, code)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(symbol["key"])
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    if symbol["market"] in {"A", "HK"}:
        try:
            quote = _fetch_tencent_quote(symbol)
        except Exception as error:
            tencent_error = f"{type(error).__name__}: {error}"
            quote = _fetch_yahoo_quote(symbol, tencent_error)
    elif symbol["market"] == "KR":
        try:
            quote = _fetch_naver_quote(symbol)
        except Exception as naver_error:
            try:
                quote = _fetch_yahoo_quote(
                    symbol,
                    f"{type(naver_error).__name__}: {naver_error}",
                    is_fallback=True,
                )
            except Exception as yahoo_error:
                raise ValueError(
                    f"韩股 {symbol['code']} 行情不可用，请确认代码和交易所后缀；"
                    "SK海力士代码为 000660.KS"
                ) from yahoo_error
    else:
        try:
            quote = _fetch_ths_quote(symbol)
        except Exception as error:
            ths_error = f"{type(error).__name__}: {error}"
            quote = _fetch_yahoo_quote(symbol, ths_error)
    quote["name"] = preferred_security_name(symbol, quote.get("name"))
    with _CACHE_LOCK:
        _CACHE[symbol["key"]] = (now, quote)
    return quote


def _fetch_watch_quote_result(index_and_item) -> dict:
    """抓取单只行情并转换为批量接口的稳定结果格式。"""
    index, item = index_and_item
    try:
        if not isinstance(item, dict):
            raise ValueError("自选股条目格式无效")
        quote = fetch_watch_quote(item.get("market"), item.get("code"))
        return {"request_index": index, "ok": True, **quote}
    except Exception as error:
        item = item if isinstance(item, dict) else {}
        return {
            "request_index": index,
            "ok": False,
            "market": str(item.get("market") or ""),
            "code": str(item.get("code") or ""),
            "error": str(error),
        }


def fetch_watch_quotes(items: list) -> list[dict]:
    """用共享线程池抓取整组行情，保持顺序并复用各线程的 HTTP 连接。"""
    if not items:
        return []
    return list(_QUOTE_EXECUTOR.map(_fetch_watch_quote_result, enumerate(items)))


def _ths_search_market(label: str) -> str | None:
    label = str(label or "")
    if label in {"沪A", "深A", "北A"}:
        return "A"
    if label == "港股":
        return "HK"
    if label == "美股":
        return "US"
    return None


def _search_ths(query: str, market: str) -> list[dict]:
    response = _session().get(
        THS_SEARCH_URL,
        params={
            "isrealcode": "1",
            "associate": "1",
            "json": "1",
            "markettype": "2",
            "query": query,
        },
        headers={
            "User-Agent": HEADERS["User-Agent"],
            "Referer": "https://www.10jqka.com.cn/",
        },
        timeout=10,
    )
    response.raise_for_status()
    # 该接口未声明 charset，但当前响应是 UTF-8 JSON。
    payload = json.loads(response.content.decode("utf-8"))
    rows = ((payload.get("data") or {}).get("body") or [])
    results = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            continue
        row_market = _ths_search_market(row[5])
        if row_market != market:
            continue
        try:
            symbol = normalize_watch_symbol(row_market, row[6] or row[0])
        except ValueError:
            continue
        results.append({
            "market": row_market,
            "market_label": MARKET_LABELS[row_market],
            "code": symbol["code"],
            "name": str(row[1] or symbol["code"]),
            "search_alias": str(row[2] or ""),
            "source": "同花顺",
        })
    return results


def _yahoo_result_market(symbol: str) -> str | None:
    symbol = str(symbol or "").upper()
    if symbol.endswith((".KS", ".KQ")):
        return "KR"
    if symbol.endswith(".HK"):
        return "HK"
    if symbol.endswith((".SS", ".SZ", ".BJ")):
        return "A"
    if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", symbol):
        return "US"
    return None


def _search_yahoo(query: str, market: str) -> list[dict]:
    response = _session().get(
        YAHOO_SEARCH_URL,
        params={"q": query, "quotesCount": 12, "newsCount": 0},
        headers={"User-Agent": HEADERS["User-Agent"]},
        timeout=10,
    )
    response.raise_for_status()
    results = []
    for item in response.json().get("quotes") or []:
        if item.get("quoteType") != "EQUITY":
            continue
        raw_symbol = str(item.get("symbol") or "").upper()
        if _yahoo_result_market(raw_symbol) != market:
            continue
        try:
            symbol = normalize_watch_symbol(market, raw_symbol)
        except ValueError:
            continue
        results.append({
            "market": market,
            "market_label": MARKET_LABELS[market],
            "code": symbol["code"],
            "name": preferred_security_name(
                symbol,
                item.get("longname") or item.get("shortname") or symbol["code"],
            ),
            "search_alias": "",
            "source": "Yahoo Finance",
        })
    return results


def search_watch_symbols(query: str, market: str, limit: int = 8) -> list[dict]:
    """按代码、中文、拼音首字母或英文名称搜索可添加标的。"""
    market = str(market or "").strip().upper()
    query = re.sub(r"\s+", " ", str(query or "")).strip()
    if market not in MARKET_LABELS:
        raise ValueError("不支持的市场")
    if not query:
        return []
    if len(query) > 64:
        raise ValueError("搜索内容过长")

    cache_key = f"{market}:{query.casefold()}"
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _SEARCH_CACHE.get(cache_key)
        if cached and now - cached[0] < 60:
            return cached[1][:limit]

    candidates: list[dict] = []
    # A/港/美优先使用同花顺证券联想；韩股同花顺公开搜索暂未覆盖。
    if market != "KR":
        try:
            candidates.extend(_search_ths(query, market))
        except Exception:
            pass
    if market == "KR" or not candidates:
        try:
            candidates.extend(_search_yahoo(query, market))
        except Exception:
            pass

    seen = set()
    results = []
    for candidate in candidates:
        key = f"{candidate['market']}:{candidate['code']}"
        if key in seen:
            continue
        seen.add(key)
        results.append(candidate)
        if len(results) >= limit:
            break
    with _CACHE_LOCK:
        _SEARCH_CACHE[cache_key] = (now, results)
    return results


class WatchlistHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        address,
        handler,
        dashboard_path: Path,
        watchlist_storage_path: Path,
        instance_token: str = "",
    ):
        super().__init__(address, handler)
        self.dashboard_path = dashboard_path
        self.watchlist_storage_path = watchlist_storage_path
        self.instance_token = instance_token


class WatchlistHandler(BaseHTTPRequestHandler):
    server: WatchlistHTTPServer

    def log_message(self, format_string, *args):
        print(f"[watchlist] {self.address_string()} - {format_string % args}")

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._send_json({
                "ok": True,
                "instance_token": self.server.instance_token,
            })
            return
        if path == "/api/watchlist/search":
            params = parse_qs(parsed.query)
            try:
                results = search_watch_symbols(
                    (params.get("q") or [""])[0],
                    (params.get("market") or [""])[0],
                )
            except ValueError as error:
                self._send_json({"error": str(error)}, 400)
                return
            self._send_json({"results": results})
            return
        if path == "/api/watchlist/items":
            try:
                initialized, items = load_persisted_watchlist(
                    self.server.watchlist_storage_path
                )
            except ValueError as error:
                self._send_json({"error": str(error)}, 500)
                return
            self._send_json({"initialized": initialized, "items": items})
            return
        if path == "/a-share-pe-valuation-report.html":
            report_path = self.server.dashboard_path.with_name(
                "a-share-pe-valuation-report.html"
            )
            try:
                body = report_path.read_bytes()
            except FileNotFoundError:
                self.send_error(404, "Valuation report has not been generated yet")
                return
            except OSError as error:
                self.send_error(500, str(error))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass
            return
        if path not in ("/", "/market_sentiment_dashboard.html"):
            self.send_error(404)
            return
        try:
            body = self.server.dashboard_path.read_bytes()
        except OSError as error:
            self.send_error(500, str(error))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/watchlist/items", "/api/watchlist/quotes"):
            self.send_error(404)
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 65536)
            payload = json.loads(self.rfile.read(length) or b"{}")
            items = payload.get("items") or []
            if not isinstance(items, list):
                raise ValueError("items 必须为数组")
            if len(items) > MAX_WATCHLIST_SIZE:
                raise ValueError(f"自选股最多 {MAX_WATCHLIST_SIZE} 只")
        except (ValueError, json.JSONDecodeError) as error:
            self._send_json({"error": str(error)}, 400)
            return

        if path == "/api/watchlist/items":
            try:
                saved_items = save_persisted_watchlist(
                    self.server.watchlist_storage_path,
                    items,
                )
            except ValueError as error:
                self._send_json({"error": str(error)}, 400)
                return
            except OSError as error:
                self._send_json({"error": f"保存自选股失败：{error}"}, 500)
                return
            self._send_json({"ok": True, "items": saved_items})
            return

        results = fetch_watch_quotes(items)
        self._send_json({
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "results": results,
        })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dashboard", default="market_sentiment_dashboard.html")
    parser.add_argument(
        "--watchlist-storage",
        help="自选股持久化 JSON 路径（默认与仪表盘放在同一目录）",
    )
    parser.add_argument("--instance-token", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    dashboard_path = Path(args.dashboard).resolve()
    watchlist_storage_path = (
        Path(args.watchlist_storage).resolve()
        if args.watchlist_storage
        else dashboard_path.with_name("market_watchlist.json")
    )
    server = WatchlistHTTPServer(
        (args.host, args.port),
        WatchlistHandler,
        dashboard_path,
        watchlist_storage_path,
        args.instance_token,
    )
    print(
        f"dashboard server: http://{args.host}:{args.port}/ "
        f"({dashboard_path}); watchlist: {watchlist_storage_path}"
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
