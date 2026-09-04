"""以沪深两市融资净加仓强度构建日度资金风险偏好分项。"""
from __future__ import annotations

import argparse
from pathlib import Path
import random
import time

import pandas as pd
import requests


EASTMONEY_DATA_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
MARKETS = {"sh": "007", "sz": "001"}


def _market_margin_history(market: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """从东财交易所汇总表读取单一市场最近约500个交易日。"""
    market_code = MARKETS[market]
    params = {
        "reportName": "RPTA_WEB_RZRQ_LSSH",
        "columns": "ALL",
        "source": "WEB",
        "sortColumns": "DIM_DATE",
        "sortTypes": "-1",
        "pageNumber": "1",
        "pageSize": "500",
        "filter": f'(SCDM="{market_code}")',
    }
    response = requests.get(
        EASTMONEY_DATA_URL,
        params=params,
        headers={
            "User-Agent": UA,
            "Referer": "https://data.eastmoney.com/rzrq/total/all.10.html",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    rows = (payload.get("result") or {}).get("data") or []
    if not payload.get("success") or not rows:
        raise ValueError(f"东财未返回{market.upper()}市场融资汇总数据：{payload.get('message')}")
    raw = pd.DataFrame(rows)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["DIM_DATE"], errors="coerce").dt.normalize(),
            f"{market}_margin_balance_yi": pd.to_numeric(raw["RZYE"], errors="coerce") / 1e8,
            f"{market}_margin_buy_yi": pd.to_numeric(raw["RZMRE"], errors="coerce") / 1e8,
        }
    ).dropna().sort_values("date").drop_duplicates("date")
    return frame[frame["date"].between(start, end)].reset_index(drop=True)


def hs_margin_risk_appetite(start: str | pd.Timestamp, end: str | pd.Timestamp) -> pd.DataFrame:
    """返回沪深两市融资余额、净加仓及其60日历史分位（0–100分）。"""
    start_date = pd.Timestamp(start).normalize()
    end_date = pd.Timestamp(end).normalize()
    sh = _market_margin_history("sh", start_date, end_date)
    time.sleep(1.0 + random.random() * 0.3)
    sz = _market_margin_history("sz", start_date, end_date)
    df = sh.merge(sz, on="date", how="inner", validate="one_to_one")
    if df.empty:
        raise ValueError(f"未获得 {start_date.date()}–{end_date.date()} 的沪深融资汇总交集")
    df["hs_margin_balance_yi"] = df["sh_margin_balance_yi"] + df["sz_margin_balance_yi"]
    df["hs_margin_buy_yi"] = df["sh_margin_buy_yi"] + df["sz_margin_buy_yi"]
    # 融资余额变动 = 融资买入 - 融资偿还，直接反映杠杆资金的净加/减仓。
    df["hs_margin_net_buy_yi"] = df["hs_margin_balance_yi"].diff()
    df["capital_score"] = df["hs_margin_net_buy_yi"].rolling(60, min_periods=20).rank(pct=True) * 100
    return df


def update_dashboard_input(
    turnover_file="hs_a_share_turnover.csv",
    output="market_sentiment_daily.csv",
    margin_output="market_margin_balance.csv",
) -> pd.DataFrame:
    turnover = pd.read_csv(turnover_file, parse_dates=["date"])
    end_date = turnover.date.max().normalize()
    start_date = end_date - pd.DateOffset(years=2)
    margin = hs_margin_risk_appetite(start_date, end_date)
    margin.to_csv(margin_output, index=False, encoding="utf-8-sig")
    new = turnover[["date"]].merge(margin, on="date", how="left")
    path = Path(output)
    if path.exists():
        old = pd.read_csv(path, parse_dates=["date"])
        legacy_columns = [
            "sse_margin_balance_yi",
            "sse_margin_buy_yi",
            "sse_margin_net_buy_yi",
        ]
        old = old.drop(
            columns=[*legacy_columns, *[c for c in new if c != "date" and c in old]],
            errors="ignore",
        )
        new = old.merge(new, on="date", how="outer")
    new.sort_values("date").to_csv(path, index=False, encoding="utf-8-sig")
    return new


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--turnover", default="hs_a_share_turnover.csv")
    parser.add_argument("--output", default="market_sentiment_daily.csv")
    parser.add_argument("--margin-output", default="market_margin_balance.csv")
    args = parser.parse_args()
    result = update_dashboard_input(args.turnover, args.output, args.margin_output)
    print(
        f"capital-risk data written: {args.output}; history={args.margin_output}; "
        f"{result['capital_score'].notna().sum()} scored days"
    )
