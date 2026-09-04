"""沪深 A 股日成交额与交易情绪。

统计口径：上交所主板 A + 科创板，深交所主板 A + 创业板 A；不含 B 股、
基金、债券与北交所。数据来自上交所/深交所收盘后的公开日度统计，因而可
回溯历史，但当日数据须待收盘后才会完整。

依赖：requests、pandas（不需要 akshare 或 openpyxl）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from time import sleep
from typing import Iterable
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pandas as pd
import requests


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_URL = "https://www.szse.cn/api/report/ShowReport"


def _number(value: object) -> float:
    """把交易所返回的带逗号数字转换为 float。"""
    return float(str(value).replace(",", "").strip())


def _szse_text(value: str) -> str:
    """修正深交所 xlsx 中偶发的“GBK 字节被当作 UTF-8”文本。"""
    try:
        return value.encode("utf-8").decode("gbk")
    except UnicodeError:
        return value


def _xlsx_rows(content: bytes) -> list[list[str]]:
    """读取深交所 xlsx 的第一张表，仅用标准库以保持零额外依赖。"""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with ZipFile(BytesIO(content)) as book:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            root = ET.fromstring(book.read("xl/sharedStrings.xml"))
            shared = [_szse_text("".join(t.text or "" for t in item.iter(ns + "t")))
                      for item in root.findall(ns + "si")]
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))

    rows: list[list[str]] = []
    for row in sheet.findall(".//" + ns + "row"):
        values: list[str] = []
        for cell in row.findall(ns + "c"):
            kind = cell.get("t")
            raw = cell.findtext(ns + "v", default="")
            if kind == "s" and raw:
                values.append(shared[int(raw)])
            elif kind == "inlineStr":
                values.append(_szse_text("".join(t.text or "" for t in cell.iter(ns + "t"))))
            else:
                values.append(raw)
        if values:
            rows.append(values)
    return rows


def _sse_a_amount_yi(session: requests.Session, day: pd.Timestamp) -> float:
    """上交所主板 A(01) + 科创板(03) 成交额，单位：亿元。"""
    params = {
        "sqlId": "COMMON_SSE_SJ_GPSJ_CJGK_MRGK_C",
        "PRODUCT_CODE": "01,02,03,11,17",
        "type": "inParams",
        "SEARCH_DATE": day.strftime("%Y-%m-%d"),
    }
    r = session.get(SSE_URL, params=params, headers={"Referer": "https://www.sse.com.cn/"}, timeout=20)
    r.raise_for_status()
    items = r.json().get("result") or []
    by_code = {str(item.get("PRODUCT_CODE")): item for item in items}
    try:
        return _number(by_code["01"]["TRADE_AMT"]) + _number(by_code["03"]["TRADE_AMT"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"上交所 {day:%Y-%m-%d} 无完整 A 股成交额") from exc


def _szse_a_amount_yi(session: requests.Session, day: pd.Timestamp) -> float:
    """深交所主板 A + 创业板 A 成交额；原表为元，返回亿元。"""
    params = {
        "SHOWTYPE": "xlsx",
        "CATALOGID": "1803_sczm",
        "TABKEY": "tab1",
        "txtQueryDate": day.strftime("%Y-%m-%d"),
        "random": "0.39339437497296137",
    }
    r = session.get(SZSE_URL, params=params, timeout=25)
    r.raise_for_status()
    rows = _xlsx_rows(r.content)
    if not rows:
        raise ValueError(f"深交所 {day:%Y-%m-%d} 未返回有效统计表")
    header = rows[0]
    # 少数环境下 xlsx 的 GBK 文本会半乱码；表格列位置始终为类别、数量、成交额。
    name_i = header.index("证券类别") if "证券类别" in header else 0
    amount_i = header.index("成交金额") if "成交金额" in header else 2
    amounts: dict[str, float] = {}
    for row in rows[1:]:
        if len(row) > max(name_i, amount_i):
            name = row[name_i].replace(" ", "").strip()
            amounts[name] = _number(row[amount_i])
    try:
        return (amounts["主板A股"] + amounts["创业板A股"]) / 1e8
    except KeyError as exc:
        # 兼容上述乱码：深交所固定把股票总计后的第 1、3 行分别列为主板 A、创业板 A。
        stock_row = next((i for i, row in enumerate(rows[1:], 1)
                          if len(row) > name_i and row[name_i].strip() == "股票"), None)
        if stock_row is None or len(rows) <= stock_row + 3:
            raise ValueError(f"深交所 {day:%Y-%m-%d} 缺少主板A股或创业板A股统计") from exc
        try:
            return (_number(rows[stock_row + 1][amount_i]) +
                    _number(rows[stock_row + 3][amount_i])) / 1e8
        except (IndexError, ValueError) as fallback_exc:
            raise ValueError(f"深交所 {day:%Y-%m-%d} 缺少主板A股或创业板A股统计") from fallback_exc


def hs_a_share_turnover(
    start: str | pd.Timestamp,
    end: str | pd.Timestamp | None = None,
    cache_path: str | Path = "hs_a_share_turnover.csv",
    pause: float = 0.25,
    workers: int = 1,
) -> pd.DataFrame:
    """获取沪深 A 股每日成交额，优先读缓存并补抓缺失交易日。

    返回列：date、sse_amount_yi、szse_amount_yi、amount_yi、scope。
    首次拉取一年的历史通常需要数分钟；交易所数据在收盘统计完成后才可用。
    """
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end or pd.Timestamp.today()).normalize()
    if end < start:
        raise ValueError("end 不能早于 start")
    path = Path(cache_path)
    columns = ["date", "sse_amount_yi", "szse_amount_yi", "amount_yi", "scope"]
    if path.exists():
        cached = pd.read_csv(path, parse_dates=["date"])
        cached = cached.reindex(columns=columns)
    else:
        cached = pd.DataFrame(columns=columns)

    # 工作日只是候选集；休市日会被交易所返回的空数据自然跳过。
    known = set(pd.to_datetime(cached["date"]).dt.normalize()) if not cached.empty else set()
    targets: Iterable[pd.Timestamp] = (d for d in pd.bdate_range(start, end) if d not in known)

    def fetch_day(day: pd.Timestamp) -> dict | None:
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
        try:
            sse = _sse_a_amount_yi(session, day)
            sleep(pause)
            szse = _szse_a_amount_yi(session, day)
        except (requests.RequestException, ValueError, KeyError, ET.ParseError):
            # 周末、节假日、尚未收盘及临时接口异常均不写入缓存，方便下次重试。
            sleep(pause)
            return None
        record = {"date": day, "sse_amount_yi": sse, "szse_amount_yi": szse,
                  "amount_yi": sse + szse, "scope": "沪深A股（不含北交所）"}
        sleep(pause)
        return record

    workers = max(1, int(workers))
    if workers == 1:
        records = (fetch_day(day) for day in targets)
    else:
        # 仅对两所官方统计做小并发；默认单线程，避免把公共接口当成高频行情源。
        with ThreadPoolExecutor(max_workers=min(workers, 4)) as pool:
            records = list(pool.map(fetch_day, targets))
    fresh = [item for item in records if item is not None]

    out = pd.concat([cached, pd.DataFrame(fresh)], ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    out = out.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    out.to_csv(path, index=False, encoding="utf-8-sig")
    return out[(out["date"] >= start) & (out["date"] <= end)].copy()


def turnover_sentiment(turnover: pd.DataFrame, lookback: int = 120) -> pd.DataFrame:
    """为成交额序列添加量能情绪指标；成交额本身不代表涨跌方向。

    量能分兼顾三个维度：
    - 50%：当日成交额在近 ``lookback`` 日的历史分位，识别持续高成交平台；
    - 30%：当日成交额相对20日均额，0.70×至1.40×线性映射为0–100分；
    - 20%：当日成交额相对前5日均额的倍数在近 ``lookback`` 日的历史分位，
      识别短期成交加速度。
    """
    required = {"date", "amount_yi"}
    if not required.issubset(turnover.columns):
        raise ValueError("turnover 必须含 date 和 amount_yi 列")
    df = turnover.copy().sort_values("date").reset_index(drop=True)
    amount = pd.to_numeric(df["amount_yi"], errors="coerce")
    df["ma5_yi"] = amount.rolling(5, min_periods=3).mean()
    df["ma20_yi"] = amount.rolling(20, min_periods=10).mean()
    df["ma60_yi"] = amount.rolling(60, min_periods=30).mean()
    df["previous_ma5_yi"] = amount.shift(1).rolling(5, min_periods=3).mean()
    df["vs_previous_ma5"] = amount / df["previous_ma5_yi"]
    df["vs_ma20"] = amount / df["ma20_yi"]
    df["change_5d_pct"] = amount.pct_change(5) * 100
    df["percentile_120"] = amount.rolling(lookback, min_periods=20).rank(pct=True) * 100
    df["liquidity_level_score"] = df["percentile_120"]
    df["liquidity_relative_score"] = (
        (df["vs_ma20"] - 0.70) / 0.70 * 100
    ).clip(0, 100)
    df["liquidity_acceleration_score"] = (
        df["vs_previous_ma5"].rolling(lookback, min_periods=20).rank(pct=True) * 100
    )
    liquidity_components = {
        "liquidity_level_score": 0.50,
        "liquidity_relative_score": 0.30,
        "liquidity_acceleration_score": 0.20,
    }
    available_weight = sum(
        df[column].notna() * weight for column, weight in liquidity_components.items()
    )
    weighted_score = sum(
        df[column].fillna(0) * weight for column, weight in liquidity_components.items()
    )
    df["liquidity_score"] = (weighted_score / available_weight).where(available_weight > 0)
    df["liquidity_component_coverage_pct"] = available_weight * 100

    def label(ratio: float) -> str | None:
        if pd.isna(ratio):
            return None
        if ratio < 0.75:
            return "明显缩量"
        if ratio < 0.95:
            return "缩量"
        if ratio <= 1.10:
            return "常态"
        if ratio <= 1.35:
            return "温和放量"
        return "显著放量"

    df["liquidity_state"] = df["vs_ma20"].map(label)
    return df


if __name__ == "__main__":
    raw = hs_a_share_turnover(pd.Timestamp.today() - pd.Timedelta(days=365))
    result = turnover_sentiment(raw)
    print(result.tail(10).to_string(index=False))
