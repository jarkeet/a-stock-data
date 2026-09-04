"""生成今日盘中市场情绪暂估快照。

盘中行情来自腾讯财经全市场报价；主力资金净流入来自东财沪深指数分钟
资金流，并仅作为盘中资金风险偏好的临时代理。正式收盘数据仍由原有
日度脚本生成。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import threading
import time as time_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

from market_breadth_short_term import HEADERS, QUOTE_URL
from market_turnover_sentiment import hs_a_share_turnover, turnover_sentiment


_LOCAL = threading.local()
FLOW_URL = "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get"
INDUSTRY_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
THS_INDUSTRY_URL = (
    "https://q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/{page}/"
)
THS_HOT_LIST_BASE = (
    "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1"
)
TENCENT_INTRADAY_URL = "https://web.ifzq.gtimg.cn/appstock/app/day/query"
INDEX_SPECS = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sh000688", "科创50"),
    ("sz399006", "创业板指"),
    ("sh000905", "中证500"),
]
_EM_LOCK = threading.Lock()
_EM_LAST_CALL = [0.0]
QUOTE_COLUMNS = [
    "secid", "code", "name", "price", "previous_close", "open",
    "volume_lot", "outer_volume_lot", "inner_volume_lot", "change_pct",
    "high", "low", "amount_wan", "limit_up_price", "limit_down_price",
    "quote_time",
]


def _session():
    if not hasattr(_LOCAL, "session"):
        _LOCAL.session = requests.Session()
        _LOCAL.session.headers.update(HEADERS)
    return _LOCAL.session


def _eastmoney_get(url, *, params):
    """东财请求串行限流，避免盘中循环触发接口风控。"""
    with _EM_LOCK:
        wait = 1.0 - (time_module.monotonic() - _EM_LAST_CALL[0])
        if wait > 0:
            time_module.sleep(wait)
        try:
            return _session().get(
                url, params=params,
                headers={"Referer": "https://quote.eastmoney.com/"}, timeout=20,
            )
        finally:
            _EM_LAST_CALL[0] = time_module.monotonic()


def _chunks(values, size=80):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _quote_batch(secids):
    """拉取一批腾讯报价；HTTP 成功但无可解析行情时也重试。"""
    last_error = None
    for attempt in range(3):
        try:
            response = _session().get(QUOTE_URL + ",".join(secids), timeout=20)
            response.raise_for_status()
            text = response.content.decode("gbk", errors="ignore")
            rows = []
            for prefix, code, payload in re.findall(
                r'v_(sh|sz|bj)(\d{6})="([^"]*)"', text
            ):
                values = payload.split("~")
                if len(values) < 49 or not values[3] or not values[4]:
                    continue

                def number(index):
                    try:
                        return float(values[index])
                    except (ValueError, IndexError):
                        return float("nan")

                rows.append({
                    "secid": prefix + code,
                    "code": code,
                    "name": values[1],
                    "price": number(3),
                    "previous_close": number(4),
                    "open": number(5),
                    "volume_lot": number(6),
                    "outer_volume_lot": number(7),
                    "inner_volume_lot": number(8),
                    "change_pct": number(32),
                    "high": number(33),
                    "low": number(34),
                    "amount_wan": number(37),
                    "limit_up_price": number(47),
                    "limit_down_price": number(48),
                    "quote_time": values[30] if len(values) > 30 else "",
                })
            if rows:
                return rows
            content_type = response.headers.get("Content-Type", "unknown")
            last_error = RuntimeError(
                "腾讯行情返回 HTTP 200，但未解析到报价"
                f"（content-type={content_type}, bytes={len(response.content)}）"
            )
        except requests.RequestException as exc:
            last_error = exc
        if attempt < 2:
            time_module.sleep(0.35 * (attempt + 1))
    raise RuntimeError(f"腾讯行情批次连续 3 次失败: {last_error}") from last_error


def fetch_market_snapshot(universe, workers=12):
    batches = list(_chunks(universe.secid.tolist()))
    rows = []
    failures = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as pool:
        futures = {
            pool.submit(_quote_batch, batch): batch for batch in batches
        }
        for future in as_completed(futures):
            try:
                rows.extend(future.result())
            except (requests.RequestException, RuntimeError) as exc:
                batch = futures[future]
                failures.append(f"{batch[0]}..{batch[-1]}: {exc}")
    quotes = pd.DataFrame(rows, columns=QUOTE_COLUMNS).drop_duplicates("secid")
    quotes = quotes.loc[
        (quotes["price"] > 0) & (quotes["previous_close"] > 0)
    ].copy()
    quote_times = pd.to_datetime(
        quotes.quote_time, format="%Y%m%d%H%M%S", errors="coerce"
    )
    if quote_times.notna().any():
        latest_quote_date = quote_times.max().normalize()
        quotes = quotes.loc[quote_times.dt.normalize() == latest_quote_date].copy()
    if len(quotes) < 4000:
        detail = f"；失败批次 {len(failures)}/{len(batches)}"
        if failures:
            detail += f"；首个错误：{failures[0]}"
        raise RuntimeError(
            f"盘中有效报价仅 {len(quotes)} 只，低于可信下限{detail}"
        )
    if failures:
        print(
            f"[WARN] 腾讯行情有 {len(failures)}/{len(batches)} 个批次失败，"
            f"其余 {len(quotes)} 条有效报价继续用于快照；首个错误：{failures[0]}"
        )
    return quotes


def _latest_quote_timestamp(quotes, fallback):
    """返回本轮全市场报价中的最新行情时间，缺失时回退到调用时间。"""
    if "quote_time" not in quotes:
        return fallback
    timestamps = pd.to_datetime(
        quotes["quote_time"], format="%Y%m%d%H%M%S", errors="coerce"
    ).dropna()
    if timestamps.empty:
        return fallback
    latest = timestamps.max().to_pydatetime()
    # 仅接受当日正常交易时段的时间戳，避免异常行情行污染盘中时间锚点。
    if latest.date() != fallback.date() or not (time(9, 15) <= latest.time() <= time(15, 5)):
        return fallback
    return latest


def fetch_main_fund_flow():
    """汇总沪指与深成指分钟主力净流入，返回亿元和最新时间。"""
    values, times = [], []
    params = {
        "klt": "1", "lmt": "0",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }
    for secid in ["1.000001", "0.399001"]:
        response = _eastmoney_get(
            FLOW_URL, params={**params, "secid": secid}
        )
        response.raise_for_status()
        klines = (response.json().get("data") or {}).get("klines") or []
        if not klines:
            raise RuntimeError(f"{secid} 未返回分钟资金流")
        parts = klines[-1].split(",")
        times.append(parts[0])
        values.append(float(parts[1]) / 1e8)
    return sum(values), min(times)


def fetch_dragon_tiger_top5(as_of=None):
    """返回最近已发布交易日龙虎榜净买额前5名，金额单位为亿元。"""
    checked_at = datetime.now()
    as_of = pd.Timestamp(as_of or checked_at).to_pydatetime()
    response = _eastmoney_get(
        DATACENTER_URL,
        params={
            "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
            "columns": (
                "TRADE_DATE,SECURITY_CODE,SECURITY_NAME_ABBR,"
                "CHANGE_RATE,BILLBOARD_NET_AMT"
            ),
            "filter": "",
            "pageNumber": "1",
            "pageSize": "100",
            "sortColumns": "TRADE_DATE,BILLBOARD_NET_AMT",
            "sortTypes": "-1,-1",
            "source": "WEB",
            "client": "WEB",
        },
    )
    response.raise_for_status()
    rows = (response.json().get("result") or {}).get("data") or []
    if not rows:
        raise RuntimeError("龙虎榜尚未返回已发布数据")

    latest_date = max(str(row.get("TRADE_DATE") or "")[:10] for row in rows)
    latest_rows = [
        row for row in rows
        if str(row.get("TRADE_DATE") or "")[:10] == latest_date
    ]

    stocks = []
    seen_codes = set()
    for row in sorted(
        latest_rows,
        key=lambda item: float(item.get("BILLBOARD_NET_AMT") or 0),
        reverse=True,
    ):
        code = str(row.get("SECURITY_CODE") or "")
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        stocks.append({
            "code": code,
            "name": str(row.get("SECURITY_NAME_ABBR") or code),
            "change_pct": float(row.get("CHANGE_RATE") or 0),
            "net_amount_yi": float(row.get("BILLBOARD_NET_AMT") or 0) / 1e8,
        })
        if len(stocks) == 5:
            break
    if not stocks:
        raise RuntimeError(f"{latest_date} 龙虎榜没有可展示的股票")
    published_date = datetime.strptime(latest_date, "%Y-%m-%d").date()
    if published_date == as_of.date():
        status = "今日已发布"
    elif as_of.hour < 15:
        status = "今日盘中尚未发布，盘后自动更新"
    else:
        status = "今日榜单待发布，自动刷新中"
    return {
        "date": latest_date,
        "stocks": stocks,
        "source": "东方财富龙虎榜",
        "status": status,
        "checked_at": checked_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _rank_industry_rows(rows, top_n, source, source_note=""):
    """校验行业横截面后，返回涨幅和跌幅各前 N 名。"""
    rows = [row for row in rows if row["name"] and pd.notna(row["change_pct"])]
    unique_rows = {row["name"]: row for row in rows}
    rows = list(unique_rows.values())
    if len(rows) < max(80, top_n * 2):
        raise RuntimeError(f"{source}行业排名仅返回 {len(rows)} 条，低于可信下限")
    valid_amounts = sum(float(row.get("amount_yi") or 0) > 0 for row in rows)
    if valid_amounts < len(rows) * 0.9:
        raise RuntimeError(f"{source}行业成交额有效率仅 {valid_amounts}/{len(rows)}")
    rows.sort(key=lambda item: item["change_pct"], reverse=True)
    return {
        "top": rows[:top_n],
        "bottom": rows[-top_n:],
        "total": len(rows),
        "source": source,
        "source_note": source_note,
    }


def fetch_ths_industry_heatmap(top_n=12):
    """同花顺行业板块排名；使用普通页面，避开需登录态的 Ajax 接口。"""
    rows = []
    headers = {
        "Referer": "https://q.10jqka.com.cn/thshy/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    for page in (1, 2):
        response = _session().get(
            THS_INDUSTRY_URL.format(page=page), headers=headers, timeout=20
        )
        response.raise_for_status()
        response.encoding = "gbk"
        tables = pd.read_html(StringIO(response.text), flavor="lxml")
        if not tables or tables[0].shape[1] < 12:
            raise RuntimeError(f"同花顺行业第 {page} 页表格结构异常")
        table = tables[0]
        for values in table.itertuples(index=False, name=None):
            try:
                change_pct = float(values[2])
                amount_yi = float(values[4])
            except (TypeError, ValueError):
                continue
            rows.append({
                "code": "",
                "name": str(values[1]).strip(),
                "change_pct": change_pct,
                "amount_yi": amount_yi,
                "market_cap_yi": 0.0,
                "net_inflow_yi": (
                    float(values[5]) if pd.notna(values[5]) else 0.0
                ),
                "up_count": int(float(values[6])) if pd.notna(values[6]) else 0,
                "down_count": int(float(values[7])) if pd.notna(values[7]) else 0,
                "leader": str(values[9]).strip() if pd.notna(values[9]) else "",
                "leader_change_pct": (
                    float(values[11]) if pd.notna(values[11]) else 0.0
                ),
            })
    return _rank_industry_rows(
        rows, top_n, "同花顺",
        "同花顺行业普通页面；Ajax 接口需要登录态，未使用",
    )


def fetch_eastmoney_industry_heatmap(top_n=12):
    """东方财富行业板块排名，作为同花顺不可用时的自动降级源。"""
    params = {
        "pn": "1", "pz": "500", "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fs": "m:90+t:2",
        "fields": "f3,f6,f20,f12,f14,f104,f105,f136,f140",
    }
    response = _eastmoney_get(INDUSTRY_URL, params=params)
    response.raise_for_status()
    payload = (response.json().get("data") or {}).get("diff") or []
    if isinstance(payload, dict):
        payload = list(payload.values())
    rows, seen_names = [], set()
    for item in payload:
        name = str(item.get("f14") or "").strip()
        try:
            change_pct = float(item.get("f3"))
        except (TypeError, ValueError):
            continue
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        rows.append({
            "code": str(item.get("f12") or ""),
            "name": name,
            "change_pct": change_pct,
            "amount_yi": float(item.get("f6") or 0) / 1e8,
            "market_cap_yi": float(item.get("f20") or 0) / 1e8,
            "up_count": int(item.get("f104") or 0),
            "down_count": int(item.get("f105") or 0),
            "leader": str(item.get("f140") or ""),
            "leader_change_pct": float(item.get("f136") or 0),
        })
    return _rank_industry_rows(rows, top_n, "东方财富")


def fetch_industry_heatmap(top_n=12):
    """行业热力图数据：同花顺优先，结构/数量异常时自动降级到东财。"""
    try:
        return fetch_ths_industry_heatmap(top_n)
    except Exception as error:
        result = fetch_eastmoney_industry_heatmap(top_n)
        result["source_note"] = f"同花顺暂不可用，已降级：{type(error).__name__}"
        result["fallback"] = True
        return result


def _ths_hot_get(kind, **params):
    response = _session().get(
        f"{THS_HOT_LIST_BASE}/{kind}",
        params=params,
        headers={
            "Referer": "https://eq.10jqka.com.cn/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status_code") != 0:
        raise RuntimeError(payload.get("status_msg") or "同花顺热榜返回异常")
    return payload.get("data") or {}


def _ths_index_hot_get(top_n):
    response = _session().post(
        "https://dq.10jqka.com.cn/fuyao/fund_fe_tools/fund/v1/index_sector",
        json={"page_info": {"page_begin": 0, "page_size": top_n}},
        headers={
            "Referer": "https://eq.10jqka.com.cn/",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status_code") != 0:
        raise RuntimeError(payload.get("status_msg") or "同花顺指数板块榜返回异常")
    data = payload.get("data") or {}
    indexes = {
        int(item["idx"]): item.get("index_id")
        for item in (data.get("indexes") or [])
        if item.get("idx") is not None
    }
    rows = []
    for rank, item in enumerate((data.get("data") or data.get("list") or [])[:top_n], 1):
        values = {
            indexes.get(int(value.get("idx"))): value.get("value")
            for value in (item.get("values") or [])
            if value.get("idx") is not None
        }
        code = str(item.get("code") or "")
        rows.append({
            "rank": rank,
            "code": code.split(":")[-1],
            "name": str(values.get("security_name") or ""),
            "heat": float(values.get("ths-hot-data-minute-attention-rate") or 0),
            "change_pct": (
                float(values["price_change_ratio_pct"])
                if values.get("price_change_ratio_pct") is not None else None
            ),
            "rank_change": 0,
            "hot_tag": "",
            "reason": "",
            "etf_name": "",
            "etf_change_pct": None,
        })
    return rows


def _normalize_ths_plate_list(items, top_n):
    rows = []
    for item in items[:top_n]:
        rows.append({
            "rank": int(item.get("order") or 0),
            "code": str(item.get("code") or ""),
            "name": str(item.get("name") or ""),
            "heat": float(item.get("rate") or 0),
            "change_pct": (
                float(item["rise_and_fall"])
                if item.get("rise_and_fall") is not None else None
            ),
            "rank_change": int(item.get("hot_rank_chg") or 0),
            "hot_tag": str(item.get("hot_tag") or ""),
            "reason": str(item.get("tag") or ""),
            "etf_name": str(item.get("etf_name") or ""),
            "etf_change_pct": (
                float(item["etf_rise_and_fall"])
                if item.get("etf_rise_and_fall") is not None else None
            ),
        })
    return rows


def fetch_ths_hot_rankings(top_n=30):
    """同花顺小时热股榜，以及概念、行业和指数板块榜。"""
    top_n = max(1, min(int(top_n), 30))
    result = {
        "source": "同花顺",
        "period": "1小时",
        "limit": top_n,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stocks": [],
        "concepts": [],
        "industries": [],
        "indices": [],
        "errors": [],
    }
    try:
        stock_list = _ths_hot_get(
            "stock", stock_type="a", type="hour", list_type="normal"
        ).get("stock_list") or []
        for item in stock_list[:top_n]:
            tag = item.get("tag") or {}
            topic = item.get("topic") or {}
            result["stocks"].append({
                "rank": int(item.get("order") or 0),
                "code": str(item.get("code") or ""),
                "name": str(item.get("name") or ""),
                "heat": float(item.get("rate") or 0),
                "change_pct": (
                    float(item["rise_and_fall"])
                    if item.get("rise_and_fall") is not None else None
                ),
                "rank_change": int(item.get("hot_rank_chg") or 0),
                "popularity_tag": str(tag.get("popularity_tag") or ""),
                "concepts": [
                    str(value) for value in (tag.get("concept_tag") or [])[:3]
                ],
                "reason_title": str(
                    item.get("analyse_title") or topic.get("title") or ""
                ).strip(),
                "reason": str(item.get("analyse") or "").strip(),
            })
    except Exception as error:
        result["errors"].append(f"热股榜：{type(error).__name__}")

    try:
        concept_list = _ths_hot_get("plate", type="concept").get("plate_list") or []
        result["concepts"] = _normalize_ths_plate_list(concept_list, top_n)
    except Exception as error:
        result["errors"].append(f"概念板块榜：{type(error).__name__}")

    try:
        plate_list = _ths_hot_get("plate", type="industry").get("plate_list") or []
        result["industries"] = _normalize_ths_plate_list(plate_list, top_n)
    except Exception as error:
        result["errors"].append(f"行业板块榜：{type(error).__name__}")

    try:
        result["indices"] = _ths_index_hot_get(top_n)
    except Exception as error:
        result["errors"].append(f"指数板块榜：{type(error).__name__}")
    return result


def _intraday_amount_map(code):
    """返回指数最近交易日的分钟累计成交额，单位为亿元。"""
    response = _session().get(
        TENCENT_INTRADAY_URL, params={"code": code}, timeout=20
    )
    response.raise_for_status()
    payload = (response.json().get("data") or {}).get(code) or {}
    days = {}
    for day in payload.get("data") or []:
        minute_amounts = {}
        for row in day.get("data") or []:
            parts = row.split()
            if len(parts) >= 4:
                minute_amounts[parts[0]] = float(parts[3]) / 1e8
        if minute_amounts:
            days[day["date"]] = minute_amounts
    if len(days) < 2:
        raise RuntimeError(f"{code} 分钟成交额不足两个交易日")
    return days


def fetch_same_time_turnover(now, current_amount_yi=None):
    """用沪指+深证综指比较今日和上一交易日同一分钟的累计成交额。

    腾讯分钟接口的成交额为当日累计值，且 09:30 的首个点已经包含集合
    竞价；因此它只能用于同一时点对比，不能直接按开盘后的分钟数线性外推。
    """
    market_days = {
        code: _intraday_amount_map(code)
        for code in ("sh000001", "sz399106")
    }
    common_dates = set.intersection(*(set(days) for days in market_days.values()))
    today = now.strftime("%Y%m%d")
    if today not in common_dates:
        raise RuntimeError("腾讯分钟成交额尚未返回今日数据")
    previous_dates = sorted(date for date in common_dates if date < today)
    if not previous_dates:
        raise RuntimeError("腾讯分钟成交额未返回上一交易日数据")
    previous_date = previous_dates[-1]
    previous_minutes = set.intersection(*(
        set(market_days[code][previous_date]) for code in market_days
    ))
    # 避开尚未走完的当前分钟。实时全市场报价可直接作为当前累计成交额，
    # 分钟指数数据只用于取得上一交易日的同时间参照，避免其延迟拖慢页面。
    target_minute = (now - timedelta(minutes=1)).strftime("%H%M")
    comparable_minutes = sorted(
        minute for minute in previous_minutes if minute <= target_minute
    )
    if not comparable_minutes:
        raise RuntimeError(f"当前 {target_minute} 尚无可比较的分钟成交额")
    comparison_minute = comparable_minutes[-1]
    if current_amount_yi is None:
        if not all(comparison_minute in market_days[code][today] for code in market_days):
            raise RuntimeError(f"今日 {comparison_minute} 尚无指数分钟成交额")
        current_yi = sum(
            market_days[code][today][comparison_minute] for code in market_days
        )
    else:
        current_yi = float(current_amount_yi)
    previous_yi = sum(
        market_days[code][previous_date][comparison_minute] for code in market_days
    )
    previous_close_yi = sum(
        market_days[code][previous_date][max(market_days[code][previous_date])]
        for code in market_days
    )
    if previous_yi <= 0:
        raise RuntimeError("上一交易日分钟成交额无法用于盘中成交额预估")
    delta_yi = current_yi - previous_yi
    delta_pct = delta_yi / previous_yi * 100 if previous_yi else float("nan")
    return {
        "same_time_amount_yi": current_yi,
        "previous_same_time_amount_yi": previous_yi,
        "same_time_amount_delta_yi": delta_yi,
        "same_time_amount_delta_pct": delta_pct,
        "same_time_volume_state": "放量" if delta_yi >= 0 else "缩量",
        "comparison_time": f"{comparison_minute[:2]}:{comparison_minute[2:]}",
        "comparison_previous_date": pd.to_datetime(previous_date).strftime("%Y-%m-%d"),
        "previous_close_amount_yi": previous_close_yi,
        "turnover_comparison_method": "昨日同一时点分钟成交额",
        "turnover_comparison_warning": "",
    }


def _fallback_same_time_turnover(now, current_amount_yi, turnover, cache_file, error):
    """分钟接口异常时，使用最近成功快照；无缓存时使用保守时段进度。"""
    completed = turnover.loc[
        turnover.date.dt.normalize() < pd.Timestamp(now.date()), ["date", "amount_yi"]
    ].dropna()
    if completed.empty:
        raise RuntimeError("无上一完整交易日成交额，无法降级预估") from error
    previous = completed.sort_values("date").iloc[-1]
    previous_full_yi = float(previous.amount_yi)
    minute = now.hour * 60 + now.minute
    anchors = [(565, .02), (570, .04), (600, .23), (630, .37), (660, .48),
               (690, .56), (780, .56), (810, .67), (840, .78), (870, .89), (900, 1.0)]
    progress = anchors[-1][1]
    for (lm, lr), (rm, rr) in zip(anchors, anchors[1:]):
        if minute <= anchors[0][0]:
            progress = anchors[0][1]
            break
        if lm <= minute <= rm:
            progress = lr + (minute - lm) / max(rm - lm, 1) * (rr - lr)
            break
    method = "交易时段进度曲线降级估算"
    cache_path = Path(cache_file)
    if cache_path.exists():
        try:
            cached = pd.read_csv(cache_path).tail(1).iloc[0]
            cached_time = pd.to_datetime(cached.get("snapshot_time"), errors="coerce")
            cached_previous = pd.to_numeric(cached.get("previous_same_time_amount_yi"), errors="coerce")
            cached_full = pd.to_numeric(cached.get("turnover_projection_base_yi"), errors="coerce")
            if (pd.notna(cached_time) and cached_time.date() == now.date()
                    and pd.notna(cached_previous) and cached_previous > 0
                    and pd.notna(cached_full) and cached_full > 0
                    and timedelta(0) <= now - cached_time.to_pydatetime() <= timedelta(minutes=20)):
                progress = min(1.0, max(.01, float(cached_previous / cached_full)))
                method = "最近成功快照成交进度降级估算"
        except (OSError, ValueError, TypeError, IndexError, pd.errors.ParserError):
            pass
    previous_yi = previous_full_yi * progress
    delta_yi = float(current_amount_yi) - previous_yi
    comparison_minute = (now - timedelta(minutes=1)).strftime("%H%M")
    return {
        "same_time_amount_yi": float(current_amount_yi),
        "previous_same_time_amount_yi": previous_yi,
        "same_time_amount_delta_yi": delta_yi,
        "same_time_amount_delta_pct": delta_yi / previous_yi * 100,
        "same_time_volume_state": "放量" if delta_yi >= 0 else "缩量",
        "comparison_time": f"{comparison_minute[:2]}:{comparison_minute[2:]}",
        "comparison_previous_date": pd.Timestamp(previous.date).strftime("%Y-%m-%d"),
        "previous_close_amount_yi": previous_full_yi,
        "turnover_comparison_method": method,
        "turnover_comparison_warning": f"{type(error).__name__}: {error}",
    }


def project_full_day_turnover(current_same_time_yi, previous_same_time_yi,
                              previous_full_day_yi):
    """按上一交易日同一时点的成交进度预估全天成交额。

    这会把集合竞价纳入与昨日相同的进度曲线，避免开盘初期把累计成交额按
    9:30 后的少量分钟错误放大。三个输入的单位均为亿元。
    """
    current_same_time_yi = float(current_same_time_yi)
    previous_same_time_yi = float(previous_same_time_yi)
    previous_full_day_yi = float(previous_full_day_yi)
    if current_same_time_yi < 0 or previous_same_time_yi <= 0:
        raise ValueError("同一时点成交额必须为非负值，且上一交易日成交额必须大于零")
    if previous_full_day_yi < previous_same_time_yi:
        raise ValueError("上一交易日全天成交额不能小于同一时点累计成交额")
    return current_same_time_yi / previous_same_time_yi * previous_full_day_yi


def fetch_index_snapshot(now):
    """获取五个主要指数的实时点位、涨跌幅和当日分钟走势。"""
    indexes = []
    today = now.strftime("%Y%m%d")
    for code, label in INDEX_SPECS:
        response = _session().get(
            TENCENT_INTRADAY_URL, params={"code": code}, timeout=20
        )
        response.raise_for_status()
        payload = (response.json().get("data") or {}).get(code) or {}
        quote = ((payload.get("qt") or {}).get(code)) or []
        if len(quote) < 33:
            raise RuntimeError(f"{label} 实时行情字段不完整")
        value = float(quote[3])
        previous_close = float(quote[4])
        change_pct = float(quote[32])
        recalculated_pct = (value / previous_close - 1) * 100
        if abs(recalculated_pct - change_pct) > 0.05:
            raise RuntimeError(f"{label} 涨跌幅校验不一致")
        day = next(
            (item for item in payload.get("data") or [] if item.get("date") == today),
            None,
        )
        points = []
        if day:
            for row in day.get("data") or []:
                parts = row.split()
                if len(parts) >= 2:
                    points.append(float(parts[1]))
        if len(points) < 2:
            points = [float(quote[4]), float(quote[3])]
        indexes.append({
            "code": code,
            "name": label,
            "value": value,
            "change_pct": change_pct,
            "quote_time": quote[30],
            "sparkline": points,
        })
    return indexes


def _history_state(kline):
    """为每只股票提取均线、新高低和昨日涨停/连板所需状态。"""
    states = []
    kline = kline.sort_values(["secid", "date"])
    for (secid, code), stock in kline.groupby(["secid", "code"], sort=False):
        stock = stock.tail(65)
        close = stock.close.to_numpy()
        high = stock.high.to_numpy()
        if len(close) < 2:
            continue
        growth = str(code).startswith(("300", "301", "688", "689"))
        limit = 19.5 if growth else 9.5

        def is_limit_up(index):
            if index <= 0:
                return False
            ret = (close[index] / close[index - 1] - 1) * 100
            high_ret = (high[index] / close[index - 1] - 1) * 100
            return high_ret >= limit and close[index] / high[index] >= 0.999 and ret >= limit

        previous_limit = is_limit_up(len(close) - 1)
        streak = 0
        for index in range(len(close) - 1, 0, -1):
            if not is_limit_up(index):
                break
            streak += 1
        states.append({
            "secid": secid,
            "hist19_count": min(len(close), 19),
            "hist19_sum": close[-19:].sum(),
            "hist19_max": close[-19:].max(),
            "hist19_min": close[-19:].min(),
            "hist59_count": min(len(close), 59),
            "hist59_sum": close[-59:].sum(),
            "previous_limit_up": previous_limit,
            "previous_limit_streak": streak,
        })
    return pd.DataFrame(states)


def _percentile_with_today(history, column, value, window=120):
    if column not in history or pd.isna(value):
        return 50.0
    values = pd.to_numeric(history[column], errors="coerce").dropna().tail(window - 1)
    combined = pd.concat([values, pd.Series([value])], ignore_index=True)
    return combined.rank(pct=True).iloc[-1] * 100


def build_intraday_snapshot(
    universe_file="market_stock_universe.csv",
    kline_file="market_stock_daily_cache.csv.gz",
    daily_file="market_sentiment_daily.csv",
    turnover_file="hs_a_share_turnover.csv",
    intraday_cache_file="market_sentiment_intraday.csv",
    workers=12,
):
    now = datetime.now()
    universe = pd.read_csv(universe_file, dtype={"code": str})
    quotes = fetch_market_snapshot(universe, workers)
    # 成交额历史文件是沪深 A 股口径，不能把北交所成交额混入同比和全天投影。
    hs_quotes = quotes.loc[quotes.secid.str.startswith(("sh", "sz"))]
    quote_amount_yi = float(hs_quotes.amount_wan.sum() / 10000)
    quote_timestamp = _latest_quote_timestamp(quotes, datetime.now())
    turnover = pd.read_csv(turnover_file, parse_dates=["date"]).sort_values("date")
    try:
        turnover_comparison = fetch_same_time_turnover(
            quote_timestamp, current_amount_yi=quote_amount_yi
        )
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
        turnover_comparison = _fallback_same_time_turnover(
            quote_timestamp, quote_amount_yi, turnover, intraday_cache_file, exc
        )
        print(
            "[WARN] minute turnover comparison degraded: "
            f"{turnover_comparison['turnover_comparison_method']}; {exc}"
        )
    cached_snapshot = None
    cache_path = Path(intraday_cache_file)
    if cache_path.exists():
        try:
            cached_frame = pd.read_csv(cache_path)
            if not cached_frame.empty:
                cached_snapshot = cached_frame.iloc[-1]
        except (OSError, ValueError, pd.errors.ParserError):
            pass

    try:
        dragon_tiger_top5 = fetch_dragon_tiger_top5(quote_timestamp)
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
        try:
            dragon_tiger_top5 = json.loads(
                cached_snapshot.get("dragon_tiger_top5_json") or "{}"
            ) if cached_snapshot is not None else {}
        except (TypeError, json.JSONDecodeError):
            dragon_tiger_top5 = {}
        if dragon_tiger_top5.get("stocks"):
            dragon_tiger_top5["status"] = "接口暂不可用，沿用上一轮"
        else:
            dragon_tiger_top5 = {
                "date": "",
                "stocks": [],
                "source": "东方财富龙虎榜",
                "status": "最近榜单暂不可用",
            }
        print(f"[WARN] dragon tiger board degraded to cache: {exc}")

    try:
        main_net_yi, fund_flow_time = fetch_main_fund_flow()
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
        main_net_yi = float(pd.to_numeric(
            cached_snapshot.get("main_net_yi"), errors="coerce"
        )) if cached_snapshot is not None else 0.0
        if not math.isfinite(main_net_yi):
            main_net_yi = 0.0
        fund_flow_time = str(
            cached_snapshot.get("fund_flow_time") or "暂不可用"
        ) if cached_snapshot is not None else "暂不可用"
        print(f"[WARN] main fund flow degraded to cache: {exc}")

    try:
        index_snapshot = fetch_index_snapshot(quote_timestamp)
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
        try:
            index_snapshot = json.loads(
                cached_snapshot.get("index_snapshot_json") or "[]"
            ) if cached_snapshot is not None else []
        except (TypeError, json.JSONDecodeError):
            index_snapshot = []
        print(f"[WARN] index snapshot degraded to cache: {exc}")

    try:
        industry_heatmap = fetch_industry_heatmap()
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
        try:
            industry_heatmap = json.loads(
                cached_snapshot.get("industry_heatmap_json") or "{}"
            ) if cached_snapshot is not None else {}
        except (TypeError, json.JSONDecodeError):
            industry_heatmap = {}
        print(f"[WARN] industry heatmap degraded to cache: {exc}")

    try:
        ths_hot_rankings = fetch_ths_hot_rankings()
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
        try:
            ths_hot_rankings = json.loads(
                cached_snapshot.get("ths_hot_rankings_json") or "{}"
            ) if cached_snapshot is not None else {}
        except (TypeError, json.JSONDecodeError):
            ths_hot_rankings = {}
        print(f"[WARN] hot rankings degraded to cache: {exc}")
    kline = pd.read_csv(kline_file, parse_dates=["date"], dtype={"code": str})
    states = _history_state(kline)
    # 停牌或当日尚无成交的证券常保留昨收和 0% 涨跌幅，不能归入“平盘”。
    # 主流行情终端的涨跌家数口径只统计当日已有实际成交的证券。
    active_quotes = quotes.loc[
        (quotes.volume_lot > 0) & (quotes.amount_wan > 0)
    ].copy()
    no_trade_count = len(quotes) - len(active_quotes)
    current = active_quotes.merge(states, on="secid", how="inner")
    # 涨跌分布只依赖实时行情及交易所涨跌停价，使用所有有效的沪深北报价；
    # 均线、昨日涨停等历史指标仍使用拥有日 K 状态的子集。
    distribution_market = active_quotes.copy()
    distribution_market["limit_up"] = (
        distribution_market.limit_up_price.notna()
        & (distribution_market.limit_up_price > 0)
        & (distribution_market.price >= distribution_market.limit_up_price * 0.999)
    )
    distribution_market["hit_limit_up"] = (
        distribution_market.limit_up_price.notna()
        & (distribution_market.limit_up_price > 0)
        & (distribution_market.high >= distribution_market.limit_up_price * 0.999)
    )
    distribution_market["break_board"] = (
        distribution_market.hit_limit_up & ~distribution_market.limit_up
    )
    distribution_market["limit_down"] = (
        distribution_market.limit_down_price.notna()
        & (distribution_market.limit_down_price > 0)
        & (distribution_market.price <= distribution_market.limit_down_price * 1.001)
    )

    current["up"] = current.change_pct > 0
    current["down"] = current.change_pct < 0
    current["above_ma20"] = (
        (current.hist19_count >= 19)
        & (current.price > (current.hist19_sum + current.price) / 20)
    )
    current["above_ma60"] = (
        (current.hist59_count >= 59)
        & (current.price > (current.hist59_sum + current.price) / 60)
    )
    current["new_high_20"] = (current.hist19_count >= 19) & (current.price >= current.hist19_max)
    current["new_low_20"] = (current.hist19_count >= 19) & (current.price <= current.hist19_min)
    valid_limit_up_price = current.limit_up_price.notna() & (current.limit_up_price > 0)
    valid_limit_down_price = current.limit_down_price.notna() & (current.limit_down_price > 0)
    current["hit_limit_up"] = (
        valid_limit_up_price & (current.high >= current.limit_up_price * 0.999)
    )
    current["limit_up"] = (
        valid_limit_up_price & (current.price >= current.limit_up_price * 0.999)
    )
    current["break_board"] = current.hit_limit_up & ~current.limit_up
    current["limit_down"] = (
        valid_limit_down_price & (current.price <= current.limit_down_price * 1.001)
    )
    current["advanced"] = current.previous_limit_up & current.limit_up
    current["today_streak"] = (
        (current.previous_limit_streak + 1).where(current.limit_up, 0)
    )

    valid = len(current)
    up_count, down_count = int(current.up.sum()), int(current.down.sum())
    ma20_valid = int((current.hist19_count >= 19).sum())
    ma60_valid = int((current.hist59_count >= 59).sum())
    new_high, new_low = int(current.new_high_20.sum()), int(current.new_low_20.sum())
    limit_up, break_board = int(current.limit_up.sum()), int(current.break_board.sum())
    limit_down = int(current.limit_down.sum())
    previous_limit_count = int(current.previous_limit_up.sum())
    advanced = int(current.advanced.sum())
    distribution_limit_up = int(distribution_market.limit_up.sum())
    distribution_limit_down = int(distribution_market.limit_down.sum())
    distribution_break_board = int(distribution_market.break_board.sum())
    regular = ~distribution_market.limit_up & ~distribution_market.limit_down
    distribution = [
        {"label": "涨停", "count": distribution_limit_up, "side": "up"},
        {
            "label": ">8%", "count": int(
                (regular & (distribution_market.change_pct > 8)).sum()
            ), "side": "up",
        },
        {
            "label": "5–8%", "count": int(
                (regular & (distribution_market.change_pct > 5)
                 & (distribution_market.change_pct <= 8)).sum()
            ), "side": "up",
        },
        {
            "label": "2–5%", "count": int(
                (regular & (distribution_market.change_pct > 2)
                 & (distribution_market.change_pct <= 5)).sum()
            ), "side": "up",
        },
        {
            "label": "0–2%", "count": int(
                (regular & (distribution_market.change_pct > 0)
                 & (distribution_market.change_pct <= 2)).sum()
            ), "side": "up",
        },
        {
            "label": "平盘", "count": int(
                (regular & (distribution_market.change_pct == 0)).sum()
            ),
            "side": "flat",
        },
        {
            "label": "0至-2%", "count": int(
                (regular & (distribution_market.change_pct < 0)
                 & (distribution_market.change_pct >= -2)).sum()
            ), "side": "down",
        },
        {
            "label": "-2至-5%", "count": int(
                (regular & (distribution_market.change_pct < -2)
                 & (distribution_market.change_pct >= -5)).sum()
            ), "side": "down",
        },
        {
            "label": "-5至-8%", "count": int(
                (regular & (distribution_market.change_pct < -5)
                 & (distribution_market.change_pct >= -8)).sum()
            ), "side": "down",
        },
        {
            "label": "<-8%", "count": int(
                (regular & (distribution_market.change_pct < -8)).sum()
            ), "side": "down",
        },
        {"label": "跌停", "count": distribution_limit_down, "side": "down"},
    ]
    if sum(item["count"] for item in distribution) != len(distribution_market):
        raise RuntimeError("涨跌幅分布分箱未覆盖全部有效股票")

    daily = pd.read_csv(daily_file, parse_dates=["date"]).sort_values("date")
   # 当前累计成交额取全市场实时报价汇总；昨日同时间比较仍使用指数分钟线。
    actual_amount_yi = quote_amount_yi
    previous_date = pd.Timestamp(turnover_comparison["comparison_previous_date"])
    previous_official = turnover.loc[
        turnover.date.dt.normalize() == previous_date,
        "amount_yi",
    ]
    # 优先使用两所正式收盘口径；缓存意外缺失时，才降级为腾讯上一交易日收盘值。
    previous_full_day_yi = (
        float(previous_official.iloc[-1])
        if not previous_official.empty
        else float(turnover_comparison["previous_close_amount_yi"])
    )
    projected_amount_yi = project_full_day_turnover(
        actual_amount_yi,
        turnover_comparison["previous_same_time_amount_yi"],
        previous_full_day_yi,
    )
    projected_turnover = pd.concat(
        [
            turnover.loc[
                turnover.date.dt.normalize() != pd.Timestamp(now.date()),
                ["date", "amount_yi"],
            ],
            pd.DataFrame([{
                "date": pd.Timestamp(now.date()),
                "amount_yi": projected_amount_yi,
            }]),
        ],
        ignore_index=True,
    )
    liquidity_snapshot = turnover_sentiment(projected_turnover).iloc[-1]
    ma20_yi = float(liquidity_snapshot.ma20_yi)
    vs_ma20 = float(liquidity_snapshot.vs_ma20)
    liquidity_score = float(liquidity_snapshot.liquidity_score)

    advance_ratio = up_count / max(up_count + down_count, 1) * 100
    above_ma20 = current.above_ma20.sum() / max(ma20_valid, 1) * 100
    above_ma60 = current.above_ma60.sum() / max(ma60_valid, 1) * 100
    new_high_low = new_high / max(new_high + new_low, 1) * 100
    net_advances = up_count - down_count
    ad10 = daily.net_advances.dropna().tail(9).sum() + net_advances
    ad_score = _percentile_with_today(daily, "ad_10d", ad10)
    breadth_score = (
        advance_ratio * 0.30 + above_ma20 * 0.25 + above_ma60 * 0.15
        + new_high_low * 0.15 + ad_score * 0.15
    )

    mean_return = float(current.change_pct.mean())
    median_return = float(current.change_pct.median())
    strong_up_share = float((current.change_pct >= 2).mean() * 100)
    strong_down_share = float((current.change_pct <= -2).mean() * 100)
    strong_net_share = strong_up_share - strong_down_share
    index_equal_weight_return = float(pd.Series([
        item.get("change_pct") for item in index_snapshot
    ]).dropna().mean())
    momentum_raw = {
        "mean_return_pct": mean_return,
        "median_return_pct": median_return,
        "strong_net_share_pct": strong_net_share,
        "index_equal_weight_return_pct": index_equal_weight_return,
    }
    momentum_weights = {
        "mean_return_pct": 0.30,
        "median_return_pct": 0.25,
        "strong_net_share_pct": 0.20,
        "index_equal_weight_return_pct": 0.25,
    }
    momentum_scores = {
        key + "_score": _percentile_with_today(daily, key, value)
        for key, value in momentum_raw.items()
    }
    momentum_score = sum(
        momentum_scores[key + "_score"] * weight
        for key, weight in momentum_weights.items()
    )

    previous_limit_return = current.loc[current.previous_limit_up, "change_pct"].median()
    advancement_rate = advanced / max(previous_limit_count, 1) * 100
    seal_success = limit_up / max(limit_up + break_board, 1) * 100
    limit_balance = limit_up - limit_down
    max_streak = int(current.today_streak.max())
    short_raw = {
        "previous_limit_return_median_pct": previous_limit_return,
        "advancement_rate_pct": advancement_rate,
        "seal_success_pct": seal_success,
        "limit_up_down_balance": limit_balance,
        "max_limit_streak": max_streak,
    }
    short_weights = {
        "previous_limit_return_median_pct": 0.30,
        "advancement_rate_pct": 0.20,
        "seal_success_pct": 0.20,
        "limit_up_down_balance": 0.15,
        "max_limit_streak": 0.15,
    }
    short_scores = {
        key + "_score": _percentile_with_today(daily, key, value)
        for key, value in short_raw.items()
    }
    short_term_score = sum(short_scores[key + "_score"] * weight
                           for key, weight in short_weights.items())

    # 同时保留外盘/内盘主动成交差额用于数据质量对照，但不参与资金风险偏好评分。
    active_trade_proxy_yi = (
        ((current.outer_volume_lot - current.inner_volume_lot) * current.price * 100).sum()
        / 1e8
    )
    main_net_ratio_pct = main_net_yi / max(actual_amount_yi, 1) * 100
    # 净流入占成交额 +5% 对应100分，-5%对应0分，中性为50分。
    capital_score = max(0, min(100, 50 + 10 * main_net_ratio_pct))
    liquidity_state = (
        "明显缩量" if vs_ma20 < 0.75 else "缩量" if vs_ma20 < 0.95
        else "常态" if vs_ma20 <= 1.10 else "温和放量" if vs_ma20 <= 1.35 else "显著放量"
    )

    row = {
        "date": pd.Timestamp(now.date()),
        "snapshot_time": quote_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "data_status": "盘中暂估",
        "valid_stock_count": valid,
        "distribution_stock_count": len(distribution_market),
        "distribution_no_trade_count": no_trade_count,
        "distribution_up_count": int((distribution_market.change_pct > 0).sum()),
        "distribution_down_count": int((distribution_market.change_pct < 0).sum()),
        "quote_amount_yi": quote_amount_yi,
        "actual_amount_yi": actual_amount_yi,
        "actual_amount_time": quote_timestamp.strftime("%H:%M:%S"),
        "projected_amount_yi": projected_amount_yi,
        "amount_yi": projected_amount_yi,
        "turnover_projection_method": turnover_comparison.get(
            "turnover_comparison_method", "昨日同一时点成交进度校准"
        ),
        "turnover_projection_base_date": previous_date.strftime("%Y-%m-%d"),
        "turnover_projection_base_yi": previous_full_day_yi,
        "ma20_yi": ma20_yi,
        "vs_ma20": vs_ma20,
        "previous_ma5_yi": liquidity_snapshot.previous_ma5_yi,
        "vs_previous_ma5": liquidity_snapshot.vs_previous_ma5,
        "liquidity_level_score": liquidity_snapshot.liquidity_level_score,
        "liquidity_relative_score": liquidity_snapshot.liquidity_relative_score,
        "liquidity_acceleration_score": liquidity_snapshot.liquidity_acceleration_score,
        "liquidity_component_coverage_pct": (
            liquidity_snapshot.liquidity_component_coverage_pct
        ),
        "liquidity_state": liquidity_state,
        "liquidity_score": liquidity_score,
        "up_count": up_count,
        "down_count": down_count,
        "advance_ratio_pct": advance_ratio,
        "above_ma20_pct": above_ma20,
        "above_ma60_pct": above_ma60,
        "new_high_20_count": new_high,
        "new_low_20_count": new_low,
        "net_advances": net_advances,
        "ad_10d": ad10,
        "ad_trend_score": ad_score,
        "breadth_score": breadth_score,
        "mean_return_pct": mean_return,
        "median_return_pct": median_return,
        "strong_up_share_pct": strong_up_share,
        "strong_down_share_pct": strong_down_share,
        "strong_net_share_pct": strong_net_share,
        "index_equal_weight_return_pct": index_equal_weight_return,
        **momentum_scores,
        "momentum_component_coverage_pct": 100.0,
        "momentum_score": momentum_score,
        "limit_up_count": limit_up,
        "break_board_count": break_board,
        "limit_down_count": limit_down,
        "distribution_limit_up_count": distribution_limit_up,
        "distribution_break_board_count": distribution_break_board,
        "distribution_limit_down_count": distribution_limit_down,
        "previous_limit_up_count": previous_limit_count,
        "advanced_count": advanced,
        **short_raw,
        **short_scores,
        "short_term_score": short_term_score,
        "main_net_yi": main_net_yi,
        "main_net_ratio_pct": main_net_ratio_pct,
        "fund_flow_time": fund_flow_time,
        "active_trade_proxy_yi": active_trade_proxy_yi,
        "capital_score": capital_score,
        "capital_proxy_method": "沪指与深成指分钟主力净流入合计÷实时成交额",
        "index_snapshot_json": json.dumps(
            index_snapshot, ensure_ascii=False, separators=(",", ":")
        ),
        "change_distribution_json": json.dumps(
            distribution, ensure_ascii=False, separators=(",", ":")
        ),
        "industry_heatmap_json": json.dumps(
            industry_heatmap, ensure_ascii=False, separators=(",", ":")
        ),
        "ths_hot_rankings_json": json.dumps(
            ths_hot_rankings, ensure_ascii=False, separators=(",", ":")
        ),
        "dragon_tiger_top5_json": json.dumps(
            dragon_tiger_top5, ensure_ascii=False, separators=(",", ":")
        ),
        "flat_count": int((distribution_market.change_pct == 0).sum()),
        "limit_up_down_ratio": limit_up / limit_down if limit_down else float("nan"),
        "distribution_limit_up_down_ratio": (
            distribution_limit_up / distribution_limit_down
            if distribution_limit_down else float("nan")
        ),
        "northbound_net_yi": float("nan"),
        "northbound_status": "盘中不披露",
        "northbound_note": (
            "沪深股通自2024-05-13起不再披露盘中实时买入、卖出及净流入"
        ),
        **turnover_comparison,
    }
    return pd.DataFrame([row])


def refresh_completed_turnover(
    turnover_file: str | Path = "hs_a_share_turnover.csv",
) -> pd.DataFrame:
    """启动盘中任务前补齐此前完整交易日，避免历史成交额图出现断档。"""
    path = Path(turnover_file)
    if path.exists():
        cached = pd.read_csv(path, parse_dates=["date"])
        start = cached.date.min()
    else:
        start = pd.Timestamp.today().normalize() - pd.Timedelta(days=365)
    completed_end = pd.Timestamp.today().normalize() - pd.Timedelta(days=1)
    refreshed = hs_a_share_turnover(start, completed_end, cache_path=path)
    if refreshed.empty:
        raise RuntimeError("未取得任何已完成交易日的成交额数据")
    return refreshed


def refresh_completed_daily_sentiment(
    turnover_file: str | Path = "hs_a_share_turnover.csv",
    daily_file: str | Path = "market_sentiment_daily.csv",
    universe_cache: str | Path = "market_stock_universe.csv",
    kline_cache: str | Path = "market_stock_daily_cache.csv.gz",
    index_cache: str | Path = "market_index_daily_cache.csv",
    workers: int = 12,
) -> pd.Timestamp | None:
    """成交额新增完整交易日时，同步补齐该日的日度情绪分项。

    仪表盘允许缺失分项并按可用权重归一化，但历史行若只剩量能一项，
    量能分会被误显示成“综合分”。因此启动盘中任务时先检查上一完整
    交易日是否已有广度、短线和动量；缺失时才触发日 K 刷新。
    """
    turnover = pd.read_csv(turnover_file, parse_dates=["date"])
    if turnover.empty:
        return None
    latest_date = turnover.date.max().normalize()
    required = ["breadth_score", "short_term_score", "momentum_score"]
    daily_path = Path(daily_file)
    if daily_path.exists():
        daily = pd.read_csv(daily_path, parse_dates=["date"])
        latest = daily.loc[daily.date.dt.normalize() == latest_date]
        if (
            not latest.empty
            and all(column in latest for column in required)
            and latest.iloc[-1][required].notna().all()
        ):
            return None

    from market_breadth_short_term import (
        calculate_daily_indicators,
        tencent_a_share_universe,
        tencent_index_kline,
        tencent_market_kline,
        update_dashboard_input,
    )

    universe = tencent_a_share_universe(
        universe_cache, workers=min(workers, 16), refresh=False
    )
    kline = tencent_market_kline(
        universe, latest_date, kline_cache, bars=380, workers=workers
    )
    index_kline = tencent_index_kline(
        latest_date, index_cache, bars=380, force=False
    )
    indicators = calculate_daily_indicators(kline, index_kline)
    result = update_dashboard_input(indicators, turnover_file, daily_file)

    # 融资融券通常晚于行情数据发布；拿不到时保留其他四项，不阻断仪表盘。
    try:
        from market_capital_risk_appetite import update_dashboard_input as update_capital

        result = update_capital(turnover_file, daily_file)
    except Exception as exc:
        print(f"[WARN] completed capital data refresh skipped: {exc}")

    latest = result.loc[result.date.dt.normalize() == latest_date]
    if latest.empty or not latest.iloc[-1][required].notna().all():
        raise RuntimeError(
            f"{latest_date:%Y-%m-%d} 日度情绪分项补写后仍不完整"
        )
    return latest_date


def refresh_market_universe(
    universe_file: str | Path = "market_stock_universe.csv",
    workers: int = 12,
) -> pd.DataFrame:
    """每天刷新一次沪深北股票池，并自动迁移旧的沪深非 ST 口径缓存。"""
    from market_breadth_short_term import tencent_a_share_universe

    path = Path(universe_file)
    refresh = not path.exists()
    if path.exists():
        cached = pd.read_csv(path, dtype={"code": str})
        cache_date = datetime.fromtimestamp(path.stat().st_mtime).date()
        refresh = (
            cache_date < datetime.now().date()
            or not cached.secid.astype(str).str.startswith("bj").any()
        )
    return tencent_a_share_universe(
        path, workers=min(workers, 16), refresh=refresh
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="market_sentiment_intraday.csv")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument(
        "--watch-seconds", type=int, default=0,
        help="持续刷新间隔秒数；0表示只刷新一次，建议盘中使用60",
    )
    parser.add_argument(
        "--dashboard-output", default="market_sentiment_dashboard.html"
    )
    parser.add_argument("--turnover", default="hs_a_share_turnover.csv")
    parser.add_argument("--universe-cache", default="market_stock_universe.csv")
    args = parser.parse_args()
    universe = refresh_market_universe(args.universe_cache, args.workers)
    print(f"market universe ready: {len(universe)} stocks")
    refreshed_turnover = refresh_completed_turnover(args.turnover)
    print(
        "completed turnover refreshed: "
        f"{refreshed_turnover.date.max():%Y-%m-%d}; rows={len(refreshed_turnover)}"
    )
    backfilled_date = refresh_completed_daily_sentiment(
        turnover_file=args.turnover,
        universe_cache=args.universe_cache,
        workers=args.workers,
    )
    if backfilled_date is not None:
        print(f"completed daily sentiment backfilled: {backfilled_date:%Y-%m-%d}")
    refresh_interval = max(15, args.watch_seconds)
    next_refresh_at = time_module.monotonic()
    while True:
        try:
            snapshot = build_intraday_snapshot(
                universe_file=args.universe_cache,
                workers=args.workers,
                turnover_file=args.turnover,
                intraday_cache_file=args.output,
            )
            output_path = Path(args.output)
            output_tmp = output_path.with_name(f".{output_path.name}.tmp")
            snapshot.to_csv(output_tmp, index=False, encoding="utf-8-sig")
            output_tmp.replace(output_path)
            from market_sentiment_dashboard import build_daily_sentiment, render_dashboard
            render_dashboard(
                build_daily_sentiment(intraday_file=args.output),
                args.dashboard_output,
            )
            row = snapshot.iloc[0]
            print(
                f"intraday snapshot written: {args.output}; {row.snapshot_time}; "
                f"liquidity={row.liquidity_score:.2f}, breadth={row.breadth_score:.2f}, "
                f"short={row.short_term_score:.2f}, momentum={row.momentum_score:.2f}, "
                f"capital-proxy={row.capital_score:.2f}, "
                f"main-net={row.main_net_yi:+.2f} yi; "
                f"dashboard={args.dashboard_output}"
            )
        except Exception as exc:
            if args.watch_seconds <= 0:
                raise
            print(f"[WARN] intraday refresh failed: {exc}")
        if args.watch_seconds <= 0:
            break
        # 固定频率调度：以每轮“开始时间”为基准，而不是在接口全部完成后
        # 再等待 60 秒。这样接口耗时不会被叠加到刷新间隔中。
        next_refresh_at += refresh_interval
        now_monotonic = time_module.monotonic()
        if next_refresh_at <= now_monotonic:
            missed_intervals = (
                int((now_monotonic - next_refresh_at) // refresh_interval) + 1
            )
            next_refresh_at += missed_intervals * refresh_interval
            print(
                "[WARN] refresh cycle exceeded interval; "
                f"skipped {missed_intervals} overlapping cycle(s)"
            )
        time_module.sleep(max(0.0, next_refresh_at - time_module.monotonic()))


if __name__ == "__main__":
    main()
