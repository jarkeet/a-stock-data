"""A股恐惧贪婪指数：采集长历史数据、双支柱模型计算与交互式页面渲染。"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import html
import json
from pathlib import Path
import random
import threading
import time

import numpy as np
import pandas as pd
import requests

from market_capital_risk_appetite import _market_margin_history


TENCENT_KLINE_URLS = (
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

BROAD_INDICES = [
    ("sz399001", "399001", "深证成指"),
    ("sh000300", "000300", "沪深300"),
    ("sh000905", "000905", "中证500"),
    ("sh000852", "000852", "中证1000"),
    ("sz399006", "399006", "创业板指"),
]

SHORT_COMPONENTS = [
    ("vol_score", "波动率", 25, "20日年化波动率（反向：波动越大越恐慌）"),
    ("turnover_score", "相对换手率", 20, "五宽基成交量相对60日均量的活跃度"),
    ("margin_score", "融资融券", 20, "沪深两市融资余额20日变动率与净加仓强度"),
    ("breadth_score", "市场宽度", 15, "全市场上涨家数占比分布（全A真实日频）"),
    ("rsi_score", "RSI", 10, "五宽基14日RSI中位数的短期价格动能"),
    ("limit_score", "涨跌停比", 10, "全市场涨停与跌停家数强弱对比（全A真实日频）"),
]

FETCH_START_DATE = "2013-01-01"
DISPLAY_START_DATE = "2016-01-01"
ROLLING_WINDOW = 756
PERCENTILE_10Y_WINDOW = 2520

DEFAULT_INDEX_CACHE = Path("market_fear_greed_index_cache.csv")
DEFAULT_MARGIN_CACHE = Path("market_fear_greed_margin_cache.csv")
DEFAULT_BREADTH_HISTORY_CACHE = Path("market_fear_greed_breadth_history.csv")
DEFAULT_UNIVERSE_CACHE = Path("market_stock_universe.csv")
DEFAULT_CSI_ALL_CACHE = Path("market_valuation_csi_all.csv")
DEFAULT_SENTIMENT_DAILY = Path("market_sentiment_daily.csv")
DEFAULT_SENTIMENT_INTRADAY = Path("market_sentiment_intraday.csv")
DEFAULT_HISTORY_CSV = Path("market_fear_greed_history.csv")
DEFAULT_OUTPUT_HTML = Path("market_fear_greed.html")


def mood_meta(score: float) -> tuple[str, str, str]:
    """根据0-100得分返回五档情绪标签、CSS类名与代表色。"""
    if pd.isna(score):
        return "暂无数据", "tone-missing", "#8a96a8"
    if score <= 25:
        return "极度恐慌", "mood-extreme-fear", "#087f5b"
    if score <= 45:
        return "恐慌", "mood-fear", "#2b8a57"
    if score <= 55:
        return "中性", "mood-neutral", "#b47b00"
    if score <= 75:
        return "贪婪", "mood-greed", "#d9534f"
    return "极度贪婪", "mood-extreme-greed", "#b42318"


def rolling_weak_percentile(
    series: pd.Series,
    window: int = ROLLING_WINDOW,
    min_periods: int = 120,
    ascending: bool = True,
) -> pd.Series:
    """计算滚动历史弱秩百分位（0-100）；ascending=False 表示值越大得分越低。"""
    values = pd.to_numeric(series, errors="coerce")
    target = values if ascending else -values
    return target.rolling(window=window, min_periods=min_periods).rank(
        pct=True, method="max"
    ) * 100.0


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """计算 Wilder 平滑 14 日 RSI。"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.where(avg_loss > 0, 100.0).where(avg_gain.notna(), np.nan)


def _fetch_tencent_index_range(
    secid: str, start_year: int, end_year: int, session: requests.Session
) -> pd.DataFrame:
    """按每两年分段从腾讯财经拉取宽基指数日K线（含轻量重试）。"""
    rows = []
    for year in range(start_year, end_year + 1, 2):
        seg_start = f"{year}-01-01"
        seg_end = f"{min(year + 1, end_year)}-12-31"
        payload = None
        for attempt in range(4):
            url = TENCENT_KLINE_URLS[attempt % len(TENCENT_KLINE_URLS)]
            try:
                resp = requests.get(
                    url,
                    params={"param": f"{secid},day,{seg_start},{seg_end},640,qfq"},
                    timeout=20,
                )
                resp.raise_for_status()
                payload = resp.json()
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(0.8 * (attempt + 1))
        node = (payload.get("data") or {}).get(secid) or {}
        bars = node.get("qfqday") or node.get("day") or []
        for bar in bars:
            if len(bar) >= 6:
                rows.append((bar[0], bar[1], bar[2], bar[3], bar[4], bar[5]))
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame(columns=["date", "secid", "open", "close", "high", "low", "volume"])
    frame = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    for col in ("open", "close", "high", "low", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["secid"] = secid
    return (
        frame.dropna(subset=["date", "close"])
        .sort_values("date")
        .drop_duplicates("date")
        .reset_index(drop=True)
    )


def load_or_update_broad_indices(
    cache_path: Path = DEFAULT_INDEX_CACHE,
    force: bool = False,
) -> pd.DataFrame:
    """加载并增量更新五个宽基指数 2013 年至今的日 K 线缓存。"""
    today = pd.Timestamp.now().normalize()
    existing = pd.DataFrame()
    if cache_path.exists() and not force:
        existing = pd.read_csv(cache_path, parse_dates=["date"])
        existing["date"] = existing["date"].dt.normalize()

    session = requests.Session()
    chunks = []
    for secid, _, _ in BROAD_INDICES:
        sub = existing[existing["secid"] == secid].copy() if not existing.empty else pd.DataFrame()
        if not sub.empty and sub["date"].min() <= pd.Timestamp("2015-01-10"):
            last_date = sub["date"].max()
            # 缓存覆盖到今天前最近交易日附近时仅补拉最近两年段
            start_year = max(2013, last_date.year - 1)
            fresh = _fetch_tencent_index_range(secid, start_year, today.year, session)
            merged = (
                pd.concat([sub, fresh], ignore_index=True)
                .sort_values("date")
                .drop_duplicates("date", keep="last")
            )
        else:
            merged = _fetch_tencent_index_range(secid, 2013, today.year, session)
        chunks.append(merged)

    result = (
        pd.concat(chunks, ignore_index=True)
        .sort_values(["secid", "date"])
        .drop_duplicates(["secid", "date"], keep="last")
        .reset_index(drop=True)
    )
    result.to_csv(cache_path, index=False, encoding="utf-8-sig")
    return result


def load_or_update_margin_history(
    cache_path: Path = DEFAULT_MARGIN_CACHE,
    force: bool = False,
) -> pd.DataFrame:
    """加载并增量更新沪深两市 2013 年至今的融资余额历史。"""
    end_date = pd.Timestamp.now().normalize()
    start_date = pd.Timestamp(FETCH_START_DATE)
    existing = pd.DataFrame()
    if cache_path.exists() and not force:
        existing = pd.read_csv(cache_path, parse_dates=["date"])
        existing["date"] = existing["date"].dt.normalize()

    if not existing.empty and existing["date"].min() <= pd.Timestamp("2013-02-01"):
        fetch_start = existing["date"].max() - pd.DateOffset(days=45)
    else:
        fetch_start = start_date

    sh = _market_margin_history("sh", fetch_start, end_date)
    time.sleep(0.6 + random.random() * 0.2)
    sz = _market_margin_history("sz", fetch_start, end_date)
    fresh = sh.merge(sz, on="date", how="inner", validate="one_to_one")
    fresh["hs_margin_balance_yi"] = fresh["sh_margin_balance_yi"] + fresh["sz_margin_balance_yi"]
    fresh["hs_margin_buy_yi"] = fresh["sh_margin_buy_yi"] + fresh["sz_margin_buy_yi"]

    if not existing.empty:
        merged = (
            pd.concat([existing, fresh], ignore_index=True)
            .sort_values("date")
            .drop_duplicates("date", keep="last")
            .reset_index(drop=True)
        )
    else:
        merged = fresh.sort_values("date").drop_duplicates("date").reset_index(drop=True)

    merged.to_csv(cache_path, index=False, encoding="utf-8-sig")
    return merged


def load_or_update_csi_all_closes(
    cache_path: Path = DEFAULT_CSI_ALL_CACHE,
    force: bool = False,
) -> pd.DataFrame:
    """加载中证全指（000985）日频收盘价，必要时通过中证官网增量刷新。"""
    existing = pd.DataFrame()
    if cache_path.exists():
        try:
            existing = pd.read_csv(cache_path, parse_dates=["date"])
            existing["date"] = existing["date"].dt.normalize()
            existing["close"] = pd.to_numeric(existing["close"], errors="coerce")
            existing = existing.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date", keep="last")
        except Exception:
            existing = pd.DataFrame()

    need_refresh = force or existing.empty or existing["date"].max() < pd.Timestamp.now().normalize() - pd.Timedelta(days=3)
    if need_refresh:
        try:
            from market_valuation import fetch_csi_index

            fresh = fetch_csi_index("000985", start="20130101")
            fresh["date"] = pd.to_datetime(fresh["date"]).dt.normalize()
            fresh["close"] = pd.to_numeric(fresh["close"], errors="coerce")
            fresh = fresh.dropna(subset=["date", "close"])
            if not existing.empty:
                merged = (
                    pd.concat([existing, fresh], ignore_index=True)
                    .sort_values("date")
                    .drop_duplicates("date", keep="last")
                )
            else:
                merged = fresh.sort_values("date").drop_duplicates("date", keep="last")
            merged.to_csv(cache_path, index=False, encoding="utf-8-sig", date_format="%Y-%m-%d")
            existing = merged
        except Exception:
            pass

    if existing.empty:
        return pd.DataFrame(columns=["date", "csi_all_close"])
    return existing[["date", "close"]].rename(columns={"close": "csi_all_close"}).reset_index(drop=True)


_BREADTH_THREAD_LOCAL = threading.local()


def _breadth_session() -> requests.Session:
    if not hasattr(_BREADTH_THREAD_LOCAL, "session"):
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Referer": "https://gu.qq.com/"})
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry

            adapter = HTTPAdapter(
                max_retries=Retry(
                    total=2,
                    connect=2,
                    read=2,
                    backoff_factor=0.25,
                    status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=["GET"],
                ),
                pool_connections=32,
                pool_maxsize=32,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
        except Exception:
            pass
        _BREADTH_THREAD_LOCAL.session = session
    return _BREADTH_THREAD_LOCAL.session


def _fetch_stock_breadth_contrib(
    secid: str,
    code: str,
    year_ranges: list[tuple[int, int]],
) -> dict[str, tuple[int, int, int, int, int]]:
    """拉取单只股票 2015 至今的前复权日 K 并转为逐日(有效,涨,跌,涨停,跌停)计数。"""
    session = _breadth_session()
    raw_rows: dict[str, tuple[float, float, float]] = {}
    for y0, y1 in year_ranges:
        param = f"{secid},day,{y0}-01-01,{y1}-12-31,800,qfq"
        rows = []
        for attempt in range(3):
            url = TENCENT_KLINE_URLS[attempt % len(TENCENT_KLINE_URLS)]
            try:
                resp = session.get(url, params={"param": param}, timeout=12)
                resp.raise_for_status()
                payload = resp.json().get("data")
                if isinstance(payload, dict):
                    item = payload.get(secid) or {}
                    rows = item.get("qfqday") or item.get("day") or []
                break
            except Exception:
                time.sleep(0.3 * (attempt + 1))
        if not rows:
            break
        first_date = str(rows[0][0])[:10]
        for row in rows:
            if len(row) < 5:
                continue
            d_str = str(row[0])[:10]
            try:
                c = float(row[2])
                h = float(row[3])
                lo = float(row[4])
            except (TypeError, ValueError):
                continue
            if c > 0 and h > 0 and lo > 0:
                raw_rows[d_str] = (c, h, lo)
        # 若该段最早日期晚于区间起点一个半月以上，说明此前尚未上市，提前终止
        if first_date > f"{y0}-02-15":
            break

    if len(raw_rows) < 2:
        return {}

    dates_sorted = sorted(raw_rows.keys())
    is_star = code.startswith(("688", "689"))
    is_chinext = code.startswith(("300", "301", "302"))
    is_bse = secid.startswith("bj")
    out: dict[str, tuple[int, int, int, int, int]] = {}
    prev_close = raw_rows[dates_sorted[0]][0]
    for idx in range(1, len(dates_sorted)):
        d_str = dates_sorted[idx]
        close, high, low = raw_rows[d_str]
        if prev_close <= 0:
            prev_close = close
            continue
        ret_pct = (close / prev_close - 1.0) * 100.0
        up = 1 if ret_pct > 0 else 0
        down = 1 if ret_pct < 0 else 0
        limit_up = 0
        limit_down = 0
        # 剔除上市前5个无涨跌幅限制交易日（主要针对科创/创业板注册制后）
        eligible = True
        if (is_star or (is_chinext and d_str >= "2020-08-24")) and idx < 5:
            eligible = False
        if eligible:
            if is_bse:
                limit_thr = 29.5
            elif is_star or (is_chinext and d_str >= "2020-08-24"):
                limit_thr = 19.5
            else:
                limit_thr = 9.5
            high_ret = (high / prev_close - 1.0) * 100.0
            low_ret = (low / prev_close - 1.0) * 100.0
            if high_ret >= limit_thr and close / high >= 0.999 and ret_pct >= limit_thr:
                limit_up = 1
            elif low_ret <= -limit_thr and close / low <= 1.001 and ret_pct <= -limit_thr:
                limit_down = 1
        out[d_str] = (1, up, down, limit_up, limit_down)
        prev_close = close
    return out


def backfill_market_breadth_history(
    universe_cache: Path = DEFAULT_UNIVERSE_CACHE,
    output_csv: Path = DEFAULT_BREADTH_HISTORY_CACHE,
    start_year: int = 2015,
    workers: int = 24,
) -> pd.DataFrame:
    """回溯全市场个股日 K，生成 2015 年至今的真实市场宽度与涨跌停日频历史。"""
    if universe_cache.exists():
        universe = pd.read_csv(universe_cache, dtype={"code": str})
    else:
        from market_breadth_short_term import tencent_a_share_universe

        universe = tencent_a_share_universe(universe_cache, workers=16, refresh=True)

    end_year = pd.Timestamp.now().year
    year_ranges = []
    y = start_year
    while y <= end_year:
        year_ranges.append((y, min(y + 2, end_year)))
        y += 3
    year_ranges.reverse()  # 从最新段往旧段拉取，便于新股提前终止

    totals: dict[str, list[int]] = {}
    records = list(zip(universe["secid"].astype(str), universe["code"].astype(str)))
    total_stocks = len(records)
    print(f"backfilling full-market breadth & limit history ({start_year}~{end_year}) for {total_stocks} stocks...")

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 32))) as pool:
        futs = {
            pool.submit(_fetch_stock_breadth_contrib, secid, code, year_ranges): secid
            for secid, code in records
        }
        for done, fut in enumerate(as_completed(futs), 1):
            contrib = fut.result()
            for d_str, (v, u, d, lu, ld) in contrib.items():
                acc = totals.get(d_str)
                if acc is None:
                    totals[d_str] = [v, u, d, lu, ld]
                else:
                    acc[0] += v
                    acc[1] += u
                    acc[2] += d
                    acc[3] += lu
                    acc[4] += ld
            if done % 500 == 0 or done == total_stocks:
                print(f"breadth backfill progress: {done}/{total_stocks}")

    rows = []
    for d_str in sorted(totals.keys()):
        v, u, d, lu, ld = totals[d_str]
        if v < 500:
            continue
        denom = u + d
        adv_ratio = (u / denom * 100.0) if denom > 0 else np.nan
        rows.append(
            {
                "date": d_str,
                "valid_stock_count": v,
                "up_count": u,
                "down_count": d,
                "advance_ratio_pct": round(adv_ratio, 4) if pd.notna(adv_ratio) else np.nan,
                "limit_up_count": lu,
                "limit_down_count": ld,
            }
        )
    df = pd.DataFrame(rows)
    df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    print(f"breadth history written: {output_csv}; days={len(df)} ({df['date'].min()}~{df['date'].max()})")
    return df


def calculate_fear_greed_history(
    index_df: pd.DataFrame,
    margin_df: pd.DataFrame,
    sentiment_daily_path: Path = DEFAULT_SENTIMENT_DAILY,
    display_start: str = DISPLAY_START_DATE,
    csi_all_df: pd.DataFrame | None = None,
    sentiment_intraday_path: Path = DEFAULT_SENTIMENT_INTRADAY,
    breadth_history_path: Path = DEFAULT_BREADTH_HISTORY_CACHE,
) -> tuple[pd.DataFrame, dict]:
    """按双支柱模型计算恐惧贪婪指数全历史序列及最新分项快照。"""
    # 1. 构建五宽基对齐表，计算价格压力（两次取中位数）及基础行情特征
    base_dates = (
        index_df.loc[index_df["secid"] == "sz399001", ["date", "close"]]
        .rename(columns={"close": "sz_close"})
        .sort_values("date")
        .drop_duplicates("date")
        .reset_index(drop=True)
    )
    frame = base_dates.copy()
    if csi_all_df is not None and not csi_all_df.empty:
        frame = frame.merge(csi_all_df[["date", "csi_all_close"]], on="date", how="left")
    else:
        frame["csi_all_close"] = np.nan

    pressure_cols = []
    vol_cols = []
    rel_vol_cols = []
    rsi_cols = []
    broad_details = {}

    for secid, code, name in BROAD_INDICES:
        sub = (
            index_df.loc[index_df["secid"] == secid, ["date", "close", "volume"]]
            .sort_values("date")
            .drop_duplicates("date")
            .copy()
        )
        sub["ret20"] = (sub["close"] / sub["close"].shift(20) - 1.0) * 100.0
        sub["dd60"] = (sub["close"] / sub["close"].rolling(60, min_periods=20).max() - 1.0) * 100.0
        sub["dd120"] = (sub["close"] / sub["close"].rolling(120, min_periods=40).max() - 1.0) * 100.0

        s_ret20 = rolling_weak_percentile(sub["ret20"], ROLLING_WINDOW, min_periods=120, ascending=True)
        s_dd60 = rolling_weak_percentile(sub["dd60"], ROLLING_WINDOW, min_periods=120, ascending=True)
        s_dd120 = rolling_weak_percentile(sub["dd120"], ROLLING_WINDOW, min_periods=120, ascending=True)
        # 第一次取中位数：单宽基三项价格压力的中位数
        sub[f"pressure_{code}"] = pd.concat([s_ret20, s_dd60, s_dd120], axis=1).median(axis=1)

        # 短期情绪所需特征：20日年化波动率、相对60日成交量、14日RSI
        sub[f"vol20_{code}"] = sub["close"].pct_change().rolling(20, min_periods=15).std() * np.sqrt(252) * 100.0
        sub[f"relvol_{code}"] = sub["volume"] / sub["volume"].rolling(60, min_periods=20).mean()
        sub[f"rsi14_{code}"] = compute_rsi(sub["close"], 14)

        pressure_cols.append(f"pressure_{code}")
        if code in ("399001", "000300"):
            vol_cols.append(f"vol20_{code}")
        rel_vol_cols.append(f"relvol_{code}")
        rsi_cols.append(f"rsi14_{code}")

        sub = sub.rename(
            columns={
                "close": f"close_{code}",
                "ret20": f"ret20_{code}",
                "dd60": f"dd60_{code}",
                "dd120": f"dd120_{code}",
            }
        )
        frame = frame.merge(
            sub[
                [
                    "date",
                    f"close_{code}",
                    f"ret20_{code}",
                    f"dd60_{code}",
                    f"dd120_{code}",
                    f"pressure_{code}",
                    f"vol20_{code}",
                    f"relvol_{code}",
                    f"rsi14_{code}",
                ]
            ],
            on="date",
            how="left",
        )

    # 第二次取中位数：五宽基综合价格压力
    frame["broad_pressure_score"] = frame[pressure_cols].median(axis=1)

    # 2. 短期市场情绪 6 项子指标（均转为 0-100 标准化分数）
    # (1) 波动率（反向：波动率越高越恐慌）
    frame["raw_vol20"] = frame[vol_cols].mean(axis=1)
    frame["vol_score"] = rolling_weak_percentile(frame["raw_vol20"], ROLLING_WINDOW, min_periods=120, ascending=False)

    # (2) 相对换手率 / 量能活跃度
    frame["raw_rel_vol"] = frame[rel_vol_cols].median(axis=1)
    frame["turnover_score"] = rolling_weak_percentile(frame["raw_rel_vol"], ROLLING_WINDOW, min_periods=120, ascending=True)

    # (3) 融资融券（沪深两市融资余额 20 日变动率）
    margin = margin_df[["date", "hs_margin_balance_yi"]].sort_values("date").drop_duplicates("date").copy()
    margin["margin_ret20"] = (margin["hs_margin_balance_yi"] / margin["hs_margin_balance_yi"].shift(20) - 1.0) * 100.0
    margin["margin_score"] = rolling_weak_percentile(margin["margin_ret20"], ROLLING_WINDOW, min_periods=120, ascending=True)
    frame = frame.merge(margin[["date", "hs_margin_balance_yi", "margin_ret20", "margin_score"]], on="date", how="left")
    # 融资数据通常 T+1 公布，最新交易日若暂缺则沿用前一交易日得分
    frame["margin_score"] = frame["margin_score"].ffill(limit=2)

    # (4) RSI（五宽基 14 日 RSI 中位数）
    frame["raw_rsi14"] = frame[rsi_cols].median(axis=1)
    frame["rsi_score"] = rolling_weak_percentile(frame["raw_rsi14"], ROLLING_WINDOW, min_periods=120, ascending=True)

    # (5) 市场宽度 & (6) 涨跌停比：正式日频优先 -> 盘中快照兜底当日 -> 全市场长历史回溯补齐早期
    frame["breadth_score"] = np.nan
    frame["limit_score"] = np.nan
    sentiment_frames = []
    for src_path in (sentiment_daily_path, sentiment_intraday_path, breadth_history_path):
        if src_path and src_path.exists():
            try:
                part = pd.read_csv(src_path, parse_dates=["date"])
                part["date"] = part["date"].dt.normalize()
                keep = [c for c in ("date", "advance_ratio_pct", "limit_up_count", "limit_down_count") if c in part.columns]
                if "date" in keep and len(keep) > 1:
                    sentiment_frames.append(part[keep])
            except Exception:
                pass
    if sentiment_frames:
        # 正式日频数据优先；当日尚未入库时使用盘中/最新快照补齐
        daily = (
            pd.concat(sentiment_frames, ignore_index=True)
            .drop_duplicates("date", keep="first")
            .sort_values("date")
            .reset_index(drop=True)
        )
        if "advance_ratio_pct" in daily.columns:
            daily["fg_breadth_score"] = rolling_weak_percentile(
                pd.to_numeric(daily["advance_ratio_pct"], errors="coerce"),
                window=252,
                min_periods=20,
                ascending=True,
            )
        if {"limit_up_count", "limit_down_count"}.issubset(daily.columns):
            up = pd.to_numeric(daily["limit_up_count"], errors="coerce")
            down = pd.to_numeric(daily["limit_down_count"], errors="coerce")
            ratio = (up - down) / (up + down + 1.0)
            daily["fg_limit_score"] = rolling_weak_percentile(
                ratio, window=252, min_periods=20, ascending=True
            )
        merge_cols = ["date"]
        if "fg_breadth_score" in daily.columns:
            merge_cols.append("fg_breadth_score")
        if "fg_limit_score" in daily.columns:
            merge_cols.append("fg_limit_score")
        if len(merge_cols) > 1:
            frame = frame.merge(daily[merge_cols], on="date", how="left")
            if "fg_breadth_score" in frame.columns:
                frame["breadth_score"] = frame["fg_breadth_score"]
            if "fg_limit_score" in frame.columns:
                frame["limit_score"] = frame["fg_limit_score"]

    # 短期情绪按当日可用分项权重加权归一化（缺失项不按零分处理）
    weighted_sum = pd.Series(0.0, index=frame.index)
    weight_used = pd.Series(0.0, index=frame.index)
    for col, _, weight, _ in SHORT_COMPONENTS:
        valid = frame[col].notna()
        weighted_sum = weighted_sum + frame[col].fillna(0.0) * weight
        weight_used = weight_used + valid.astype(float) * weight
    frame["short_sentiment_score"] = np.where(weight_used > 0, weighted_sum / weight_used, np.nan)
    frame["short_weight_coverage_pct"] = weight_used

    # 3. 合成主指数与近 10 年历史分位
    frame["fear_greed_index"] = (
        0.5 * frame["short_sentiment_score"] + 0.5 * frame["broad_pressure_score"]
    )
    frame = frame[frame["date"] >= pd.Timestamp(display_start)].dropna(
        subset=["fear_greed_index"]
    ).reset_index(drop=True)

    frame["history_pct_10y"] = rolling_weak_percentile(
        frame["fear_greed_index"],
        window=PERCENTILE_10Y_WINDOW,
        min_periods=60,
        ascending=True,
    )

    latest = frame.iloc[-1]
    for _, code, name in BROAD_INDICES:
        broad_details[code] = {
            "code": code,
            "name": name,
            "score": round(float(latest[f"pressure_{code}"]), 2),
            "ret20": round(float(latest[f"ret20_{code}"]), 2),
            "dd60": round(float(latest[f"dd60_{code}"]), 2),
            "dd120": round(float(latest[f"dd120_{code}"]), 2),
        }

    meta = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "latest_date": latest["date"].strftime("%Y-%m-%d"),
        "first_date": frame["date"].iloc[0].strftime("%Y-%m-%d"),
        "total_days": int(len(frame)),
        "fear_greed_index": round(float(latest["fear_greed_index"]), 2),
        "short_sentiment_score": round(float(latest["short_sentiment_score"]), 2),
        "broad_pressure_score": round(float(latest["broad_pressure_score"]), 2),
        "history_pct_10y": round(float(latest["history_pct_10y"]), 1),
        "short_weight_coverage_pct": round(float(latest["short_weight_coverage_pct"]), 0),
        "broad_details": broad_details,
    }
    return frame, meta


def save_history_csv(frame: pd.DataFrame, output_csv: Path = DEFAULT_HISTORY_CSV) -> None:
    """导出 2016 年至今完整恐惧贪婪指数历史 CSV。"""
    export_cols = [
        "date",
        "fear_greed_index",
        "short_sentiment_score",
        "broad_pressure_score",
        "history_pct_10y",
        "csi_all_close",
        "sz_close",
        "vol_score",
        "turnover_score",
        "margin_score",
        "breadth_score",
        "rsi_score",
        "limit_score",
        "pressure_399001",
        "pressure_000300",
        "pressure_000905",
        "pressure_000852",
        "pressure_399006",
    ]
    out = frame[[c for c in export_cols if c in frame.columns]].copy()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    for col in out.columns:
        if col != "date":
            out[col] = out[col].round(2)
    out.to_csv(output_csv, index=False, encoding="utf-8-sig")


FEAR_GREED_SCRIPT = r"""<script>
(function(){
  var dataNode=document.getElementById("fg-data");
  if(!dataNode)return;
  var payload=null;
  try{payload=JSON.parse(dataNode.textContent||"null")}catch(e){return}
  if(!payload||!Array.isArray(payload.dates)||payload.dates.length<2)return;

  var dates=payload.dates;
  var total=dates.length,maxIndex=total-1;
  var SVG_NS="http://www.w3.org/2000/svg";
  var tip=document.getElementById("fg-popover");

  function clamp(v,lo,hi){return Math.max(lo,Math.min(hi,v))}
  function fmt(v,d){
    if(v===null||v===undefined||isNaN(v))return "--";
    return Number(v).toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d});
  }
  function moodLabel(score){
    if(score===null||score===undefined||isNaN(score))return "--";
    if(score<=25)return "极度恐慌";
    if(score<=45)return "恐慌";
    if(score<=55)return "中性";
    if(score<=75)return "贪婪";
    return "极度贪婪";
  }
  function moodColor(score){
    if(score===null||score===undefined||isNaN(score))return "#64748b";
    if(score<=25)return "#10b981";
    if(score<=45)return "#65a30d";
    if(score<=55)return "#ca8a04";
    if(score<=75)return "#f97316";
    return "#c81e1e";
  }

  function showTip(title,dateText,rows,clientX,clientY){
    if(!tip)return;
    var head=tip.querySelector("[data-tip-title]");
    var dateEl=tip.querySelector("[data-tip-date]");
    var list=tip.querySelector("[data-tip-rows]");
    if(head)head.textContent=title;
    if(dateEl)dateEl.textContent=dateText;
    if(list){
      list.textContent="";
      rows.forEach(function(r){
        var item=document.createElement("div");
        item.className="fg-tip-row";
        var left=document.createElement("span");
        var dot=document.createElement("i");
        dot.style.background=r.color;
        left.appendChild(dot);
        left.appendChild(document.createTextNode(r.name));
        var right=document.createElement("b");
        right.textContent=r.value;
        if(r.valueColor)right.style.color=r.valueColor;
        item.appendChild(left);
        item.appendChild(right);
        list.appendChild(item);
      });
    }
    tip.hidden=false;
    var rect=tip.getBoundingClientRect();
    var leftPos=clientX+16;
    if(leftPos+rect.width>window.innerWidth-8)leftPos=clientX-rect.width-16;
    leftPos=Math.max(8,Math.min(window.innerWidth-rect.width-8,leftPos));
    var topPos=clientY-rect.height-14;
    if(topPos<8)topPos=Math.min(window.innerHeight-rect.height-8,clientY+16);
    tip.style.left=leftPos+"px";
    tip.style.top=Math.max(8,topPos)+"px";
  }

  function hideTip(){if(tip)tip.hidden=true}

  // 通用交互式多曲线 SVG 图表引擎（支持区间切换、Ctrl+滚轮缩放、左键拖动、鼠标实时悬浮）
  function createInteractiveChart(cfg){
    var svg=document.getElementById(cfg.svgId);
    if(!svg)return null;
    var W=1080,H=cfg.height||380,L=cfg.leftPad||56,R=cfg.rightPad||62,T=24,B=42;
    var PW=W-L-R,PH=H-T-B;
    var minSpan=Math.min(20,maxIndex);
    var viewStart=Math.max(0,maxIndex-(cfg.defaultDays||240));
    var viewEnd=maxIndex;
    if(cfg.defaultDays==="all")viewStart=0;

    var crosshair=svg.querySelector("[data-crosshair]");
    var datesGroup=svg.querySelector("[data-dates]");
    var rMaxEl=svg.querySelector("[data-rmax]");
    var r75El=svg.querySelector("[data-r75]");
    var rMidEl=svg.querySelector("[data-rmid]");
    var r25El=svg.querySelector("[data-r25]");
    var rMinEl=svg.querySelector("[data-rmin]");
    var section=svg.closest("section");
    var rangeEl=section?section.querySelector("[data-chart-range]"):null;
    var resetBtn=section?section.querySelector("[data-chart-reset]"):null;
    var rangeButtons=section?section.querySelectorAll("[data-range-days]"):[];
    var toggles=section?section.querySelectorAll("[data-series-toggle]"):[];

    var visible={};
    cfg.series.forEach(function(s){visible[s.key]=s.defaultVisible!==false});

    function xFor(idx){return L+((idx-viewStart)/Math.max(viewEnd-viewStart,1e-9))*PW}
    function yForScore(v){return T+((100-v)/100)*PH}

    var rightMin=0,rightMax=1;
    function yForRight(v){return T+((rightMax-v)/Math.max(rightMax-rightMin,1e-9))*PH}

    function buildPath(arr,i0,i1,yFn){
      var cmds=[];
      for(var i=i0;i<=i1;i++){
        var v=arr[i];
        if(v===null||v===undefined||isNaN(v))continue;
        cmds.push((cmds.length?"L":"M")+xFor(i).toFixed(1)+","+yFn(v).toFixed(1));
      }
      return cmds.join(" ");
    }

    function buildAreaPath(arr,i0,i1,yFn){
      var cmds=[],firstX=null,lastX=null,yBase=(T+PH).toFixed(1);
      for(var i=i0;i<=i1;i++){
        var v=arr[i];
        if(v===null||v===undefined||isNaN(v))continue;
        var x=xFor(i).toFixed(1),y=yFn(v).toFixed(1);
        if(firstX===null)firstX=x;
        lastX=x;
        cmds.push((cmds.length?"L":"M")+x+","+y);
      }
      if(!cmds.length)return "";
      return cmds.join(" ")+" L"+lastX+","+yBase+" L"+firstX+","+yBase+" Z";
    }

    function syncRangeButtons(){
      var span=Math.round(viewEnd-viewStart);
      rangeButtons.forEach(function(btn){
        var target=btn.getAttribute("data-range-days");
        var active=(target==="all"&&viewStart<=0.5&&viewEnd>=maxIndex-0.5)
          ||(target!=="all"&&Math.abs(span-Number(target))<=3&&viewEnd>=maxIndex-1);
        btn.setAttribute("aria-pressed",String(active));
      });
    }

    function render(){
      var i0=Math.max(0,Math.floor(viewStart));
      var i1=Math.min(maxIndex,Math.ceil(viewEnd));

      if(cfg.rightSeries){
        var lo=Infinity,hi=-Infinity,rArr=cfg.rightSeries.values;
        for(var i=i0;i<=i1;i++){
          var rv=rArr[i];
          if(rv!==null&&rv!==undefined&&!isNaN(rv)){if(rv<lo)lo=rv;if(rv>hi)hi=rv}
        }
        if(!isFinite(lo)||!isFinite(hi)){lo=0;hi=1}
        var pad=Math.max((hi-lo)*0.08,hi*0.005);
        rightMin=Math.max(0,lo-pad);
        rightMax=hi+pad;
        if(rMaxEl)rMaxEl.textContent=Math.round(rightMax).toLocaleString("zh-CN");
        if(r75El)r75El.textContent=Math.round(rightMin+(rightMax-rightMin)*0.75).toLocaleString("zh-CN");
        if(rMidEl)rMidEl.textContent=Math.round((rightMin+rightMax)/2).toLocaleString("zh-CN");
        if(r25El)r25El.textContent=Math.round(rightMin+(rightMax-rightMin)*0.25).toLocaleString("zh-CN");
        if(rMinEl)rMinEl.textContent=Math.round(rightMin).toLocaleString("zh-CN");
      }

      cfg.series.forEach(function(s){
        var pathEl=svg.querySelector('[data-line="'+s.key+'"]');
        var areaEl=svg.querySelector('[data-area="'+s.key+'"]');
        if(!visible[s.key]){
          if(pathEl)pathEl.setAttribute("d","");
          if(areaEl)areaEl.setAttribute("d","");
          return;
        }
        var yFn=s.axis==="right"?yForRight:yForScore;
        if(pathEl)pathEl.setAttribute("d",buildPath(s.values,i0,i1,yFn));
        if(areaEl)areaEl.setAttribute("d",buildAreaPath(s.values,i0,i1,yFn));
      });

      if(datesGroup){
        datesGroup.textContent="";
        var span=Math.max(i1-i0,1);
        var count=Math.min(7,span+1);
        var seen={};
        for(var k=0;k<count;k++){
          var idx=Math.round(i0+k*span/Math.max(count-1,1));
          if(seen[idx])continue;
          seen[idx]=true;
          var txt=document.createElementNS(SVG_NS,"text");
          txt.setAttribute("x",xFor(idx).toFixed(1));
          txt.setAttribute("y",String(H-14));
          txt.setAttribute("text-anchor",k===0?"start":(k===count-1?"end":"middle"));
          txt.textContent=dates[idx];
          datesGroup.appendChild(txt);
        }
      }

      var isFull=viewStart<=0.5&&viewEnd>=maxIndex-0.5;
      svg.classList.toggle("is-zoomed",!isFull);
      if(resetBtn)resetBtn.hidden=isFull;
      if(rangeEl){
        var ratio=maxIndex/Math.max(viewEnd-viewStart,1);
        rangeEl.textContent=dates[i0]+" ~ "+dates[i1]+(!isFull?" ("+ratio.toFixed(1)+"×)":"");
      }
      syncRangeButtons();
    }

    function clientToSvgX(clientX){
      var rect=svg.getBoundingClientRect();
      return rect.width?((clientX-rect.left)/rect.width)*W:L;
    }

    function updateHover(clientX,clientY){
      var svgX=clientToSvgX(clientX);
      if(svgX<L||svgX>W-R){
        if(crosshair)crosshair.setAttribute("hidden","");
        hideTip();
        return;
      }
      var ratio=clamp((svgX-L)/PW,0,1);
      var idx=clamp(Math.round(viewStart+ratio*(viewEnd-viewStart)),0,maxIndex);
      if(crosshair){
        var x=xFor(idx).toFixed(1);
        crosshair.setAttribute("x1",x);
        crosshair.setAttribute("x2",x);
        crosshair.removeAttribute("hidden");
      }
      var rows=[];
      cfg.series.forEach(function(s){
        if(!visible[s.key])return;
        var val=s.values[idx];
        var formatted=s.axis==="right"
          ? fmt(val,2)+" 点"
          : (s.withMood? fmt(val,2)+" 分 ("+moodLabel(val)+")" : fmt(val,2)+" 分");
        var dotColor=s.dynamicMoodColor?moodColor(val):s.color;
        var valColor=s.dynamicMoodColor?moodColor(val):null;
        rows.push({name:s.name,color:dotColor,value:formatted,valueColor:valColor});
      });
      showTip(cfg.title,dates[idx],rows,clientX,clientY);
    }

    svg.addEventListener("wheel",function(event){
      if(!event.ctrlKey)return;
      event.preventDefault();
      var svgX=clientToSvgX(event.clientX);
      var ratio=clamp((svgX-L)/PW,0,1);
      var span=viewEnd-viewStart;
      var newSpan=clamp(span*(event.deltaY<0?0.8:1.25),minSpan,maxIndex);
      if(Math.abs(newSpan-span)<1e-6)return;
      var anchor=viewStart+ratio*span;
      viewStart=clamp(anchor-ratio*newSpan,0,maxIndex-newSpan);
      viewEnd=viewStart+newSpan;
      render();
      updateHover(event.clientX,event.clientY);
    },{passive:false});

    var dragging=false,dragX=0,originStart=0,originEnd=0,moved=false;
    svg.addEventListener("mousedown",function(event){
      if(event.button!==0)return;
      dragging=true;moved=false;dragX=event.clientX;originStart=viewStart;originEnd=viewEnd;
      event.preventDefault();
    });
    svg.addEventListener("mousemove",function(event){
      if(dragging)return;
      updateHover(event.clientX,event.clientY);
    });
    svg.addEventListener("mouseleave",function(){
      if(dragging)return;
      if(crosshair)crosshair.setAttribute("hidden","");
      hideTip();
    });
    window.addEventListener("mousemove",function(event){
      if(!dragging)return;
      var dx=event.clientX-dragX;
      if(!moved&&Math.abs(dx)>3){
        moved=true;
        svg.classList.add("is-dragging");
        if(crosshair)crosshair.setAttribute("hidden","");
        hideTip();
      }
      if(!moved)return;
      var rect=svg.getBoundingClientRect();
      var plotPx=rect.width*(PW/W);
      var span=originEnd-originStart;
      if(plotPx<=0||span>=maxIndex-1e-6)return;
      viewStart=clamp(originStart-(dx/plotPx)*span,0,maxIndex-span);
      viewEnd=viewStart+span;
      render();
    });
    window.addEventListener("mouseup",function(event){
      if(!dragging)return;
      dragging=false;
      svg.classList.remove("is-dragging");
      if(moved){
        var rect=svg.getBoundingClientRect();
        if(event.clientX>=rect.left&&event.clientX<=rect.right&&event.clientY>=rect.top&&event.clientY<=rect.bottom){
          updateHover(event.clientX,event.clientY);
        }
      }
    });
    svg.addEventListener("dblclick",function(event){
      event.preventDefault();
      viewStart=0;viewEnd=maxIndex;render();
    });
    if(resetBtn)resetBtn.addEventListener("click",function(){viewStart=0;viewEnd=maxIndex;render()});

    rangeButtons.forEach(function(btn){
      btn.addEventListener("click",function(){
        var days=btn.getAttribute("data-range-days");
        if(days==="all"){viewStart=0;viewEnd=maxIndex}
        else{viewEnd=maxIndex;viewStart=Math.max(0,maxIndex-Number(days))}
        render();
      });
    });

    toggles.forEach(function(btn){
      btn.addEventListener("click",function(){
        var key=btn.getAttribute("data-series-toggle");
        visible[key]=!visible[key];
        btn.setAttribute("aria-pressed",String(visible[key]));
        render();
      });
    });

    render();
    return {render:render};
  }

  // 1. 主历史走势图
  createInteractiveChart({
    svgId:"fg-main-svg",
    title:"A股恐惧贪婪指数",
    height:420,
    leftPad:92,
    rightPad:106,
    defaultDays:240,
    rightSeries:{values:payload.csi_all_close},
    series:[
      {key:"fg",name:"恐惧贪婪指数",color:"#10b981",values:payload.fg,withMood:true,dynamicMoodColor:true,defaultVisible:true},
      {key:"short",name:"短期市场情绪",color:"#ec8b00",values:payload.short,withMood:true,defaultVisible:false},
      {key:"pressure",name:"五宽基价格压力",color:"#7c3aed",values:payload.pressure,defaultVisible:false},
      {key:"csi_all",name:"中证全指（收盘点位）",color:"#3b82f6",values:payload.csi_all_close,axis:"right",defaultVisible:true}
    ]
  });

  // 2. 五宽基价格压力走势图
  createInteractiveChart({
    svgId:"fg-broad-svg",
    title:"五宽基价格压力分数",
    height:320,
    rightPad:22,
    defaultDays:720,
    series:[
      {key:"p_all",name:"五宽基综合压力",color:"#182b49",values:payload.pressure,defaultVisible:true},
      {key:"p_399001",name:"深证成指",color:"#2563eb",values:payload.p_399001,defaultVisible:true},
      {key:"p_000300",name:"沪深300",color:"#0d9488",values:payload.p_000300,defaultVisible:true},
      {key:"p_000905",name:"中证500",color:"#d97706",values:payload.p_000905,defaultVisible:true},
      {key:"p_000852",name:"中证1000",color:"#dc2626",values:payload.p_000852,defaultVisible:true},
      {key:"p_399006",name:"创业板指",color:"#7c3aed",values:payload.p_399006,defaultVisible:true}
    ]
  });

  // 3. 短期情绪六子指标走势图
  createInteractiveChart({
    svgId:"fg-short-svg",
    title:"短期情绪六子指标（0-100分）",
    height:320,
    rightPad:22,
    defaultDays:720,
    series:[
      {key:"vol",name:"波动率 (25%)",color:"#dc2626",values:payload.vol,defaultVisible:true},
      {key:"turnover",name:"相对换手率 (20%)",color:"#2563eb",values:payload.turnover,defaultVisible:true},
      {key:"margin",name:"融资融券 (20%)",color:"#d97706",values:payload.margin,defaultVisible:true},
      {key:"breadth",name:"市场宽度 (15%)",color:"#16a34a",values:payload.breadth,defaultVisible:true},
      {key:"rsi",name:"RSI (10%)",color:"#7c3aed",values:payload.rsi,defaultVisible:true},
      {key:"limit",name:"涨跌停比 (10%)",color:"#0891b2",values:payload.limit,defaultVisible:true}
    ]
  });

  // 前端一键导出完整 CSV
  var csvBtn=document.getElementById("fg-download-csv");
  if(csvBtn){
    csvBtn.addEventListener("click",function(){
      var lines=["date,fear_greed_index,short_sentiment_score,broad_pressure_score,csi_all_close"];
      for(var i=0;i<total;i++){
        lines.push([
          dates[i],
          payload.fg[i]===null?"":payload.fg[i],
          payload.short[i]===null?"":payload.short[i],
          payload.pressure[i]===null?"":payload.pressure[i],
          payload.csi_all_close[i]===null?"":payload.csi_all_close[i]
        ].join(","));
      }
      var blob=new Blob(["\ufeff"+lines.join("\n")],{type:"text/csv;charset=utf-8;"});
      var url=URL.createObjectURL(blob);
      var a=document.createElement("a");
      a.href=url;
      a.download="a-share-fear-greed-history.csv";
      a.click();
      setTimeout(function(){URL.revokeObjectURL(url)},1000);
    });
  }
})();
</script>"""


def _series_to_json_list(series: pd.Series) -> list:
    return [round(float(v), 2) if pd.notna(v) else None for v in series]


def render_fear_greed_page(
    frame: pd.DataFrame,
    meta: dict,
    output_html: Path = DEFAULT_OUTPUT_HTML,
) -> None:
    """渲染独立单文件 A股恐惧贪婪指数 HTML。"""
    fg_label, fg_cls, _ = mood_meta(meta["fear_greed_index"])
    short_label, short_cls, _ = mood_meta(meta["short_sentiment_score"])
    pressure_label, pressure_cls, _ = mood_meta(meta["broad_pressure_score"])

    # 五宽基分项卡片
    broad_cards = []
    for _, code, name in BROAD_INDICES:
        d = meta["broad_details"][code]
        lbl, cls, _ = mood_meta(d["score"])
        broad_cards.append(
            f'<article class="fg-subcard">'
            f'<div class="fg-sub-head"><b>{html.escape(name)}</b><small>{code}</small></div>'
            f'<div class="fg-sub-val {cls}">{d["score"]:.2f}<span>{lbl}</span></div>'
            f'<div class="fg-sub-metrics">'
            f'<span>20日收益 <b>{d["ret20"]:+.2f}%</b></span>'
            f'<span>60日回撤 <b>{d["dd60"]:+.2f}%</b></span>'
            f'<span>120日回撤 <b>{d["dd120"]:+.2f}%</b></span>'
            f'</div></article>'
        )

    # 短期情绪六子指标卡片
    latest = frame.iloc[-1]
    short_cards = []
    for col, title, weight, desc in SHORT_COMPONENTS:
        val = latest[col]
        lbl, cls, _ = mood_meta(val)
        val_text = f"{float(val):.2f}" if pd.notna(val) else "暂无"
        bar_w = max(0.0, min(100.0, float(val))) if pd.notna(val) else 0.0
        short_cards.append(
            f'<article class="fg-subcard">'
            f'<div class="fg-sub-head"><b>{html.escape(title)}</b><small>权重 {weight}%</small></div>'
            f'<div class="fg-sub-val {cls}">{val_text}<span>{lbl}</span></div>'
            f'<div class="fg-mini-bar"><i style="width:{bar_w:.1f}%"></i></div>'
            f'<small class="fg-sub-desc">{html.escape(desc)}</small>'
            f'</article>'
        )

    # 最近 20 个交易日明细表
    recent_rows = []
    for _, row in frame.tail(20).iloc[::-1].iterrows():
        lbl, cls, _ = mood_meta(row["fear_greed_index"])
        csi_text = f"{row['csi_all_close']:,.2f}" if pd.notna(row.get("csi_all_close")) else "--"
        recent_rows.append(
            f'<tr>'
            f'<td>{row["date"]:%Y-%m-%d}</td>'
            f'<td class="num"><strong class="{cls}">{row["fear_greed_index"]:.2f}</strong></td>'
            f'<td><span class="fg-badge {cls}">{lbl}</span></td>'
            f'<td class="num">{row["short_sentiment_score"]:.2f}</td>'
            f'<td class="num">{row["broad_pressure_score"]:.2f}</td>'
            f'<td class="num">{row["history_pct_10y"]:.1f}%</td>'
            f'<td class="num">{csi_text}</td>'
            f'</tr>'
        )

    payload_json = json.dumps(
        {
            "dates": [d.strftime("%Y-%m-%d") for d in frame["date"]],
            "fg": _series_to_json_list(frame["fear_greed_index"]),
            "short": _series_to_json_list(frame["short_sentiment_score"]),
            "pressure": _series_to_json_list(frame["broad_pressure_score"]),
            "csi_all_close": _series_to_json_list(frame["csi_all_close"]),
            "sz_close": _series_to_json_list(frame["sz_close"]),
            "p_399001": _series_to_json_list(frame["pressure_399001"]),
            "p_000300": _series_to_json_list(frame["pressure_000300"]),
            "p_000905": _series_to_json_list(frame["pressure_000905"]),
            "p_000852": _series_to_json_list(frame["pressure_000852"]),
            "p_399006": _series_to_json_list(frame["pressure_399006"]),
            "vol": _series_to_json_list(frame["vol_score"]),
            "turnover": _series_to_json_list(frame["turnover_score"]),
            "margin": _series_to_json_list(frame["margin_score"]),
            "breadth": _series_to_json_list(frame["breadth_score"]),
            "rsi": _series_to_json_list(frame["rsi_score"]),
            "limit": _series_to_json_list(frame["limit_score"]),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    pct10 = meta["history_pct_10y"]
    doc = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>A股恐惧贪婪指数</title>
<link rel="stylesheet" href="/assets/market-site.css">
<style>
body{{margin:0;background:#f6f8fb;color:#182b49;font:14px -apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif}}
main{{max-width:1140px;margin:auto;padding:24px 20px 56px}}
h1{{margin:0;font-size:26px}}h2{{font-size:17px;margin:0}}
.sub,.note,small{{color:#68778d}}
.fg-top-bar{{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:10px}}
.fg-back-link{{color:#2869b2;text-decoration:none;font-weight:600;font-size:13px}}
.fg-back-link:hover{{text-decoration:underline}}
.fg-kpis{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:16px 0}}
.fg-card,section{{background:#fff;border:1px solid #e6edf5;border-radius:12px;padding:18px;box-sizing:border-box}}
section{{margin-top:16px}}
.fg-card .val{{font-size:28px;font-weight:750;margin:6px 0;font-variant-numeric:tabular-nums}}
.fg-badge{{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700;background:#f1f5f9}}
.mood-extreme-fear{{color:#087f5b}}.fg-badge.mood-extreme-fear{{background:#e6f7f1}}
.mood-fear{{color:#2b8a57}}.fg-badge.mood-fear{{background:#edf8f2}}
.mood-neutral{{color:#b47b00}}.fg-badge.mood-neutral{{background:#fff7e0}}
.mood-greed{{color:#d9534f}}.fg-badge.mood-greed{{background:#ffefef}}
.mood-extreme-greed{{color:#b42318}}.fg-badge.mood-extreme-greed{{background:#fde8e8}}
.tone-missing{{color:#8a96a8}}
.fg-pct-box{{background:#fff;border:1px solid #e6edf5;border-radius:12px;padding:14px 18px;margin-bottom:16px}}
.fg-pct-head{{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:8px;font-size:13px}}
.fg-pct-track{{position:relative;height:10px;border-radius:999px;background:linear-gradient(90deg,#087f5b 0%,#2b8a57 25%,#f4c95d 50%,#df655f 75%,#b42318 100%);opacity:.85}}
.fg-pct-thumb{{position:absolute;top:-4px;width:18px;height:18px;margin-left:-9px;border-radius:50%;background:#182b49;border:3px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,.28)}}
.fg-section-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}}
.fg-toolbar{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.fg-btn-group{{display:inline-flex;border:1px solid #d9e3f0;border-radius:8px;overflow:hidden;background:#f8fafd}}
.fg-btn-group button{{appearance:none;border:0;background:transparent;padding:5px 10px;font:inherit;font-size:12px;color:#475569;cursor:pointer;font-weight:600}}
.fg-btn-group button[aria-pressed="true"]{{background:#1d4ed8;color:#fff}}
.fg-btn-group button:hover:not([aria-pressed="true"]){{background:#eaf0f8}}
.fg-chip{{appearance:none;border:1px solid #d9e3f0;border-radius:999px;padding:4px 10px;background:#fff;font:inherit;font-size:12px;color:#334155;cursor:pointer;display:inline-flex;align-items:center;gap:6px}}
.fg-chip i{{width:9px;height:9px;border-radius:50%;display:inline-block}}
.fg-chip[aria-pressed="false"]{{opacity:.45;text-decoration:line-through}}
.fg-action-btn{{appearance:none;border:1px solid #cdd9f0;border-radius:7px;padding:5px 11px;background:#f5f8ff;color:#1d4ed8;font:inherit;font-size:12px;font-weight:600;cursor:pointer}}
.fg-action-btn:hover{{background:#e8f0fe}}
.fg-range-text{{font-size:12px;color:#68778d;font-variant-numeric:tabular-nums}}
.fg-svg{{width:100%;display:block;user-select:none;-webkit-user-select:none;cursor:crosshair}}
.fg-svg.is-zoomed{{cursor:grab}}.fg-svg.is-dragging{{cursor:grabbing}}
.fg-svg text{{font-size:11px;fill:#68778d}}
.fg-axis{{stroke:#e2e8f0;stroke-width:1}}
.fg-threshold-line{{stroke:#94a3b8;stroke-width:1;stroke-dasharray:4 3;opacity:.75}}
.fg-threshold-label{{font-size:11px;fill:#64748b}}
.fg-sz-axis-line{{stroke:#93c5fd;stroke-width:1.4}}
.fg-sz-axis-text{{fill:#3b82f6!important;font-variant-numeric:tabular-nums}}
.fg-line{{fill:none;stroke-width:2.1;stroke-linecap:round;stroke-linejoin:round}}
.fg-line-main{{stroke-width:2.4}}
.fg-line-sz{{fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;opacity:.92}}
.fg-legend-bands{{display:inline-flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:6px;font-size:12px;color:#475569}}
.fg-legend-bands span{{display:inline-flex;align-items:center;gap:4px}}
.fg-legend-bands i{{width:10px;height:10px;border-radius:3px;display:inline-block}}
.fg-crosshair{{stroke:#64748b;stroke-width:1;stroke-dasharray:4 3;pointer-events:none}}
.fg-band-ef{{fill:#087f5b;opacity:.12}}.fg-band-f{{fill:#3b9b63;opacity:.09}}.fg-band-n{{fill:#f4c95d;opacity:.16}}.fg-band-g{{fill:#df655f;opacity:.09}}.fg-band-eg{{fill:#b42318;opacity:.13}}
.fg-subgrid-5{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-bottom:14px}}
.fg-subgrid-6{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:14px}}
.fg-subcard{{border:1px solid #e6edf5;border-radius:10px;padding:12px 13px;background:#fbfcfe}}
.fg-sub-head{{display:flex;justify-content:space-between;align-items:baseline;gap:6px}}
.fg-sub-val{{font-size:22px;font-weight:750;margin:6px 0;font-variant-numeric:tabular-nums;display:flex;align-items:baseline;gap:6px}}
.fg-sub-val span{{font-size:12px;font-weight:600}}
.fg-sub-metrics{{display:flex;flex-direction:column;gap:3px;font-size:11px;color:#68778d;font-variant-numeric:tabular-nums}}
.fg-sub-metrics b{{color:#1e293b}}
.fg-mini-bar{{height:6px;background:#e9eff6;border-radius:999px;overflow:hidden;margin:6px 0 7px}}
.fg-mini-bar i{{display:block;height:100%;background:#2869b2;border-radius:999px}}
.fg-sub-desc{{display:block;font-size:11px;line-height:1.45}}
details.fg-method{{background:#fff;border:1px solid #e6edf5;border-radius:12px;padding:14px 18px;margin-top:16px}}
details.fg-method summary{{cursor:pointer;font-weight:700;font-size:15px;color:#182b49}}
.fg-method-body{{margin-top:12px;line-height:1.7;font-size:13px;color:#334155}}
.fg-method-body ul{{margin:6px 0 10px 18px;padding:0}}
.fg-score-guide{{margin-top:14px;padding:16px 18px;border:1px solid #e6edf5;border-radius:10px;background:#fcfdff}}
.fg-score-guide h3{{margin:0 0 12px;font-size:15px;color:#182b49}}
.fg-score-strip{{display:grid;grid-template-columns:repeat(5,1fr);border-radius:8px;overflow:hidden;margin-bottom:12px;text-align:center}}
.fg-score-seg{{padding:10px 6px;display:flex;flex-direction:column;gap:2px;line-height:1.3}}
.fg-score-seg b{{font-size:13px}}.fg-score-seg span{{font-size:12px;font-weight:700;font-variant-numeric:tabular-nums}}
.fg-seg-ef{{background:#00a846;color:#fff}}.fg-seg-f{{background:#84cc00;color:#1f2937}}.fg-seg-n{{background:#ffcc00;color:#1f2937}}.fg-seg-g{{background:#f97300;color:#fff}}.fg-seg-eg{{background:#cc0018;color:#fff}}
.fg-score-guide p{{margin:6px 0 0;font-size:12px;line-height:1.65;color:#475569}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:9px 8px;border-bottom:1px solid #edf1f6;text-align:left}}
th{{color:#68778d;font-weight:500}}td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums}}
.fg-popover{{position:fixed;z-index:300;min-width:210px;max-width:290px;padding:11px 13px;border:1px solid #d5e0ee;border-radius:10px;background:rgba(255,255,255,.97);box-shadow:0 10px 28px rgba(15,23,42,.18);pointer-events:none;font-size:12px}}
.fg-popover[hidden]{{display:none}}
.fg-popover-head{{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:6px;padding-bottom:5px;border-bottom:1px solid #edf1f6}}
.fg-tip-row{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:4px 0;font-variant-numeric:tabular-nums}}
.fg-tip-row span{{display:inline-flex;align-items:center;gap:6px;color:#475569}}
.fg-tip-row i{{width:8px;height:8px;border-radius:50%;display:inline-block}}
.fg-note{{font-size:12px;color:#68778d;line-height:1.6;margin:10px 0 0}}
@media(max-width:960px){{.fg-kpis{{grid-template-columns:repeat(2,1fr)}}.fg-subgrid-5{{grid-template-columns:repeat(2,1fr)}}.fg-subgrid-6{{grid-template-columns:repeat(2,1fr)}}}}
@media(max-width:600px){{.fg-kpis,.fg-subgrid-5,.fg-subgrid-6{{grid-template-columns:1fr}}}}
</style>
<body><main>
<!-- SITE_NAVIGATION -->
<div class="fg-top-bar">
  <div><h1>A股恐惧贪婪指数</h1><p class="sub" style="margin:4px 0 0">双支柱合成模型（短期市场情绪 50% + 五宽基价格压力 50%）· 统一历史自 {meta["first_date"]} 起（共 {meta["total_days"]:,} 个交易日）</p></div>
  <a class="fg-back-link" href="/">← 返回市场情绪仪表盘</a>
</div>

<div class="fg-kpis">
  <div class="fg-card"><small>最新正式交易日</small><div class="val" style="font-size:24px">{meta["latest_date"]}</div><small>更新于 {meta["updated_at"]}</small></div>
  <div class="fg-card"><small>恐惧贪婪指数</small><div class="val {fg_cls}">{meta["fear_greed_index"]:.2f}</div><span class="fg-badge {fg_cls}">{fg_label}</span></div>
  <div class="fg-card"><small>短期市场情绪（权重 50%）</small><div class="val {short_cls}">{meta["short_sentiment_score"]:.2f}</div><span class="fg-badge {short_cls}">{short_label}</span> · <small>覆盖权重 {meta["short_weight_coverage_pct"]:.0f}%</small></div>
  <div class="fg-card"><small>五宽基价格压力（权重 50%）</small><div class="val {pressure_cls}">{meta["broad_pressure_score"]:.2f}</div><span class="fg-badge {pressure_cls}">{pressure_label}</span> · <small>五宽基中位数</small></div>
</div>

<div class="fg-pct-box">
  <div class="fg-pct-head">
    <span><strong>近 10 年历史分位（最多 2,520 个交易日）</strong>：当前处于历史 <strong>{pct10:.1f}%</strong> 分位</span>
    <small>0–25 极度恐慌 · 26–45 恐慌 · 46–55 中性 · 56–75 贪婪 · 76–100 极度贪婪</small>
  </div>
  <div class="fg-pct-track"><div class="fg-pct-thumb" style="left:{pct10:.1f}%"></div></div>
</div>

<section>
  <div class="fg-section-head">
    <h2>历史走势图（恐惧贪婪指数 vs 中证全指）</h2>
    <div class="fg-toolbar">
      <div class="fg-btn-group" role="group" aria-label="时间区间">
        <button type="button" data-range-days="240" aria-pressed="true">近1年</button>
        <button type="button" data-range-days="480" aria-pressed="false">近2年</button>
        <button type="button" data-range-days="720" aria-pressed="false">近3年</button>
        <button type="button" data-range-days="1200" aria-pressed="false">近5年</button>
        <button type="button" data-range-days="all" aria-pressed="false">全部(2016+)</button>
      </div>
      <span class="fg-range-text" data-chart-range></span>
      <button type="button" class="fg-action-btn" data-chart-reset hidden>重置视图</button>
      <button type="button" class="fg-action-btn" id="fg-download-csv">导出 CSV</button>
    </div>
  </div>
  <div class="fg-toolbar" style="margin-bottom:8px">
    <button type="button" class="fg-chip" data-series-toggle="fg" aria-pressed="true"><i style="background:linear-gradient(135deg,#c81e1e 0%,#f97316 35%,#eab308 55%,#84cc16 75%,#10b981 100%)"></i>恐惧贪婪指数（五色分段）</button>
    <button type="button" class="fg-chip" data-series-toggle="short" aria-pressed="false"><i style="background:#ec8b00"></i>短期市场情绪</button>
    <button type="button" class="fg-chip" data-series-toggle="pressure" aria-pressed="false"><i style="background:#7c3aed"></i>五宽基价格压力</button>
    <button type="button" class="fg-chip" data-series-toggle="csi_all" aria-pressed="true"><i style="background:#3b82f6"></i>中证全指（左外轴）</button>
  </div>
  <svg id="fg-main-svg" class="fg-svg" viewBox="0 0 1080 420">
    <defs>
      <linearGradient id="fg-line-grad" x1="0" y1="24" x2="0" y2="378" gradientUnits="userSpaceOnUse">
        <stop offset="0%" stop-color="#c81e1e"/>
        <stop offset="25%" stop-color="#c81e1e"/>
        <stop offset="25%" stop-color="#f97316"/>
        <stop offset="45%" stop-color="#f97316"/>
        <stop offset="45%" stop-color="#eab308"/>
        <stop offset="55%" stop-color="#eab308"/>
        <stop offset="55%" stop-color="#84cc16"/>
        <stop offset="75%" stop-color="#84cc16"/>
        <stop offset="75%" stop-color="#10b981"/>
        <stop offset="100%" stop-color="#10b981"/>
      </linearGradient>
      <linearGradient id="fg-area-grad" x1="0" y1="24" x2="0" y2="378" gradientUnits="userSpaceOnUse">
        <stop offset="0%" stop-color="#ef4444" stop-opacity="0.30"/>
        <stop offset="25%" stop-color="#ef4444" stop-opacity="0.30"/>
        <stop offset="25%" stop-color="#fb923c" stop-opacity="0.28"/>
        <stop offset="45%" stop-color="#fb923c" stop-opacity="0.28"/>
        <stop offset="45%" stop-color="#fde047" stop-opacity="0.32"/>
        <stop offset="55%" stop-color="#fde047" stop-opacity="0.32"/>
        <stop offset="55%" stop-color="#a3e635" stop-opacity="0.28"/>
        <stop offset="75%" stop-color="#a3e635" stop-opacity="0.28"/>
        <stop offset="75%" stop-color="#34d399" stop-opacity="0.32"/>
        <stop offset="100%" stop-color="#34d399" stop-opacity="0.32"/>
      </linearGradient>
    </defs>
    <line class="fg-sz-axis-line" x1="56" y1="24" x2="56" y2="378"/>
    <text class="fg-sz-axis-text" x="50" y="28" text-anchor="end" data-rmax></text>
    <text class="fg-sz-axis-text" x="50" y="116" text-anchor="end" data-r75></text>
    <text class="fg-sz-axis-text" x="50" y="205" text-anchor="end" data-rmid></text>
    <text class="fg-sz-axis-text" x="50" y="293" text-anchor="end" data-r25></text>
    <text class="fg-sz-axis-text" x="50" y="382" text-anchor="end" data-rmin></text>
    <line class="fg-axis" x1="92" y1="24" x2="974" y2="24" opacity=".55"/>
    <line class="fg-threshold-line" x1="92" y1="112.5" x2="974" y2="112.5"/>
    <line class="fg-threshold-line" x1="92" y1="183.3" x2="974" y2="183.3"/>
    <line class="fg-threshold-line" x1="92" y1="218.7" x2="974" y2="218.7"/>
    <line class="fg-threshold-line" x1="92" y1="289.5" x2="974" y2="289.5"/>
    <line class="fg-axis" x1="92" y1="378" x2="974" y2="378"/>
    <text x="86" y="28" text-anchor="end">100</text><text x="86" y="99" text-anchor="end">80</text><text x="86" y="170" text-anchor="end">60</text><text x="86" y="240" text-anchor="end">40</text><text x="86" y="311" text-anchor="end">20</text><text x="86" y="382" text-anchor="end">0</text>
    <text class="fg-threshold-label" x="982" y="116">→ 贪婪/极度贪婪</text>
    <text class="fg-threshold-label" x="982" y="187">→ 中性/贪婪</text>
    <text class="fg-threshold-label" x="982" y="222">→ 恐慌/中性</text>
    <text class="fg-threshold-label" x="982" y="293">→ 极度恐慌/恐慌</text>
    <path data-area="fg" fill="url(#fg-area-grad)"/>
    <path class="fg-line" data-line="pressure" stroke="#7c3aed"/>
    <path class="fg-line" data-line="short" stroke="#ec8b00"/>
    <path class="fg-line fg-line-main" data-line="fg" stroke="url(#fg-line-grad)"/>
    <path class="fg-line-sz" data-line="csi_all" stroke="#3b82f6"/>
    <line class="fg-crosshair" data-crosshair x1="0" y1="24" x2="0" y2="378" hidden/>
    <g data-dates></g>
  </svg>
  <div class="fg-legend-bands">
    <span><i style="background:#10b981"></i>0–25 极度恐慌</span>
    <span><i style="background:#84cc16"></i>26–45 恐慌</span>
    <span><i style="background:#eab308"></i>46–55 中性</span>
    <span><i style="background:#f97316"></i>56–75 贪婪</span>
    <span><i style="background:#c81e1e"></i>76–100 极度贪婪</span>
  </div>
  <p class="fg-note">双左轴：最左侧蓝轴为中证全指（000985）点位，内侧灰轴为恐惧贪婪指数（0–100分）。按住 <b>Ctrl + 滚轮</b> 缩放，按住 <b>鼠标左键</b> 拖动平移，<b>鼠标移动</b> 实时查看当日数值，双击或点「重置视图」恢复全量。</p>
</section>

<section>
  <div class="fg-section-head">
    <h2>五个宽基指数的价格压力分数（主指数权重 50%）</h2>
    <div class="fg-toolbar">
      <span class="fg-range-text" data-chart-range></span>
      <button type="button" class="fg-action-btn" data-chart-reset hidden>重置视图</button>
    </div>
  </div>
  <div class="fg-subgrid-5">{''.join(broad_cards)}</div>
  <div class="fg-toolbar" style="margin-bottom:8px">
    <button type="button" class="fg-chip" data-series-toggle="p_all" aria-pressed="true"><i style="background:#182b49"></i>五宽基综合压力</button>
    <button type="button" class="fg-chip" data-series-toggle="p_399001" aria-pressed="true"><i style="background:#2563eb"></i>深证成指</button>
    <button type="button" class="fg-chip" data-series-toggle="p_000300" aria-pressed="true"><i style="background:#0d9488"></i>沪深300</button>
    <button type="button" class="fg-chip" data-series-toggle="p_000905" aria-pressed="true"><i style="background:#d97706"></i>中证500</button>
    <button type="button" class="fg-chip" data-series-toggle="p_000852" aria-pressed="true"><i style="background:#dc2626"></i>中证1000</button>
    <button type="button" class="fg-chip" data-series-toggle="p_399006" aria-pressed="true"><i style="background:#7c3aed"></i>创业板指</button>
  </div>
  <svg id="fg-broad-svg" class="fg-svg" viewBox="0 0 1080 320">
    <line class="fg-axis" x1="56" y1="24" x2="1058" y2="24"/><line class="fg-axis" x1="56" y1="151" x2="1058" y2="151"/><line class="fg-axis" x1="56" y1="278" x2="1058" y2="278"/>
    <text x="12" y="28">100</text><text x="18" y="155">50</text><text x="24" y="282">0</text>
    <path class="fg-line" data-line="p_399001" stroke="#2563eb" opacity=".75"/>
    <path class="fg-line" data-line="p_000300" stroke="#0d9488" opacity=".75"/>
    <path class="fg-line" data-line="p_000905" stroke="#d97706" opacity=".75"/>
    <path class="fg-line" data-line="p_000852" stroke="#dc2626" opacity=".75"/>
    <path class="fg-line" data-line="p_399006" stroke="#7c3aed" opacity=".75"/>
    <path class="fg-line fg-line-main" data-line="p_all" stroke="#182b49"/>
    <line class="fg-crosshair" data-crosshair x1="0" y1="24" x2="0" y2="278" hidden/>
    <g data-dates></g>
  </svg>
  <p class="fg-note">每个宽基分别计算 20 日收益、60 日回撤、120 日回撤在近 756 个交易日的历史弱秩百分位，先取单宽基中位数，再取五个宽基的中位数作为综合价格压力。</p>
</section>

<section>
  <div class="fg-section-head">
    <h2>短期市场情绪的六项基础指标（主指数权重 50%）</h2>
    <div class="fg-toolbar">
      <span class="fg-range-text" data-chart-range></span>
      <button type="button" class="fg-action-btn" data-chart-reset hidden>重置视图</button>
    </div>
  </div>
  <div class="fg-subgrid-6">{''.join(short_cards)}</div>
  <div class="fg-toolbar" style="margin-bottom:8px">
    <button type="button" class="fg-chip" data-series-toggle="vol" aria-pressed="true"><i style="background:#dc2626"></i>波动率</button>
    <button type="button" class="fg-chip" data-series-toggle="turnover" aria-pressed="true"><i style="background:#2563eb"></i>相对换手率</button>
    <button type="button" class="fg-chip" data-series-toggle="margin" aria-pressed="true"><i style="background:#d97706"></i>融资融券</button>
    <button type="button" class="fg-chip" data-series-toggle="breadth" aria-pressed="true"><i style="background:#16a34a"></i>市场宽度</button>
    <button type="button" class="fg-chip" data-series-toggle="rsi" aria-pressed="true"><i style="background:#7c3aed"></i>RSI</button>
    <button type="button" class="fg-chip" data-series-toggle="limit" aria-pressed="true"><i style="background:#0891b2"></i>涨跌停比</button>
  </div>
  <svg id="fg-short-svg" class="fg-svg" viewBox="0 0 1080 320">
    <line class="fg-axis" x1="56" y1="24" x2="1058" y2="24"/><line class="fg-axis" x1="56" y1="151" x2="1058" y2="151"/><line class="fg-axis" x1="56" y1="278" x2="1058" y2="278"/>
    <text x="12" y="28">100</text><text x="18" y="155">50</text><text x="24" y="282">0</text>
    <path class="fg-line" data-line="vol" stroke="#dc2626" opacity=".82"/>
    <path class="fg-line" data-line="turnover" stroke="#2563eb" opacity=".82"/>
    <path class="fg-line" data-line="margin" stroke="#d97706" opacity=".82"/>
    <path class="fg-line" data-line="breadth" stroke="#16a34a" opacity=".82"/>
    <path class="fg-line" data-line="rsi" stroke="#7c3aed" opacity=".82"/>
    <path class="fg-line" data-line="limit" stroke="#0891b2" opacity=".82"/>
    <line class="fg-crosshair" data-crosshair x1="0" y1="24" x2="0" y2="278" hidden/>
    <g data-dates></g>
  </svg>
  <p class="fg-note">六项信号均标准化为 0–100 分（越高越偏贪婪）。当早期历史缺少全市场个股涨跌停/宽度缓存时，自动按可用分项权重重新归一化，不按零分处理。</p>
</section>

<section>
  <div class="fg-section-head"><h2>最近 20 个交易日观察明细</h2><small>按日期倒序排列</small></div>
  <table>
    <thead><tr><th>日期</th><th class="num">恐惧贪婪指数</th><th>情绪状态</th><th class="num">短期市场情绪</th><th class="num">五宽基压力</th><th class="num">近10年分位</th><th class="num">中证全指</th></tr></thead>
    <tbody>{''.join(recent_rows)}</tbody>
  </table>
</section>

<details class="fg-method">
  <summary>计算方法与口径说明（点击展开）</summary>
  <div class="fg-method-body">
    <p><strong>主公式：</strong>恐惧贪婪指数 = 短期市场情绪 × 50% + 五宽基价格压力 × 50%（分值范围 0–100，越高越贪婪，越低越恐慌）。</p>
    <ul>
      <li><strong>① 短期市场情绪（50%）：</strong>由波动率（25%，反向）、相对换手率/量能（20%）、融资融券（20%）、市场宽度（15%）、RSI（10%）、涨跌停比（10%）六项 0–100 分指标加权合成；缺失分项按当日可用权重重新归一化。</li>
      <li><strong>② 五宽基价格压力（50%）：</strong>覆盖深证成指、沪深300、中证500、中证1000、创业板指。每个指数计算 20 日收益、60 日回撤、120 日回撤在近 756 个交易日（约 3 年）滚动窗口内的历史弱秩百分位，先取单指数三项中位数，再取五个宽基的中位数。</li>
      <li><strong>历史分位：</strong>顶部近 10 年历史分位按最近最多 2,520 个有效交易日计算，不随下方图表缩放窗口变化。</li>
    </ul>
    <div class="fg-score-guide">
      <h3>最后怎么看分数？</h3>
      <div class="fg-score-strip" role="img" aria-label="恐贪分数五档色条：极恐0-25、恐惧25-45、中性45-55、贪婪55-75、极贪75-100">
        <div class="fg-score-seg fg-seg-ef"><b>极恐</b><span>0–25</span></div>
        <div class="fg-score-seg fg-seg-f"><b>恐惧</b><span>25–45</span></div>
        <div class="fg-score-seg fg-seg-n"><b>中性</b><span>45–55</span></div>
        <div class="fg-score-seg fg-seg-g"><b>贪婪</b><span>55–75</span></div>
        <div class="fg-score-seg fg-seg-eg"><b>极贪</b><span>75–100</span></div>
      </div>
      <p><strong>恐贪分数和历史百分位不是一回事。</strong>上面的 0–100 是市场情绪得分；页面顶部“近 10 年历史分位”则是该得分在最近最多 2,520 个有效交易日中的相对位置，不是第二次计算恐贪分数。</p>
      <p>每次计算只使用截至当日已知的数据。主指数历史口径统一从 2016 年开始；更早的短期情绪数据不拼接到主指数。此指标描述历史市场状态，不能单独用作买卖信号。</p>
    </div>
  </div>
</details>

<div id="fg-popover" class="fg-popover" hidden>
  <div class="fg-popover-head"><strong data-tip-title></strong><small data-tip-date></small></div>
  <div data-tip-rows></div>
</div>
<script type="application/json" id="fg-data">{payload_json}</script>
{FEAR_GREED_SCRIPT}
</main></body></html>'''

    tmp = output_html.with_name(f".{output_html.name}.tmp")
    tmp.write_text(doc, encoding="utf-8")
    tmp.replace(output_html)


def build_fear_greed(
    index_cache: Path = DEFAULT_INDEX_CACHE,
    margin_cache: Path = DEFAULT_MARGIN_CACHE,
    csi_all_cache: Path = DEFAULT_CSI_ALL_CACHE,
    sentiment_daily: Path = DEFAULT_SENTIMENT_DAILY,
    sentiment_intraday: Path = DEFAULT_SENTIMENT_INTRADAY,
    output_csv: Path = DEFAULT_HISTORY_CSV,
    output_html: Path = DEFAULT_OUTPUT_HTML,
    force: bool = False,
) -> dict:
    """执行完整采集、计算、CSV 导出与 HTML 渲染，返回最新快照元数据。"""
    index_df = load_or_update_broad_indices(index_cache, force=force)
    margin_df = load_or_update_margin_history(margin_cache, force=force)
    csi_all_df = load_or_update_csi_all_closes(csi_all_cache, force=force)
    frame, meta = calculate_fear_greed_history(
        index_df,
        margin_df,
        sentiment_daily,
        csi_all_df=csi_all_df,
        sentiment_intraday_path=sentiment_intraday,
    )
    save_history_csv(frame, output_csv)
    render_fear_greed_page(frame, meta, output_html)
    return meta


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成 A股恐惧贪婪指数")
    parser.add_argument("--force", action="store_true", help="强制全量重拉历史缓存")
    parser.add_argument("--backfill-breadth", action="store_true", help="回溯2015年至今全市场宽度与涨跌停真实历史")
    parser.add_argument("--workers", type=int, default=24, help="回溯全市场日K的并发线程数")
    parser.add_argument("--output-html", default=str(DEFAULT_OUTPUT_HTML))
    parser.add_argument("--output-csv", default=str(DEFAULT_HISTORY_CSV))
    args = parser.parse_args()
    if args.backfill_breadth:
        backfill_market_breadth_history(workers=args.workers)
    snapshot = build_fear_greed(
        output_csv=Path(args.output_csv),
        output_html=Path(args.output_html),
        force=args.force,
    )
    print(
        f"fear-greed written: {args.output_html}; csv={args.output_csv}; "
        f"days={snapshot['total_days']} ({snapshot['first_date']}~{snapshot['latest_date']}); "
        f"latest={snapshot['fear_greed_index']:.2f} (10y_pct={snapshot['history_pct_10y']:.1f}%)"
    )
