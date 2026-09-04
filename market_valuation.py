"""A 股宽基估值：申万 A 指代理万得全 A + 中证官方指数 PE-TTM。

数据口径与 WorkBuddy 参考实现一致：
- 万得全 A：申万 A 指（801003）整体法滚动市盈率代理；
- 沪深 300 / 中证全指：中证指数官网 index-perf 的 peg 字段。

原始日频缓存和派生 JSON 均保存在本项目目录，正常情况下每日最多刷新一次。
"""
from __future__ import annotations

import bisect
import base64
import concurrent.futures as futures
import datetime as dt
import json
from pathlib import Path

import pandas as pd
import requests
import urllib3


ROOT = Path(__file__).resolve().parent
DEFAULT_CACHE = ROOT / "market_valuation_payload.json"
DEFAULT_REPORT = ROOT / "a-share-pe-valuation-report.html"
HISTORY_TEMPLATE = ROOT / "market_valuation_history_template.html"
BACKTEST_TEMPLATE = ROOT / "market_valuation_backtest_template.html"
SWA_CACHE = ROOT / "market_valuation_swa.csv"
CSI300_CACHE = ROOT / "market_valuation_csi300.csv"
CSI_ALL_CACHE = ROOT / "market_valuation_csi_all.csv"

SWS_URL = (
    "https://www.swsresearch.com/institute-sw/api/index_analysis/"
    "index_analysis_report/"
)
CSI_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/140.0 Safari/537.36"
)
ANCHORS = [
    ("2007-10-16", "2007年10月 牛市顶"),
    ("2008-11-04", "2008年11月 金融危机底"),
    ("2010-07-05", "2010年7月 中期底部"),
    ("2012-11-30", "2012年12月 1949点底"),
    ("2014-05-19", "2014年5月 十年最低估值"),
    ("2015-06-12", "2015年6月 杠杆牛顶"),
    ("2016-01-28", "2016年1月 熔断底"),
    ("2018-12-28", "2018年12月 去杠杆底"),
    ("2021-02-10", "2021年2月 核心资产顶"),
    ("2022-10-31", "2022年10月 疫情底"),
    ("2024-02-05", "2024年2月 流动性底"),
    ("2024-09-30", "2024年9月 政策底反转"),
]
HOLDS = {"1个月": 21, "3个月": 63, "6个月": 126, "12个月": 252}
BUCKETS = [
    (0, 20, "0-20%（低估）"),
    (20, 40, "20-40%（偏低）"),
    (40, 60, "40-60%（中性）"),
    (60, 80, "60-80%（偏高）"),
    (80, 101, "80-100%（高估）"),
]


def _is_today(path: Path) -> bool:
    return path.exists() and dt.datetime.fromtimestamp(path.stat().st_mtime).date() == dt.date.today()


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    frame.to_csv(tmp, index=False, encoding="utf-8-sig", date_format="%Y-%m-%d")
    tmp.replace(path)


def _atomic_json(payload: dict, path: Path) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def fetch_swa(start="2006-01-01", end=None, timeout=60) -> pd.DataFrame:
    """申万宏源研究 API：申万 A 指 801003 日频整体法滚动 PE。"""
    end = end or dt.date.today().isoformat()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    rows, page = [], 1
    while True:
        response = session.get(
            SWS_URL,
            params={
                "page": str(page),
                "page_size": "5000",
                "index_type": "市场表征",
                "start_date": start,
                "end_date": end,
                "type": "DAY",
                "swindexcode": "801003",
            },
            verify=False,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        batch = data.get("results") or []
        rows.extend(batch)
        if not batch or len(rows) >= int(data.get("count") or len(rows)):
            break
        page += 1
    frame = pd.DataFrame(rows)
    required = ["bargaindate", "closeindex", "pe"]
    if frame.empty or any(column not in frame for column in required):
        raise RuntimeError("申万宏源接口未返回申万A指日频 PE")
    keep = [
        "bargaindate", "swindexname", "closeindex", "pe", "pb", "dp", "turnoverrate"
    ]
    frame = frame[[column for column in keep if column in frame]].copy()
    frame = frame.rename(
        columns={
            "bargaindate": "date",
            "swindexname": "name",
            "closeindex": "close",
            "pb": "pb",
            "dp": "dividend_yield",
            "turnoverrate": "turnover_rate",
        }
    )
    frame["date"] = pd.to_datetime(frame["date"], utc=True, errors="coerce").dt.tz_convert(
        "Asia/Shanghai"
    ).dt.tz_localize(None)
    for column in frame.columns.difference(["date", "name"]):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "pe", "close"])
    frame = frame[frame.pe > 0].sort_values("date").drop_duplicates("date", keep="last")
    if len(frame) < 4500 or frame.date.min() > pd.Timestamp("2006-02-01"):
        raise RuntimeError(f"申万A指序列完整性检查失败：rows={len(frame)}")
    return frame.reset_index(drop=True)


def _fetch_csi_year(code: str, year: int, timeout=60) -> list[dict]:
    response = requests.get(
        CSI_URL,
        params={"indexCode": code, "startDate": f"{year}0101", "endDate": f"{year}1231"},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    response.raise_for_status()
    return [
        {
            "date": item.get("tradeDate"),
            "code": code,
            "close": item.get("close"),
            "pe": item.get("peg"),
        }
        for item in (response.json().get("data") or [])
    ]


def fetch_csi(code: str, start_year=2005, timeout=60) -> pd.DataFrame:
    """中证指数官网：指数历史收盘与官方 PE-TTM。"""
    rows = []
    with futures.ThreadPoolExecutor(max_workers=4) as executor:
        jobs = [
            executor.submit(_fetch_csi_year, code, year, timeout)
            for year in range(start_year, dt.date.today().year + 1)
        ]
        for job in jobs:
            rows.extend(job.result())
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError(f"中证指数官网未返回 {code} 数据")
    frame["date"] = pd.to_datetime(frame["date"], format="%Y%m%d", errors="coerce")
    frame["pe"] = pd.to_numeric(frame["pe"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "pe", "close"])
    frame = frame[frame.pe > 0].sort_values("date").drop_duplicates("date", keep="last")
    if len(frame) < 3300 or frame.date.min() > pd.Timestamp("2011-07-31"):
        raise RuntimeError(f"中证指数 {code} 序列完整性检查失败：rows={len(frame)}")
    return frame.reset_index(drop=True)


def _read_series(path: Path, kind: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if kind == "swa" and "日期" in frame:
        frame = frame.rename(columns={"日期": "date", "收盘": "close", "PE": "pe"})
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["pe"] = pd.to_numeric(frame["pe"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return frame.dropna(subset=["date", "pe", "close"]).query("pe > 0").sort_values("date").reset_index(drop=True)


def _stats(frame: pd.DataFrame, label: str) -> dict:
    values = frame.pe
    current = float(values.iloc[-1])
    return {
        "label": label,
        "n": int(len(values)),
        "start": frame.date.min().strftime("%Y-%m-%d"),
        "end": frame.date.max().strftime("%Y-%m-%d"),
        "current": round(current, 2),
        "pct": round(float((values < current).mean() * 100), 1),
        "min": round(float(values.min()), 2),
        "q20": round(float(values.quantile(0.2)), 2),
        "median": round(float(values.median()), 2),
        "q80": round(float(values.quantile(0.8)), 2),
        "max": round(float(values.max()), 2),
        "maxDate": frame.loc[values.idxmax(), "date"].strftime("%Y-%m-%d"),
        "minDate": frame.loc[values.idxmin(), "date"].strftime("%Y-%m-%d"),
    }


def _weekly_series(frame: pd.DataFrame, column="pe") -> list[list]:
    daily = frame.set_index("date")[column]
    weekly = daily.resample("W").last().dropna()
    if weekly.index[-1].normalize() != daily.index[-1].normalize():
        weekly = pd.concat([weekly, pd.Series([daily.iloc[-1]], index=[daily.index[-1]])])
    return [[int(index.timestamp() * 1000), round(float(value), 2)] for index, value in weekly.items()]


def _rolling_percentile(frame: pd.DataFrame) -> list[list]:
    window = 2430
    ranked = frame.pe.rolling(window, min_periods=int(window * 0.6)).apply(
        lambda values: (values[:-1] < values[-1]).mean() * 100,
        raw=True,
    )
    daily = pd.Series(ranked.values, index=frame.date).dropna()
    weekly = daily.resample("W").last().dropna()
    return [[int(index.timestamp() * 1000), round(float(value), 1)] for index, value in weekly.items()]


def _histogram(frame: pd.DataFrame, bins=45) -> list[dict]:
    cut, _ = pd.cut(frame.pe, bins=bins, retbins=True)
    counts = cut.value_counts().sort_index()
    return [
        {"x0": round(float(interval.left), 2), "x1": round(float(interval.right), 2), "n": int(count)}
        for interval, count in counts.items()
    ]


def _anchors(frame: pd.DataFrame) -> list[dict]:
    values = frame.set_index("date").pe
    output = []
    for date_text, name in ANCHORS:
        index = values.index.searchsorted(pd.Timestamp(date_text))
        if index >= len(values):
            continue
        low, high = max(0, index - 2), min(len(values), index + 3)
        output.append(
            {
                "date": values.index[index].strftime("%Y-%m-%d"),
                "name": name,
                "pe": round(float(values.iloc[low:high].mean()), 2),
            }
        )
    return output


def _series_payload(frame: pd.DataFrame, label: str, name: str) -> dict:
    return {
        "name": name,
        "data": _weekly_series(frame),
        "stats": _stats(frame, label),
        "anchors": _anchors(frame),
        "hist": _histogram(frame),
        "rolling10y": _rolling_percentile(frame),
    }


def _expanding_percentile(values: pd.Series, minimum=250) -> pd.Series:
    history, output = [], []
    for index, value in enumerate(values.astype(float)):
        output.append(None if index < minimum else bisect.bisect_left(history, value) / len(history) * 100)
        bisect.insort(history, value)
    return pd.Series(output, index=values.index, dtype="float64")


def _backtest_frame(frame: pd.DataFrame, exclude_ranges=None) -> pd.DataFrame:
    data = frame[["date", "pe", "close"]].copy()
    if exclude_ranges:
        mask = pd.Series(True, index=data.index)
        for start, end in exclude_ranges:
            mask &= ~data.date.between(pd.Timestamp(start), pd.Timestamp(end))
        data = data[mask].reset_index(drop=True)
    data["pct"] = _expanding_percentile(data.pe)
    for name, holding_days in HOLDS.items():
        data[f"ret_{name}"] = data.close.shift(-holding_days) / data.close - 1
    return data


def _bucket_stats(frame: pd.DataFrame, stride=1) -> dict:
    if stride > 1:
        frame = frame.iloc[::stride].reset_index(drop=True)
    output = {}
    for low, high, label in BUCKETS:
        subset = frame[frame.pct.ge(low) & frame.pct.lt(high)]
        row = {"label": label, "n": int(len(subset))}
        for name in HOLDS:
            returns = subset[f"ret_{name}"].dropna() * 100
            row[name] = {
                "n": int(len(returns)),
                "median": round(float(returns.median()), 2) if len(returns) else None,
                "mean": round(float(returns.mean()), 2) if len(returns) else None,
                "win": round(float((returns > 0).mean() * 100), 1) if len(returns) else None,
                "p25": round(float(returns.quantile(0.25)), 2) if len(returns) else None,
                "p75": round(float(returns.quantile(0.75)), 2) if len(returns) else None,
                "p05": round(float(returns.quantile(0.05)), 2) if len(returns) else None,
                "worst": round(float(returns.min()), 2) if len(returns) else None,
                "best": round(float(returns.max()), 2) if len(returns) else None,
            }
        output[label] = row
    return output


def _similar_events(frame: pd.DataFrame, current_pct: float, band=3.0, gap_days=120):
    subset = frame[frame.pct.between(current_pct - band, current_pct + band)].copy()
    if subset.empty:
        return [], []
    subset["group"] = subset.date.diff().dt.days.fillna(10**9).gt(gap_days).cumsum()
    events = []
    for _, group in subset.groupby("group"):
        middle = group.iloc[len(group) // 2]
        event = {
            "start": group.date.min().strftime("%Y-%m-%d"),
            "end": group.date.max().strftime("%Y-%m-%d"),
            "days": int(len(group)),
            "pe": round(float(group.pe.mean()), 2),
            "pct": round(float(group.pct.mean()), 1),
        }
        for short, name in [("r1", "1个月"), ("r3", "3个月"), ("r6", "6个月"), ("r12", "12个月")]:
            value = middle[f"ret_{name}"]
            event[short] = None if pd.isna(value) else round(float(value) * 100, 2)
        events.append(event)
    raw = []
    for _, row in subset.iterrows():
        item = {"date": row.date.strftime("%Y-%m-%d"), "pe": round(float(row.pe), 2), "pct": round(float(row.pct), 1)}
        for short, name in [("r1", "1个月"), ("r3", "3个月"), ("r6", "6个月"), ("r12", "12个月")]:
            value = row[f"ret_{name}"]
            item[short] = None if pd.isna(value) else round(float(value) * 100, 2)
        raw.append(item)
    return events, raw


def _backtest_payload(swa: pd.DataFrame) -> dict:
    current_pe = float(swa.pe.iloc[-1])
    current_pct = float((swa.pe.iloc[:-1] < current_pe).mean() * 100)
    full = _backtest_frame(swa)
    excluded = _backtest_frame(
        swa,
        exclude_ranges=[("2006-01-01", "2008-12-31"), ("2015-01-01", "2016-06-30")],
    )
    events, raw = _similar_events(full, current_pct)
    scatter = full.dropna(subset=["pct", "ret_12个月"]).iloc[::5]
    return {
        "generated": dt.date.today().isoformat(),
        "asOf": swa.date.max().strftime("%Y-%m-%d"),
        "current": {
            "pe": round(current_pe, 2),
            "pct": round(current_pct, 1),
            "sample": int(len(swa)),
            "start": swa.date.min().strftime("%Y-%m-%d"),
        },
        "holds": list(HOLDS),
        "full": {
            "buckets": _bucket_stats(full),
            "buckets_indep": _bucket_stats(full, stride=63),
            "events": events,
            "similar": raw,
            "scatter": [[round(float(row.pct), 2), round(float(row["ret_12个月"]) * 100, 2)] for _, row in scatter.iterrows()],
        },
        "excl": {
            "buckets": _bucket_stats(excluded),
            "buckets_indep": _bucket_stats(excluded, stride=63),
            "note": "剔除 2006-01~2008-12 与 2015-01~2016-06 两段极端牛熊",
        },
    }


def build_payload(swa: pd.DataFrame, csi300: pd.DataFrame, csi_all: pd.DataFrame) -> dict:
    return {
        "generated": dt.date.today().isoformat(),
        "series": {
            "swa": _series_payload(swa, "万得全A", "万得全A（申万A指口径）"),
            "csi985": _series_payload(csi_all, "中证全指", "中证全指（官方口径）"),
            "hs300": _series_payload(csi300, "沪深300", "沪深300（中证官方口径）"),
        },
        "backtest": _backtest_payload(swa),
        "methodology": {
            "swa": "申万A指 801003，申万宏源研究 API，整体法滚动市盈率；作为万得全A公开代理",
            "hs300": "沪深300 000300，中证指数官网 index-perf，peg 字段（PE-TTM）",
            "csi985": "中证全指 000985，中证指数官网 index-perf，peg 字段（PE-TTM），用于交叉校验",
        },
    }


def _load_payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def render_market_valuation_report(payload: dict, output_file=DEFAULT_REPORT, force=False) -> bool:
    """生成自包含完整报告；同一自然日已生成时不重复改写文件。"""
    output_path = Path(output_file)
    if not force and _is_today(output_path):
        return False
    history_doc = HISTORY_TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    backtest_doc = BACKTEST_TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/",
        json.dumps(payload["backtest"], ensure_ascii=False, separators=(",", ":")),
    )
    history_b64 = base64.b64encode(history_doc.encode("utf-8")).decode("ascii")
    backtest_b64 = base64.b64encode(backtest_doc.encode("utf-8")).decode("ascii")
    shell = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股 PE-TTM 历史分位与未来收益回测</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f7f8fa;color:#1a1d24;font-family:"PingFang SC","Microsoft YaHei",Arial,sans-serif}
.report-nav{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:7px;padding:10px max(12px,calc((100vw - 1240px)/2));border-bottom:1px solid #e3e6ec;background:rgba(247,248,250,.96);backdrop-filter:blur(10px)}
.report-nav button{appearance:none;border:1px solid transparent;border-radius:7px;padding:9px 18px;background:#eef1f6;color:#4a5160;font:inherit;font-size:13px;font-weight:700;cursor:pointer}
.report-nav button.on{border-color:#cbd7f0;background:#fff;color:#2b5cd9;box-shadow:0 1px 4px rgba(20,25,35,.08)}
.report-nav small{margin-left:auto;color:#7b8494}.report-frame{display:block;width:100%;height:3000px;border:0;background:#f7f8fa}.report-frame[hidden]{display:none}
@media(max-width:600px){.report-nav{padding:8px}.report-nav button{flex:1;padding:8px 5px}.report-nav small{display:none}.report-frame{height:3600px}}
</style></head><body>
<nav class="report-nav" role="tablist" aria-label="完整估值报告视图">
  <button type="button" class="on" role="tab" aria-selected="true" data-report-tab="history">历史分位</button>
  <button type="button" role="tab" aria-selected="false" data-report-tab="backtest">分位回测</button>
  <small>每日快照 · __GENERATED__</small>
</nav>
<iframe class="report-frame" title="A股 PE-TTM 历史分位" data-report-frame="history" data-document="__HISTORY__"></iframe>
<iframe class="report-frame" title="估值分位未来收益回测" data-report-frame="backtest" data-document="__BACKTEST__" hidden></iframe>
<script>(function(){var frames=document.querySelectorAll('[data-report-frame]');function decode(value){var raw=atob(value),bytes=new Uint8Array(raw.length);for(var i=0;i<raw.length;i++)bytes[i]=raw.charCodeAt(i);return new TextDecoder('utf-8').decode(bytes)}function size(frame){try{var doc=frame.contentDocument;if(!doc)return;var h=Math.max(doc.documentElement.scrollHeight,doc.body?doc.body.scrollHeight:0);if(h>0)frame.style.height=(h+4)+'px'}catch(error){}}function load(frame){if(frame.dataset.loaded)return;frame.srcdoc=decode(frame.dataset.document);frame.dataset.loaded='1';frame.addEventListener('load',function(){size(frame);setTimeout(function(){size(frame)},120)})}function show(key){frames.forEach(function(frame){var active=frame.dataset.reportFrame===key;frame.hidden=!active;if(active){load(frame);setTimeout(function(){size(frame)},0)}});document.querySelectorAll('[data-report-tab]').forEach(function(button){var active=button.dataset.reportTab===key;button.classList.toggle('on',active);button.setAttribute('aria-selected',String(active))})}document.addEventListener('click',function(event){var button=event.target.closest('[data-report-tab]');if(button)show(button.dataset.reportTab)});window.addEventListener('resize',function(){frames.forEach(function(frame){if(!frame.hidden)size(frame)})});show('history')})();</script>
</body></html>"""
    shell = (
        shell.replace("__GENERATED__", str(payload.get("generated") or dt.date.today()))
        .replace("__HISTORY__", history_b64)
        .replace("__BACKTEST__", backtest_b64)
    )
    tmp = output_path.with_name(f".{output_path.name}.tmp")
    tmp.write_text(shell, encoding="utf-8")
    tmp.replace(output_path)
    return True


def load_market_valuation(cache_file=DEFAULT_CACHE):
    """读取每日估值快照；必要时刷新，失败则退回最近一次有效缓存。"""
    payload_path = Path(cache_file)
    if _is_today(payload_path):
        return _load_payload(payload_path), {"mode": "cache", "warning": ""}

    warnings = []
    source_paths = [(SWA_CACHE, "swa"), (CSI300_CACHE, "csi"), (CSI_ALL_CACHE, "csi")]
    raw_is_today = all(_is_today(path) for path, _ in source_paths)
    if not raw_is_today:
        try:
            _atomic_csv(fetch_swa(), SWA_CACHE)
        except Exception as error:
            warnings.append(f"申万A指刷新失败：{error}")
        for code, path in [("000300", CSI300_CACHE), ("000985", CSI_ALL_CACHE)]:
            try:
                _atomic_csv(fetch_csi(code), path)
            except Exception as error:
                warnings.append(f"中证指数 {code} 刷新失败：{error}")

    try:
        swa = _read_series(SWA_CACHE, "swa")
        csi300 = _read_series(CSI300_CACHE, "csi")
        csi_all = _read_series(CSI_ALL_CACHE, "csi")
        payload = build_payload(swa, csi300, csi_all)
        _atomic_json(payload, payload_path)
        return payload, {
            "mode": "stale-source" if warnings else ("source-cache" if raw_is_today else "live"),
            "warning": "；".join(warnings),
        }
    except Exception as error:
        if payload_path.exists():
            return _load_payload(payload_path), {
                "mode": "stale-cache",
                "warning": f"估值数据刷新失败，沿用最近快照：{error}",
            }
        return {}, {"mode": "unavailable", "warning": f"估值数据不可用：{error}"}


if __name__ == "__main__":
    payload, status = load_market_valuation()
    print(
        json.dumps(
            {
                "status": status,
                "generated": payload.get("generated"),
                "series": {
                    key: value["stats"] for key, value in payload.get("series", {}).items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
