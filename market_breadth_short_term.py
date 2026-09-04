"""用全市场日 K 计算市场广度与短线赚钱效应。

数据口径：
- 股票池：沪深北 A 股，包含 ST/退市整理股票，不含 B 股。
- 行情源：腾讯财经批量报价（股票池）与前复权日 K（历史行情）。
- 正式日值只计算到成交额文件中的最新完整交易日。

输出会合并写入 market_sentiment_daily.csv，同时保留已存在的融资等分项。
"""
from __future__ import annotations

import argparse
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import sleep

import pandas as pd
import requests


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
QUOTE_URL = "https://qt.gtimg.cn/q="
BSE_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
KLINE_URLS = [
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
]
INDEX_SPECS = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sh000688", "科创50"),
    ("sz399006", "创业板指"),
    ("sh000905", "中证500"),
]
HEADERS = {"User-Agent": UA, "Referer": "https://gu.qq.com/"}
_THREAD_LOCAL = threading.local()


def _session() -> requests.Session:
    """每个工作线程复用自己的连接，降低代理握手和并发失败率。"""
    if not hasattr(_THREAD_LOCAL, "session"):
        _THREAD_LOCAL.session = requests.Session()
        _THREAD_LOCAL.session.headers.update(HEADERS)
    return _THREAD_LOCAL.session


def _chunks(values: list[str], size: int):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _candidate_secids() -> list[str]:
    """生成沪深 A 股可能使用的代码段，实际上市状态由腾讯批量报价确认。"""
    ranges = [
        ("sh", 600000, 605999),
        ("sh", 688000, 689999),
        ("sz", 0, 3999),
        # 创业板已开始使用 302xxx；多留代码空间避免新股再次漏入。
        ("sz", 300000, 309999),
    ]
    return [f"{prefix}{code:06d}" for prefix, first, last in ranges
            for code in range(first, last + 1)]


def _bse_list() -> list[dict]:
    """分页取得北交所股票清单；仅在刷新股票池时调用，严格串行限流。"""
    records = []
    page = 1
    while True:
        params = {
            "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f12", "fs": "m:0+t:81+s:2048", "fields": "f12,f14",
        }
        response = requests.get(
            BSE_LIST_URL, params=params, headers=HEADERS, timeout=20
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        rows = data.get("diff") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        for item in rows:
            code = str(item.get("f12") or "").zfill(6)
            name = str(item.get("f14") or "").strip()
            if code.isdigit() and len(code) == 6 and name:
                records.append({"secid": "bj" + code, "code": code, "name": name})
        if not rows or len(records) >= int(data.get("total") or 0):
            break
        page += 1
        sleep(1.05)
    return records


def _quote_batch(secids: list[str]) -> list[dict]:
    error = None
    for attempt in range(3):
        try:
            response = _session().get(QUOTE_URL + ",".join(secids), timeout=20)
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            error = exc
            sleep(0.25 * (attempt + 1))
    else:
        raise error
    text = response.content.decode("gbk", errors="ignore")
    records = []
    for prefix, code, payload in re.findall(r'v_(sh|sz|bj)(\d{6})="([^"]*)"', text):
        values = payload.split("~")
        # 腾讯首字段是市场标识：沪市通常为 1，深市通常为 51，不能当成成功码。
        if len(values) < 4 or not values[0] or not values[1] or not values[3]:
            continue
        name = values[1].strip()
        records.append({"secid": prefix + code, "code": code, "name": name})
    return records


def tencent_a_share_universe(
    cache_path: str | Path = "market_stock_universe.csv",
    workers: int = 12,
    refresh: bool = False,
) -> pd.DataFrame:
    """读取或发现当前沪深北 A 股股票池。"""
    path = Path(cache_path)
    cached_bse: list[dict] = []
    if path.exists():
        universe = pd.read_csv(path, dtype={"code": str})
        universe["code"] = universe["code"].str.zfill(6)
        if not refresh:
            return universe.drop_duplicates("secid").sort_values("secid").reset_index(drop=True)
        cached_bse = universe.loc[
            universe.secid.str.startswith("bj")
        ].to_dict("records")

    batches = list(_chunks(_candidate_secids(), 80))
    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 16))) as pool:
        futures = [pool.submit(_quote_batch, batch) for batch in batches]
        for future in as_completed(futures):
            try:
                records.extend(future.result())
            except requests.RequestException:
                continue
    try:
        records.extend(_bse_list())
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"[WARN] 北交所股票清单刷新失败: {exc}")
        records.extend(cached_bse)
    universe = pd.DataFrame(records).drop_duplicates("secid").sort_values("secid")
    if len(universe) < 4000:
        raise RuntimeError(f"腾讯股票池仅识别到 {len(universe)} 只，低于合理下限，停止写入")
    universe.to_csv(path, index=False, encoding="utf-8-sig")
    return universe.reset_index(drop=True)


def _fetch_kline(record: dict, bars: int, end: pd.Timestamp) -> pd.DataFrame | None:
    secid = record["secid"]
    params = {"param": f"{secid},day,,,{bars},qfq"}
    error = None
    for attempt in range(5):
        try:
            response = _session().get(KLINE_URLS[attempt % len(KLINE_URLS)],
                                      params=params, timeout=25)
            response.raise_for_status()
            item = (response.json().get("data") or {}).get(secid) or {}
            rows = item.get("qfqday") or item.get("day") or []
            if not rows:
                return None
            frame = pd.DataFrame([row[:6] for row in rows],
                                 columns=["date", "open", "close", "high", "low", "volume"])
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
            for column in ["open", "close", "high", "low", "volume"]:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            frame = frame.dropna().loc[lambda x: x.date <= end]
            if frame.empty:
                return None
            frame["secid"], frame["code"], frame["name"] = secid, record["code"], record["name"]
            return frame
        except (requests.RequestException, ValueError, KeyError) as exc:
            error = exc
            sleep(0.75 * (attempt + 1))
    print(f"[WARN] {secid} 日K失败: {error}")
    return None


def tencent_market_kline(
    universe: pd.DataFrame,
    end: str | pd.Timestamp,
    cache_path: str | Path = "market_stock_daily_cache.csv.gz",
    bars: int = 380,
    workers: int = 8,
    force: bool = False,
) -> pd.DataFrame:
    """批量回溯股票日 K；缓存已覆盖目标日期时直接复用。"""
    end = pd.Timestamp(end).normalize()
    path = Path(cache_path)
    if path.exists() and not force:
        cached = pd.read_csv(path, parse_dates=["date"], dtype={"code": str})
        if not cached.empty and cached.date.max() >= end:
            return cached.loc[cached.date <= end].copy()

    frames: list[pd.DataFrame] = []
    records = universe.to_dict("records")
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 32))) as pool:
        futures = {pool.submit(_fetch_kline, record, bars, end): record["secid"]
                   for record in records}
        for completed, future in enumerate(as_completed(futures), 1):
            frame = future.result()
            if frame is not None:
                frames.append(frame)
            if completed % 250 == 0 or completed == len(futures):
                print(f"daily K progress: {completed}/{len(futures)}")
    if len(frames) < 4000:
        raise RuntimeError(f"仅成功取得 {len(frames)} 只股票日K，低于合理下限，停止写入")
    data = pd.concat(frames, ignore_index=True)
    data.to_csv(path, index=False, encoding="utf-8-sig", compression="gzip")
    return data


def tencent_index_kline(
    end: str | pd.Timestamp,
    cache_path: str | Path = "market_index_daily_cache.csv",
    bars: int = 380,
    force: bool = False,
) -> pd.DataFrame:
    """获取五大指数日K，用于计算等权指数动量。"""
    end = pd.Timestamp(end).normalize()
    path = Path(cache_path)
    cached = None
    if path.exists():
        cached = pd.read_csv(path, parse_dates=["date"])
        if not force and not cached.empty and cached.date.max() >= end:
            return cached.loc[cached.date <= end].copy()

    frames = []
    try:
        for secid, name in INDEX_SPECS:
            params = {"param": f"{secid},day,,,{bars},qfq"}
            error = None
            for attempt in range(max(3, len(KLINE_URLS))):
                try:
                    response = _session().get(
                        KLINE_URLS[attempt % len(KLINE_URLS)],
                        params=params,
                        timeout=25,
                    )
                    response.raise_for_status()
                    item = (response.json().get("data") or {}).get(secid) or {}
                    rows = item.get("qfqday") or item.get("day") or []
                    if not rows:
                        raise ValueError(f"腾讯未返回{name}({secid})日K")
                    frame = pd.DataFrame(
                        [row[:6] for row in rows],
                        columns=["date", "open", "close", "high", "low", "volume"],
                    )
                    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
                    for column in ["open", "close", "high", "low", "volume"]:
                        frame[column] = pd.to_numeric(frame[column], errors="coerce")
                    frame = frame.dropna().loc[lambda x: x.date <= end]
                    if frame.empty:
                        raise ValueError(f"腾讯返回的{name}({secid})日K无有效记录")
                    frame["secid"], frame["name"] = secid, name
                    frames.append(frame)
                    break
                except (requests.RequestException, ValueError, KeyError) as exc:
                    error = exc
                    if attempt + 1 < max(3, len(KLINE_URLS)):
                        sleep(0.75 * (attempt + 1))
            else:
                raise RuntimeError(f"{name}({secid})日K刷新失败: {error}")
    except RuntimeError as exc:
        if cached is None or cached.empty:
            raise
        print(
            f"[WARN] 指数日K刷新失败，沿用缓存至 "
            f"{cached.date.max():%Y-%m-%d}: {exc}"
        )
        return cached.loc[cached.date <= end].copy()

    data = pd.concat(frames, ignore_index=True).sort_values(["secid", "date"])
    data.to_csv(path, index=False, encoding="utf-8-sig")
    return data


def _rolling_percentile(series: pd.Series, window: int = 120) -> pd.Series:
    return series.rolling(window, min_periods=20).rank(pct=True) * 100


def calculate_daily_indicators(
    kline: pd.DataFrame,
    index_kline: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """从股票级日 K 计算日度广度和短线赚钱效应。"""
    frames = []
    for (_, code), stock in kline.groupby(["secid", "code"], sort=False):
        stock = stock.sort_values("date").copy()
        close, high, low = stock["close"], stock["high"], stock["low"]
        previous = close.shift()
        stock["return_pct"] = close.pct_change(fill_method=None) * 100
        stock["up"] = stock["return_pct"] > 0
        stock["down"] = stock["return_pct"] < 0
        stock["ma20_valid"] = close.rolling(20, min_periods=20).mean().notna()
        stock["ma60_valid"] = close.rolling(60, min_periods=60).mean().notna()
        stock["above_ma20"] = close > close.rolling(20, min_periods=20).mean()
        stock["above_ma60"] = close > close.rolling(60, min_periods=60).mean()
        stock["new_high_20"] = close >= close.rolling(20, min_periods=20).max()
        stock["new_low_20"] = close <= close.rolling(20, min_periods=20).min()

        growth_board = str(code).startswith(("300", "301", "688", "689"))
        limit = 19.5 if growth_board else 9.5
        eligible = pd.Series(True, index=stock.index)
        if growth_board:
            eligible.iloc[:5] = False
        high_return = (high / previous - 1) * 100
        low_return = (low / previous - 1) * 100
        hit_up = eligible & (high_return >= limit)
        hit_down = eligible & (low_return <= -limit)
        stock["limit_up"] = hit_up & (close / high >= 0.999) & (stock["return_pct"] >= limit)
        stock["break_board"] = hit_up & ~stock["limit_up"]
        stock["limit_down"] = hit_down & (close / low <= 1.001) & (stock["return_pct"] <= -limit)
        groups = (~stock["limit_up"]).cumsum()
        stock["limit_streak"] = stock["limit_up"].groupby(groups).cumsum()
        stock["previous_limit_up"] = stock["limit_up"].shift(fill_value=False)
        stock["advanced"] = stock["previous_limit_up"] & stock["limit_up"]
        stock["previous_limit_return_pct"] = stock["return_pct"].where(stock["previous_limit_up"])
        frames.append(stock[[
            "date", "return_pct", "up", "down", "ma20_valid", "ma60_valid",
            "above_ma20", "above_ma60", "new_high_20", "new_low_20",
            "limit_up", "break_board", "limit_down", "limit_streak",
            "previous_limit_up", "advanced", "previous_limit_return_pct",
        ]])

    detail = pd.concat(frames, ignore_index=True)
    grouped = detail.groupby("date", sort=True)
    daily = grouped.agg(
        valid_stock_count=("return_pct", "count"),
        up_count=("up", "sum"),
        down_count=("down", "sum"),
        ma20_valid_count=("ma20_valid", "sum"),
        ma60_valid_count=("ma60_valid", "sum"),
        above_ma20_count=("above_ma20", "sum"),
        above_ma60_count=("above_ma60", "sum"),
        new_high_20_count=("new_high_20", "sum"),
        new_low_20_count=("new_low_20", "sum"),
        limit_up_count=("limit_up", "sum"),
        break_board_count=("break_board", "sum"),
        limit_down_count=("limit_down", "sum"),
        max_limit_streak=("limit_streak", "max"),
        previous_limit_up_count=("previous_limit_up", "sum"),
        advanced_count=("advanced", "sum"),
        previous_limit_return_median_pct=("previous_limit_return_pct", "median"),
    ).reset_index()

    direction_denominator = daily.up_count + daily.down_count
    daily["advance_ratio_pct"] = daily.up_count / direction_denominator * 100
    daily["above_ma20_pct"] = daily.above_ma20_count / daily.ma20_valid_count * 100
    daily["above_ma60_pct"] = daily.above_ma60_count / daily.ma60_valid_count * 100
    extremes = daily.new_high_20_count + daily.new_low_20_count
    daily["new_high_low_score"] = (
        daily.new_high_20_count / extremes.where(extremes > 0) * 100
    ).fillna(50)
    daily["net_advances"] = daily.up_count - daily.down_count
    daily["ad_10d"] = daily.net_advances.rolling(10, min_periods=5).sum()
    daily["ad_trend_score"] = _rolling_percentile(daily.ad_10d)
    daily["breadth_score"] = (
        daily.advance_ratio_pct * 0.30
        + daily.above_ma20_pct * 0.25
        + daily.above_ma60_pct * 0.15
        + daily.new_high_low_score * 0.15
        + daily.ad_trend_score * 0.15
    )

    return_group = detail.groupby("date", sort=True)["return_pct"]
    daily["mean_return_pct"] = return_group.mean().reindex(daily.date).to_numpy()
    daily["median_return_pct"] = return_group.median().reindex(daily.date).to_numpy()
    daily["strong_up_share_pct"] = (
        return_group.apply(lambda values: (values >= 2).mean() * 100)
        .reindex(daily.date).to_numpy()
    )
    daily["strong_down_share_pct"] = (
        return_group.apply(lambda values: (values <= -2).mean() * 100)
        .reindex(daily.date).to_numpy()
    )
    daily["strong_net_share_pct"] = (
        daily.strong_up_share_pct - daily.strong_down_share_pct
    )
    for column in ["mean_return_pct", "median_return_pct", "strong_net_share_pct"]:
        daily[column + "_score"] = _rolling_percentile(daily[column])

    if index_kline is not None and not index_kline.empty:
        index_data = index_kline.sort_values(["secid", "date"]).copy()
        index_data["index_return_pct"] = (
            index_data.groupby("secid").close.pct_change(fill_method=None) * 100
        )
        index_daily = (
            index_data.groupby("date", as_index=False)
            .agg(
                index_equal_weight_return_pct=("index_return_pct", "mean"),
                index_count=("index_return_pct", "count"),
            )
        )
        index_daily["index_equal_weight_return_pct_score"] = _rolling_percentile(
            index_daily.index_equal_weight_return_pct
        )
        daily = daily.merge(index_daily, on="date", how="left")
    else:
        daily["index_equal_weight_return_pct"] = pd.NA
        daily["index_count"] = pd.NA
        daily["index_equal_weight_return_pct_score"] = pd.NA

    momentum_inputs = {
        "mean_return_pct_score": 0.30,
        "median_return_pct_score": 0.25,
        "strong_net_share_pct_score": 0.20,
        "index_equal_weight_return_pct_score": 0.25,
    }
    momentum_available_weight = sum(
        daily[column].notna() * weight for column, weight in momentum_inputs.items()
    )
    momentum_weighted_score = sum(
        daily[column].fillna(0) * weight for column, weight in momentum_inputs.items()
    )
    daily["momentum_score"] = (
        momentum_weighted_score / momentum_available_weight
    ).where(momentum_available_weight >= 0.75)
    daily["momentum_component_coverage_pct"] = momentum_available_weight * 100

    attempts = daily.limit_up_count + daily.break_board_count
    daily["seal_success_pct"] = daily.limit_up_count / attempts.where(attempts > 0) * 100
    daily["advancement_rate_pct"] = (
        daily.advanced_count / daily.previous_limit_up_count.where(daily.previous_limit_up_count > 0) * 100
    )
    daily["limit_up_down_balance"] = daily.limit_up_count - daily.limit_down_count
    short_inputs = {
        "previous_limit_return_median_pct": 0.30,
        "advancement_rate_pct": 0.20,
        "seal_success_pct": 0.20,
        "limit_up_down_balance": 0.15,
        "max_limit_streak": 0.15,
    }
    score_columns = []
    for column, weight in short_inputs.items():
        score_column = column + "_score"
        daily[score_column] = _rolling_percentile(daily[column])
        score_columns.append((score_column, weight))
    available_weight = sum(daily[column].notna() * weight for column, weight in score_columns)
    weighted_score = sum(daily[column].fillna(0) * weight for column, weight in score_columns)
    daily["short_term_score"] = (weighted_score / available_weight).where(available_weight >= 0.50)
    return daily


def update_dashboard_input(
    indicators: pd.DataFrame,
    turnover_file: str | Path = "hs_a_share_turnover.csv",
    output: str | Path = "market_sentiment_daily.csv",
) -> pd.DataFrame:
    """按成交额交易日历合并新分项，并保留其他已接入列。"""
    calendar = pd.read_csv(turnover_file, parse_dates=["date"])[["date"]]
    indicators = calendar.merge(indicators, on="date", how="left")
    path = Path(output)
    if path.exists():
        old = pd.read_csv(path, parse_dates=["date"])
        replace = [column for column in indicators.columns if column != "date" and column in old]
        old = old.drop(columns=replace)
        indicators = old.merge(indicators, on="date", how="outer")
    indicators = indicators.sort_values("date").drop_duplicates("date", keep="last")
    indicators.to_csv(path, index=False, encoding="utf-8-sig")
    return indicators


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--turnover", default="hs_a_share_turnover.csv")
    parser.add_argument("--output", default="market_sentiment_daily.csv")
    parser.add_argument("--universe-cache", default="market_stock_universe.csv")
    parser.add_argument("--kline-cache", default="market_stock_daily_cache.csv.gz")
    parser.add_argument("--index-cache", default="market_index_daily_cache.csv")
    parser.add_argument("--bars", type=int, default=380)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--refresh-universe", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    turnover = pd.read_csv(args.turnover, parse_dates=["date"])
    end = turnover.date.max()
    universe = tencent_a_share_universe(
        args.universe_cache, workers=min(args.workers, 16), refresh=args.refresh_universe
    )
    print(f"A-share universe: {len(universe)}")
    kline = tencent_market_kline(
        universe, end, args.kline_cache, bars=args.bars, workers=args.workers, force=args.force
    )
    print(f"daily K rows: {len(kline)}; stocks: {kline.secid.nunique()}")
    index_kline = tencent_index_kline(
        end, args.index_cache, bars=args.bars, force=args.force
    )
    daily = calculate_daily_indicators(kline, index_kline)
    result = update_dashboard_input(daily, args.turnover, args.output)
    latest = result.dropna(subset=["breadth_score", "short_term_score"]).iloc[-1]
    print(
        f"breadth/short-term written: {args.output}; "
        f"{latest.date:%Y-%m-%d} breadth={latest.breadth_score:.2f}, "
        f"short={latest.short_term_score:.2f}, momentum={latest.momentum_score:.2f}"
    )


if __name__ == "__main__":
    main()
