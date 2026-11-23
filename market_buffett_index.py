"""中国巴菲特指数（沪深股票市值总额 / GDP）：数据采集、滚动GDP计算与交互页面生成。"""
from __future__ import annotations

import argparse
from bisect import bisect_left
from datetime import datetime
import html
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd
import requests


EASTMONEY_DATA_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
TENCENT_KLINE_URLS = (
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

DEFAULT_MCAP_CACHE = Path("market_buffett_mcap_cache.csv")
DEFAULT_GDP_CACHE = Path("market_buffett_gdp_cache.csv")
DEFAULT_HS300_CACHE = Path("market_valuation_csi300.csv")
DEFAULT_CSI_ALL_CACHE = Path("market_valuation_csi_all.csv")
DEFAULT_HISTORY_CSV = Path("market_buffett_index_history.csv")
DEFAULT_OUTPUT_HTML = Path("market_buffett_index.html")


def _em_get(report_name: str, columns: str, page_size: int = 500) -> list[dict]:
    """从东财数据中心拉取单表全部数据（含轻量重试）。"""
    headers = {"User-Agent": UA, "Referer": "https://data.eastmoney.com/cjsj/"}
    rows = []
    page = 1
    while True:
        params = {
            "reportName": report_name,
            "columns": columns,
            "source": "WEB",
            "sortColumns": "REPORT_DATE",
            "sortTypes": "-1",
            "pageNumber": str(page),
            "pageSize": str(page_size),
        }
        payload = None
        for attempt in range(3):
            try:
                resp = requests.get(EASTMONEY_DATA_URL, params=params, headers=headers, timeout=25)
                resp.raise_for_status()
                payload = resp.json()
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
        result = (payload or {}).get("result") or {}
        batch = result.get("data") or []
        if not payload or not payload.get("success") or not batch:
            if page == 1:
                raise ValueError(f"东财未返回 {report_name} 数据：{payload}")
            break
        rows.extend(batch)
        total_pages = int(result.get("pages") or page)
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.2)
    return rows


def load_or_update_mcap(cache_path: Path = DEFAULT_MCAP_CACHE, force: bool = False) -> pd.DataFrame:
    """加载并更新东财全国股票交易统计表（沪深两市月度总市值，亿元）。"""
    if cache_path.exists() and not force:
        mtime = datetime.fromtimestamp(cache_path.stat().st_mtime).date()
        if mtime == datetime.now().date():
            df = pd.read_csv(cache_path, parse_dates=["date"])
            if not df.empty:
                return df
    rows = _em_get(
        "RPT_ECONOMY_STOCK_STATISTICS",
        "REPORT_DATE,TIME,TOTAL_MARKE_SH,TOTAL_MARKE_SZ",
        page_size=500,
    )
    raw = pd.DataFrame(rows)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["REPORT_DATE"], errors="coerce") + pd.offsets.MonthEnd(0),
            "sh_mcap_yi": pd.to_numeric(raw["TOTAL_MARKE_SH"], errors="coerce"),
            "sz_mcap_yi": pd.to_numeric(raw["TOTAL_MARKE_SZ"], errors="coerce"),
        }
    ).dropna(subset=["date", "sh_mcap_yi", "sz_mcap_yi"])
    frame["total_mcap_yi"] = frame["sh_mcap_yi"] + frame["sz_mcap_yi"]
    frame = frame.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    frame.to_csv(cache_path, index=False, encoding="utf-8-sig")
    return frame


def load_or_update_gdp(cache_path: Path = DEFAULT_GDP_CACHE, force: bool = False) -> pd.DataFrame:
    """加载并更新东财中国季度累计 GDP，换算为单季 GDP 与滚动 4 季度（TTM）GDP（亿元）。"""
    if cache_path.exists() and not force:
        mtime = datetime.fromtimestamp(cache_path.stat().st_mtime).date()
        if mtime == datetime.now().date():
            df = pd.read_csv(cache_path, parse_dates=["date"])
            if not df.empty:
                return df
    rows = _em_get(
        "RPT_ECONOMY_GDP",
        "REPORT_DATE,TIME,DOMESTICL_PRODUCT_BASE",
        page_size=200,
    )
    raw = pd.DataFrame(rows)
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["REPORT_DATE"], errors="coerce") + pd.offsets.MonthEnd(0),
            "period_label": raw["TIME"].astype(str),
            "cum_gdp_yi": pd.to_numeric(raw["DOMESTICL_PRODUCT_BASE"], errors="coerce"),
        }
    ).dropna(subset=["date", "cum_gdp_yi"])
    frame = frame.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    frame["year"] = frame["date"].dt.year
    frame["quarter"] = frame["date"].dt.quarter

    # 由年内累计 GDP 差分计算单季 GDP，再滚动求和 4 个季度得到 TTM GDP
    single_q = []
    prev_by_year: dict[int, float] = {}
    for _, row in frame.iterrows():
        yr = int(row["year"])
        q = int(row["quarter"])
        cum = float(row["cum_gdp_yi"])
        q_val = cum if q == 1 else (cum - prev_by_year[yr] if yr in prev_by_year else np.nan)
        prev_by_year[yr] = cum
        single_q.append(q_val)
    frame["single_q_gdp_yi"] = single_q
    frame["gdp_ttm_yi"] = frame["single_q_gdp_yi"].rolling(4, min_periods=4).sum()
    frame = frame.dropna(subset=["gdp_ttm_yi"]).reset_index(drop=True)
    frame.to_csv(cache_path, index=False, encoding="utf-8-sig")
    return frame


def _load_hs300_closes(csi300_path: Path = DEFAULT_HS300_CACHE) -> pd.DataFrame:
    """加载沪深300收盘价（优先本地缓存，并补拉 2008–2011 早期历史以便全段对照）。"""
    local = pd.DataFrame(columns=["date", "close"])
    if csi300_path.exists():
        local = pd.read_csv(csi300_path, parse_dates=["date"])[["date", "close"]]
        local["date"] = local["date"].dt.normalize()
        local["close"] = pd.to_numeric(local["close"], errors="coerce")
        local = local.dropna()

    early_rows = []
    if local.empty or local["date"].min() > pd.Timestamp("2008-02-01"):
        for yr in (2008, 2010):
            for attempt in range(3):
                url = TENCENT_KLINE_URLS[attempt % len(TENCENT_KLINE_URLS)]
                try:
                    resp = requests.get(
                        url,
                        params={"param": f"sh000300,day,{yr}-01-01,{yr+1}-12-31,640,qfq"},
                        timeout=15,
                    )
                    resp.raise_for_status()
                    node = (resp.json().get("data") or {}).get("sh000300") or {}
                    bars = node.get("qfqday") or node.get("day") or []
                    for bar in bars:
                        if len(bar) >= 3:
                            early_rows.append((bar[0], bar[2]))
                    break
                except Exception:
                    time.sleep(0.4)
    if early_rows:
        early = pd.DataFrame(early_rows, columns=["date", "close"])
        early["date"] = pd.to_datetime(early["date"], errors="coerce").dt.normalize()
        early["close"] = pd.to_numeric(early["close"], errors="coerce")
        local = pd.concat([early.dropna(), local], ignore_index=True)

    return local.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def _estimate_current_month_row(
    mcap_df: pd.DataFrame,
    csi_all_path: Path = DEFAULT_CSI_ALL_CACHE,
    csi300_df: pd.DataFrame | None = None,
) -> dict | None:
    """当月官方月度总市值尚未公布时，按中证全指（或沪深300）区间涨跌幅估算最新交易日总市值。"""
    last_mcap = mcap_df.iloc[-1]
    last_mcap_date = pd.Timestamp(last_mcap["date"]).normalize()

    ref = pd.DataFrame()
    if csi_all_path.exists():
        ref = pd.read_csv(csi_all_path, parse_dates=["date"])[["date", "close"]].dropna()
    elif csi300_df is not None and not csi300_df.empty:
        ref = csi300_df[["date", "close"]].copy()
    if ref.empty:
        return None
    ref["date"] = pd.to_datetime(ref["date"]).dt.normalize()
    ref["close"] = pd.to_numeric(ref["close"], errors="coerce")
    ref = ref.dropna().sort_values("date").drop_duplicates("date")

    latest_trade = ref.iloc[-1]
    if latest_trade["date"] <= last_mcap_date:
        return None

    base_sub = ref[ref["date"] <= last_mcap_date]
    if base_sub.empty:
        return None
    base_close = float(base_sub.iloc[-1]["close"])
    cur_close = float(latest_trade["close"])
    if base_close <= 0:
        return None
    ratio = cur_close / base_close
    return {
        "date": latest_trade["date"],
        "sh_mcap_yi": float(last_mcap["sh_mcap_yi"]) * ratio,
        "sz_mcap_yi": float(last_mcap["sz_mcap_yi"]) * ratio,
        "total_mcap_yi": float(last_mcap["total_mcap_yi"]) * ratio,
        "is_estimate": True,
    }


def buffett_zone(pct_10y: float) -> tuple[str, str, str]:
    """根据巴菲特指数近10年滚动历史分位（0~100）返回状态标签、文字色与浅底色（绿-黄绿-黄-浅红-红）。"""
    if pd.isna(pct_10y):
        return "暂无", "#64748b", "#f1f5f9"
    if pct_10y < 20:
        return "显著偏低", "#047857", "#d1fae5"
    if pct_10y < 40:
        return "偏低区间", "#4d7c0f", "#d9f99d"
    if pct_10y < 60:
        return "合理区间", "#b45309", "#fde68a"
    if pct_10y < 80:
        return "偏高区间", "#e11d48", "#ffe4e6"
    return "高位警示", "#b91c1c", "#fecaca"


def calculate_buffett_history(
    mcap_df: pd.DataFrame,
    gdp_df: pd.DataFrame,
    hs300_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """合成月频+当月估算巴菲特指数序列、年度对比表及最新快照元数据。"""
    mcap = mcap_df.copy()
    mcap["is_estimate"] = False
    est = _estimate_current_month_row(mcap, DEFAULT_CSI_ALL_CACHE, hs300_df)
    if est is not None:
        mcap = pd.concat([mcap, pd.DataFrame([est])], ignore_index=True)
    mcap = mcap.sort_values("date").reset_index(drop=True)

    gdp_clean = gdp_df[["date", "period_label", "gdp_ttm_yi"]].sort_values("date").drop_duplicates("date")
    frame = pd.merge_asof(
        mcap,
        gdp_clean,
        on="date",
        direction="backward",
    )
    frame = pd.merge_asof(
        frame,
        hs300_df[["date", "close"]].rename(columns={"close": "hs300_close"}),
        on="date",
        direction="backward",
        tolerance=pd.Timedelta(days=15),
    ).dropna(subset=["total_mcap_yi", "gdp_ttm_yi"]).reset_index(drop=True)

    frame["buffett_index_pct"] = frame["total_mcap_yi"] / frame["gdp_ttm_yi"] * 100.0

    # 逐期计算近10年滚动历史分位及五档分位带阈值（仅使用截至当期的历史数据，无未来函数）
    dates_list = frame["date"].tolist()
    vals_arr = frame["buffett_index_pct"].to_numpy(dtype=float)
    pct_10y_series = []
    q20_series, q40_series, q60_series, q80_series = [], [], [], []
    for i, cur_date in enumerate(dates_list):
        cutoff = cur_date - pd.DateOffset(years=10)
        start_idx = bisect_left(dates_list, cutoff)
        window = vals_arr[start_idx : i + 1]
        pct_10y_series.append(float((window <= vals_arr[i]).mean() * 100.0))
        q20, q40, q60, q80 = np.quantile(window, [0.2, 0.4, 0.6, 0.8])
        q20_series.append(float(q20))
        q40_series.append(float(q40))
        q60_series.append(float(q60))
        q80_series.append(float(q80))
    frame["pct_10y"] = pct_10y_series
    frame["q20_10y"] = q20_series
    frame["q40_10y"] = q40_series
    frame["q60_10y"] = q60_series
    frame["q80_10y"] = q80_series

    # 年度年末对比表（2008 年至最新完整年度 + 当前最新年）
    frame["year"] = frame["date"].dt.year
    annual = (
        frame.sort_values("date")
        .groupby("year", as_index=False)
        .last()[["year", "date", "total_mcap_yi", "gdp_ttm_yi", "buffett_index_pct", "is_estimate"]]
    )

    latest = frame.iloc[-1]
    latest_official = frame[~frame["is_estimate"]].iloc[-1]
    latest_val = float(latest["buffett_index_pct"])

    all_pct = float((frame["buffett_index_pct"] <= latest_val).mean() * 100.0)
    pct_10y = float(latest["pct_10y"])

    max_row = frame.loc[frame["buffett_index_pct"].idxmax()]
    min_row = frame.loc[frame["buffett_index_pct"].idxmin()]

    meta = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "first_date": frame["date"].iloc[0].strftime("%Y-%m"),
        "latest_date": latest["date"].strftime("%Y-%m-%d"),
        "latest_is_estimate": bool(latest["is_estimate"]),
        "latest_official_month": latest_official["date"].strftime("%Y-%m"),
        "latest_official_pct": round(float(latest_official["buffett_index_pct"]), 2),
        "total_points": int(len(frame)),
        "buffett_index_pct": round(latest_val, 2),
        "total_mcap_wan_yi": round(float(latest["total_mcap_yi"]) / 1e4, 2),
        "sh_mcap_wan_yi": round(float(latest["sh_mcap_yi"]) / 1e4, 2),
        "sz_mcap_wan_yi": round(float(latest["sz_mcap_yi"]) / 1e4, 2),
        "gdp_ttm_wan_yi": round(float(latest["gdp_ttm_yi"]) / 1e4, 2),
        "gdp_period": str(latest["period_label"]),
        "hs300_close": round(float(latest["hs300_close"]), 2) if pd.notna(latest["hs300_close"]) else None,
        "pct_all": round(all_pct, 1),
        "pct_10y": round(pct_10y, 1),
        "max_val": round(float(max_row["buffett_index_pct"]), 2),
        "max_date": max_row["date"].strftime("%Y-%m"),
        "min_val": round(float(min_row["buffett_index_pct"]), 2),
        "min_date": min_row["date"].strftime("%Y-%m"),
    }
    return frame, annual, meta


def save_history_csv(frame: pd.DataFrame, output_csv: Path = DEFAULT_HISTORY_CSV) -> None:
    """导出巴菲特指数全历史 CSV。"""
    out = frame[
        [
            "date",
            "sh_mcap_yi",
            "sz_mcap_yi",
            "total_mcap_yi",
            "gdp_ttm_yi",
            "buffett_index_pct",
            "pct_10y",
            "hs300_close",
            "is_estimate",
        ]
    ].copy()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    for col in ("sh_mcap_yi", "sz_mcap_yi", "total_mcap_yi", "gdp_ttm_yi", "buffett_index_pct", "hs300_close"):
        out[col] = pd.to_numeric(out[col], errors="coerce").round(2)
    out["pct_10y"] = pd.to_numeric(out["pct_10y"], errors="coerce").round(1)
    out.to_csv(output_csv, index=False, encoding="utf-8-sig")


BUFFETT_SCRIPT = r"""<script>
(function(){
  var dataNode=document.getElementById("bf-data");
  if(!dataNode)return;
  var payload=null;
  try{payload=JSON.parse(dataNode.textContent||"null")}catch(e){return}
  if(!payload||!Array.isArray(payload.dates)||payload.dates.length<2)return;

  var dates=payload.dates,total=dates.length,maxIndex=total-1;
  var SVG_NS="http://www.w3.org/2000/svg";
  var tip=document.getElementById("bf-popover");
  var dateTimes=dates.map(function(d){return new Date(d+"T00:00:00").getTime()});
  var latestTime=dateTimes[maxIndex];

  function clamp(v,lo,hi){return Math.max(lo,Math.min(hi,v))}
  function fmt(v,d){
    if(v===null||v===undefined||isNaN(v))return "--";
    return Number(v).toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d});
  }
  function quantile(sorted,p){
    if(!sorted.length)return null;
    var pos=(sorted.length-1)*p;
    var lo=Math.floor(pos),hi=Math.ceil(pos);
    return sorted[lo]+(sorted[hi]-sorted[lo])*(pos-lo);
  }

  function showTip(title,dateText,rows,clientX,clientY){
    if(!tip)return;
    tip.querySelector("[data-tip-title]").textContent=title;
    tip.querySelector("[data-tip-date]").textContent=dateText;
    var list=tip.querySelector("[data-tip-rows]");
    list.textContent="";
    rows.forEach(function(r){
      var item=document.createElement("div");
      item.className="bf-tip-row";
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

  // 图 1：巴菲特指数与沪深300双轴交互走势图
  (function initMainChart(){
    var svg=document.getElementById("bf-main-svg");
    if(!svg)return;
    var W=1080,H=410,L=62,R=64,T=26,B=42;
    var PW=W-L-R,PH=H-T-B;
    var minSpan=Math.min(12,maxIndex);
    var viewStart=0,viewEnd=maxIndex;
    var visible={main:true,index:true,bands:true};
    var leftMin=0,leftMax=140,rightMin=0,rightMax=6000;

    var crosshair=svg.querySelector("[data-crosshair]");
    var bandsGroup=svg.querySelector("[data-zone-bands]");
    var datesGroup=svg.querySelector("[data-dates]");
    var extremaGroup=svg.querySelector("[data-extrema]");
    var lyEls=[0,1,2,3,4].map(function(i){return svg.querySelector('[data-ly="'+i+'"]')});
    var ryEls=[0,1,2,3,4].map(function(i){return svg.querySelector('[data-ry="'+i+'"]')});
    var p80Line=svg.querySelector("[data-p80-line]"),p80Text=svg.querySelector("[data-p80-text]");
    var p50Line=svg.querySelector("[data-p50-line]"),p50Text=svg.querySelector("[data-p50-text]");
    var p20Line=svg.querySelector("[data-p20-line]"),p20Text=svg.querySelector("[data-p20-text]");
    var mainPath=svg.querySelector('[data-line="main"]');
    var idxPath=svg.querySelector('[data-line="index"]');
    var section=svg.closest("section");
    var rangeEl=section?section.querySelector("[data-chart-range]"):null;
    var resetBtn=section?section.querySelector("[data-chart-reset]"):null;
    var rangeBtns=section?section.querySelectorAll("[data-range-years]"):[];
    var toggles=section?section.querySelectorAll("[data-series-toggle]"):[];

    function indexForYears(y){
      if(y==="all")return 0;
      var cutoff=latestTime-Number(y)*365.25*86400000;
      for(var i=0;i<=maxIndex;i++){if(dateTimes[i]>=cutoff)return i}
      return 0;
    }
    function xFor(i){return L+((i-viewStart)/Math.max(viewEnd-viewStart,1e-9))*PW}
    function yLeft(v){return T+((leftMax-v)/Math.max(leftMax-leftMin,1e-9))*PH}
    function yRight(v){return T+((rightMax-v)/Math.max(rightMax-rightMin,1e-9))*PH}

    function buildPath(arr,i0,i1,yFn){
      var cmds=[];
      for(var i=i0;i<=i1;i++){
        var v=arr[i];
        if(v===null||v===undefined||isNaN(v))continue;
        cmds.push((cmds.length?"L":"M")+xFor(i).toFixed(1)+","+yFn(v).toFixed(1));
      }
      return cmds.join(" ");
    }

    function renderZoneBands(i0,i1){
      if(!bandsGroup)return;
      bandsGroup.textContent="";
      if(!visible.bands||!payload.q20||!payload.q40||!payload.q60||!payload.q80)return;
      var yTop=T,yBot=H-B;
      function yClamped(v){return clamp(yLeft(v),yTop,yBot)}
      function buildRibbon(upperFn,lowerFn,fill,opacity){
        var pts=[];
        for(var i=i0;i<=i1;i++){
          pts.push(xFor(i).toFixed(1)+","+upperFn(i).toFixed(1));
        }
        for(var j=i1;j>=i0;j--){
          pts.push(xFor(j).toFixed(1)+","+lowerFn(j).toFixed(1));
        }
        if(pts.length<4)return;
        var poly=document.createElementNS(SVG_NS,"polygon");
        poly.setAttribute("points",pts.join(" "));
        poly.setAttribute("fill",fill);
        poly.setAttribute("fill-opacity",String(opacity));
        bandsGroup.appendChild(poly);
      }
      // 五档滚动分位背景：红(>=80%) -> 浅红(60-80%) -> 暖黄(40-60%) -> 黄绿(20-40%) -> 翠绿(<20%)
      buildRibbon(function(){return yTop},function(i){return yClamped(payload.q80[i])},"#dc2626",0.22);
      buildRibbon(function(i){return yClamped(payload.q80[i])},function(i){return yClamped(payload.q60[i])},"#fb7185",0.13);
      buildRibbon(function(i){return yClamped(payload.q60[i])},function(i){return yClamped(payload.q40[i])},"#f59e0b",0.20);
      buildRibbon(function(i){return yClamped(payload.q40[i])},function(i){return yClamped(payload.q20[i])},"#84cc16",0.22);
      buildRibbon(function(i){return yClamped(payload.q20[i])},function(){return yBot},"#10b981",0.22);
      // 柔和分界线增强相邻区间辨识度（尤其是 40% 黄/黄绿 交界）
      [
        {arr:payload.q80,stroke:"#dc2626"},
        {arr:payload.q60,stroke:"#d97706"},
        {arr:payload.q40,stroke:"#65a30d"},
        {arr:payload.q20,stroke:"#059669"}
      ].forEach(function(b){
        var d=buildPath(b.arr,i0,i1,yClamped);
        if(!d)return;
        var p=document.createElementNS(SVG_NS,"path");
        p.setAttribute("d",d);
        p.setAttribute("fill","none");
        p.setAttribute("stroke",b.stroke);
        p.setAttribute("stroke-width","1");
        p.setAttribute("stroke-opacity","0.35");
        p.setAttribute("stroke-dasharray","3 3");
        bandsGroup.appendChild(p);
      });
    }

    function setRefLine(lineEl,textEl,val,label){
      if(!lineEl||!textEl)return;
      if(!visible.bands||val===null||val<leftMin||val>leftMax){
        lineEl.setAttribute("hidden","");textEl.setAttribute("hidden","");return;
      }
      var y=yLeft(val).toFixed(1);
      lineEl.setAttribute("y1",y);lineEl.setAttribute("y2",y);lineEl.removeAttribute("hidden");
      textEl.setAttribute("y",String(Number(y)-4));textEl.textContent=label+" "+fmt(val,1)+"%";
      textEl.removeAttribute("hidden");
    }

    function renderExtrema(i0,i1){
      if(!extremaGroup)return;
      extremaGroup.textContent="";
      if(!visible.main)return;
      var maxI=-1,minI=-1,maxV=-Infinity,minV=Infinity;
      for(var i=i0;i<=i1;i++){
        var v=payload.buffett[i];
        if(v===null||v===undefined||isNaN(v))continue;
        if(v>maxV){maxV=v;maxI=i}
        if(v<minV){minV=v;minI=i}
      }
      if(maxI<0)return;
      var pts=[{label:"最高",i:maxI,v:maxV}];
      if(minI>=0&&minI!==maxI)pts.push({label:"最低",i:minI,v:minV});
      pts.forEach(function(pt){
        var x=xFor(pt.i),y=yLeft(pt.v);
        var anchor="middle",tx=x;
        if(x<L+85){anchor="start";tx=x+7}
        else if(x>W-R-85){anchor="end";tx=x-7}
        var ty=pt.label==="最高"?(y<T+22?y+17:y-9):(y>H-B-20?y-9:y+17);
        var c=document.createElementNS(SVG_NS,"circle");
        c.setAttribute("cx",x.toFixed(1));c.setAttribute("cy",y.toFixed(1));c.setAttribute("r","4");
        c.setAttribute("fill","#2563eb");c.setAttribute("stroke","#fff");c.setAttribute("stroke-width","2");
        var t=document.createElementNS(SVG_NS,"text");
        t.setAttribute("x",tx.toFixed(1));t.setAttribute("y",ty.toFixed(1));t.setAttribute("text-anchor",anchor);
        t.setAttribute("class","bf-extrema-text");
        t.textContent=pt.label+" "+fmt(pt.v,2)+"%";
        extremaGroup.appendChild(c);extremaGroup.appendChild(t);
      });
    }

    function render(){
      var i0=Math.max(0,Math.floor(viewStart)),i1=Math.min(maxIndex,Math.ceil(viewEnd));
      var lLo=Infinity,lHi=-Infinity,rLo=Infinity,rHi=-Infinity,slice=[];
      for(var i=i0;i<=i1;i++){
        var mv=payload.buffett[i],iv=payload.hs300[i];
        if(mv!==null&&mv!==undefined&&!isNaN(mv)){if(mv<lLo)lLo=mv;if(mv>lHi)lHi=mv;slice.push(mv)}
        if(iv!==null&&iv!==undefined&&!isNaN(iv)){if(iv<rLo)rLo=iv;if(iv>rHi)rHi=iv}
      }
      if(!isFinite(lLo)||!isFinite(lHi)){lLo=0;lHi=100}
      if(!isFinite(rLo)||!isFinite(rHi)){rLo=0;rHi=5000}
      var lPad=Math.max((lHi-lLo)*0.12,4);
      leftMin=Math.max(0,lLo-lPad);leftMax=lHi+lPad;
      var rPad=Math.max((rHi-rLo)*0.08,100);
      rightMin=Math.max(0,rLo-rPad);rightMax=rHi+rPad;

      for(var k=0;k<5;k++){
        var f=k/4;
        if(lyEls[k])lyEls[k].textContent=fmt(leftMax-(leftMax-leftMin)*f,0)+"%";
        if(ryEls[k])ryEls[k].textContent=Math.round(rightMax-(rightMax-rightMin)*f).toLocaleString("zh-CN");
      }
      renderZoneBands(i0,i1);
      slice.sort(function(a,b){return a-b});
      setRefLine(p80Line,p80Text,quantile(slice,0.8),"80%分位");
      setRefLine(p50Line,p50Text,quantile(slice,0.5),"中位数");
      setRefLine(p20Line,p20Text,quantile(slice,0.2),"20%分位");

      if(mainPath)mainPath.setAttribute("d",visible.main?buildPath(payload.buffett,i0,i1,yLeft):"");
      if(idxPath)idxPath.setAttribute("d",visible.index?buildPath(payload.hs300,i0,i1,yRight):"");
      renderExtrema(i0,i1);

      if(datesGroup){
        datesGroup.textContent="";
        var span=Math.max(i1-i0,1),cnt=Math.min(7,span+1),seen={};
        for(var d=0;d<cnt;d++){
          var idx=Math.round(i0+d*span/Math.max(cnt-1,1));
          if(seen[idx])continue;seen[idx]=true;
          var txt=document.createElementNS(SVG_NS,"text");
          txt.setAttribute("x",xFor(idx).toFixed(1));txt.setAttribute("y",String(H-14));
          txt.setAttribute("text-anchor",d===0?"start":(d===cnt-1?"end":"middle"));
          txt.textContent=dates[idx].slice(0,7);
          datesGroup.appendChild(txt);
        }
      }
      var isFull=viewStart<=0.5&&viewEnd>=maxIndex-0.5;
      svg.classList.toggle("is-zoomed",!isFull);
      if(resetBtn)resetBtn.hidden=isFull;
      if(rangeEl){
        var ratio=maxIndex/Math.max(viewEnd-viewStart,1);
        rangeEl.textContent=dates[i0].slice(0,7)+" ~ "+dates[i1].slice(0,7)+"（"+(i1-i0+1)+"期"+(!isFull?" · "+ratio.toFixed(1)+"×":"")+"）";
      }
      rangeBtns.forEach(function(btn){
        var y=btn.getAttribute("data-range-years");
        var active=Math.abs(viewStart-indexForYears(y))<=1.5&&viewEnd>=maxIndex-1;
        btn.setAttribute("aria-pressed",String(active));
      });
    }

    function clientToSvgX(cx){var r=svg.getBoundingClientRect();return r.width?((cx-r.left)/r.width)*W:L}
    function updateHover(cx,cy){
      var sx=clientToSvgX(cx);
      if(sx<L||sx>W-R){if(crosshair)crosshair.setAttribute("hidden","");hideTip();return}
      var idx=clamp(Math.round(viewStart+clamp((sx-L)/PW,0,1)*(viewEnd-viewStart)),0,maxIndex);
      if(crosshair){var x=xFor(idx).toFixed(1);crosshair.setAttribute("x1",x);crosshair.setAttribute("x2",x);crosshair.removeAttribute("hidden")}
      var dLabel=payload.est[idx]?dates[idx]+" (当月估算)":dates[idx].slice(0,7);
      var pctVal=payload.pct10y?payload.pct10y[idx]:null;
      var zLabel="--",zColor="#475569";
      if(pctVal!==null&&pctVal!==undefined&&!isNaN(pctVal)){
        if(pctVal<20){zLabel="显著偏低";zColor="#15803d"}
        else if(pctVal<40){zLabel="偏低区间";zColor="#4d7c0f"}
        else if(pctVal<60){zLabel="合理区间";zColor="#b47b00"}
        else if(pctVal<80){zLabel="偏高区间";zColor="#e11d48"}
        else{zLabel="高位警示";zColor="#b91c1c"}
      }
      var rows=[
        {name:"巴菲特指数 (总市值/GDP)",color:"#2563eb",value:fmt(payload.buffett[idx],2)+"%",valueColor:"#2563eb"},
        {name:"近10年滚动分位",color:zColor,value:fmt(pctVal,1)+"% · "+zLabel,valueColor:zColor},
        {name:"沪深股票总市值",color:"#0d9488",value:fmt(payload.mcap[idx]/10000,2)+" 万亿元"},
        {name:"滚动4季度GDP (TTM)",color:"#f59e0b",value:fmt(payload.gdp[idx]/10000,2)+" 万亿元"},
        {name:"沪深300收盘",color:"#dc2626",value:fmt(payload.hs300[idx],2)+" 点"}
      ];
      showTip("中国巴菲特指数",dLabel,rows,cx,cy);
    }

    svg.addEventListener("wheel",function(e){
      if(!e.ctrlKey)return;
      e.preventDefault();
      var ratio=clamp((clientToSvgX(e.clientX)-L)/PW,0,1);
      var span=viewEnd-viewStart;
      var newSpan=clamp(span*(e.deltaY<0?0.8:1.25),minSpan,maxIndex);
      if(Math.abs(newSpan-span)<1e-6)return;
      var anchor=viewStart+ratio*span;
      viewStart=clamp(anchor-ratio*newSpan,0,maxIndex-newSpan);
      viewEnd=viewStart+newSpan;
      render();updateHover(e.clientX,e.clientY);
    },{passive:false});

    var dragging=false,dragX=0,oS=0,oE=0,moved=false;
    svg.addEventListener("mousedown",function(e){if(e.button!==0)return;dragging=true;moved=false;dragX=e.clientX;oS=viewStart;oE=viewEnd;e.preventDefault()});
    svg.addEventListener("mousemove",function(e){if(!dragging)updateHover(e.clientX,e.clientY)});
    svg.addEventListener("mouseleave",function(){if(!dragging){if(crosshair)crosshair.setAttribute("hidden","");hideTip()}});
    window.addEventListener("mousemove",function(e){
      if(!dragging)return;
      var dx=e.clientX-dragX;
      if(!moved&&Math.abs(dx)>3){moved=true;svg.classList.add("is-dragging");if(crosshair)crosshair.setAttribute("hidden","");hideTip()}
      if(!moved)return;
      var rect=svg.getBoundingClientRect(),plotPx=rect.width*(PW/W),span=oE-oS;
      if(plotPx<=0||span>=maxIndex-1e-6)return;
      viewStart=clamp(oS-(dx/plotPx)*span,0,maxIndex-span);viewEnd=viewStart+span;render();
    });
    window.addEventListener("mouseup",function(e){
      if(!dragging)return;dragging=false;svg.classList.remove("is-dragging");
      if(moved){var r=svg.getBoundingClientRect();if(e.clientX>=r.left&&e.clientX<=r.right&&e.clientY>=r.top&&e.clientY<=r.bottom)updateHover(e.clientX,e.clientY)}
    });
    svg.addEventListener("dblclick",function(e){e.preventDefault();viewStart=0;viewEnd=maxIndex;render()});
    if(resetBtn)resetBtn.addEventListener("click",function(){viewStart=0;viewEnd=maxIndex;render()});
    rangeBtns.forEach(function(b){b.addEventListener("click",function(){viewEnd=maxIndex;viewStart=indexForYears(b.getAttribute("data-range-years"));render()})});
    toggles.forEach(function(b){b.addEventListener("click",function(){var k=b.getAttribute("data-series-toggle");visible[k]=!visible[k];b.setAttribute("aria-pressed",String(visible[k]));render()})});
    render();
  })();

  // 图 2：年度沪深总市值 vs GDP 柱状图悬浮交互
  document.querySelectorAll("[data-annual-year]").forEach(function(g){
    g.addEventListener("mousemove",function(e){
      var yr=g.getAttribute("data-annual-year");
      var mcap=g.getAttribute("data-annual-mcap");
      var gdp=g.getAttribute("data-annual-gdp");
      var ratio=g.getAttribute("data-annual-ratio");
      showTip("年度总市值 vs GDP",yr+"年",[
        {name:"沪深股票总市值",color:"#2563eb",value:mcap+" 万亿元"},
        {name:"滚动/全年GDP",color:"#f59e0b",value:gdp+" 万亿元"},
        {name:"巴菲特指数",color:"#0d9488",value:ratio+"%",valueColor:"#0d9488"}
      ],e.clientX,e.clientY);
    });
    g.addEventListener("mouseleave",hideTip);
  });

  // 导出 CSV
  var csvBtn=document.getElementById("bf-download-csv");
  if(csvBtn){
    csvBtn.addEventListener("click",function(){
      var lines=["date,total_mcap_yi,gdp_ttm_yi,buffett_index_pct,hs300_close,is_estimate"];
      for(var i=0;i<total;i++){
        lines.push([dates[i],payload.mcap[i],payload.gdp[i],payload.buffett[i],payload.hs300[i],payload.est[i]].join(","));
      }
      var blob=new Blob(["\ufeff"+lines.join("\n")],{type:"text/csv;charset=utf-8;"});
      var url=URL.createObjectURL(blob);
      var a=document.createElement("a");a.href=url;a.download="china-buffett-index-history.csv";a.click();
      setTimeout(function(){URL.revokeObjectURL(url)},1000);
    });
  }
})();
</script>"""


def _build_annual_bars_svg(annual: pd.DataFrame) -> str:
    """绘制年度沪深股票总市值与 GDP 并列对比柱状图 SVG。"""
    W, H, L, R, T, B = 1080, 320, 62, 24, 24, 42
    PW, PH = W - L - R, H - T - B
    max_val = max(float(annual["total_mcap_yi"].max()), float(annual["gdp_ttm_yi"].max())) / 1e4 * 1.12
    n = len(annual)
    slot = PW / max(n, 1)
    bw = max(6.0, min(18.0, slot * 0.34))

    parts = []
    for k in range(5):
        y = T + PH * k / 4
        val = max_val * (1 - k / 4)
        parts.append(f'<line class="bf-axis" x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}"/>')
        parts.append(f'<text x="{L - 8}" y="{y + 4:.1f}" text-anchor="end">{val:.0f}万亿</text>')

    for idx, row in annual.reset_index(drop=True).iterrows():
        cx = L + (idx + 0.5) * slot
        m_w = float(row["total_mcap_yi"]) / 1e4
        g_w = float(row["gdp_ttm_yi"]) / 1e4
        b_pct = float(row["buffett_index_pct"])
        hm = m_w / max_val * PH
        hg = g_w / max_val * PH
        ym = T + PH - hm
        yg = T + PH - hg
        yr_lbl = f"{int(row['year'])}" + ("*" if row["is_estimate"] else "")
        parts.append(
            f'<g class="bf-bar-group" data-annual-year="{int(row["year"])}" '
            f'data-annual-mcap="{m_w:.2f}" data-annual-gdp="{g_w:.2f}" data-annual-ratio="{b_pct:.2f}">'
            f'<rect x="{cx - slot / 2:.1f}" y="{T}" width="{slot:.1f}" height="{PH}" fill="transparent"/>'
            f'<rect class="bf-bar-mcap" x="{cx - bw - 1:.1f}" y="{ym:.1f}" width="{bw:.1f}" height="{hm:.1f}" rx="2"/>'
            f'<rect class="bf-bar-gdp" x="{cx + 1:.1f}" y="{yg:.1f}" width="{bw:.1f}" height="{hg:.1f}" rx="2"/>'
            f'<text x="{cx:.1f}" y="{H - 14}" text-anchor="middle">{yr_lbl}</text>'
            f'</g>'
        )
    return f'<svg class="bf-svg" viewBox="0 0 {W} {H}">{"".join(parts)}</svg>'


def render_buffett_page(
    frame: pd.DataFrame,
    annual: pd.DataFrame,
    meta: dict,
    output_html: Path = DEFAULT_OUTPUT_HTML,
) -> None:
    """渲染独立单文件中国巴菲特指数页面。"""
    zone_label, zone_color, zone_bg = buffett_zone(meta["pct_10y"])
    annual_svg = _build_annual_bars_svg(annual)

    recent_rows = []
    for _, row in frame.tail(12).iloc[::-1].iterrows():
        pct_val = float(row["pct_10y"])
        lbl, clr, bg = buffett_zone(pct_val)
        d_text = (
            f"{row['date']:%Y-%m-%d} (当月估算)"
            if row["is_estimate"]
            else f"{row['date']:%Y-%m}"
        )
        hs_text = f"{row['hs300_close']:,.2f}" if pd.notna(row["hs300_close"]) else "--"
        recent_rows.append(
            f'<tr>'
            f'<td>{d_text}</td>'
            f'<td class="num"><strong style="color:{clr}">{row["buffett_index_pct"]:.2f}%</strong></td>'
            f'<td class="num">{pct_val:.1f}%</td>'
            f'<td><span class="bf-badge" style="color:{clr};background:{bg}">{lbl}</span></td>'
            f'<td class="num">{row["total_mcap_yi"] / 1e4:.2f} 万亿</td>'
            f'<td class="num">{row["sh_mcap_yi"] / 1e4:.2f} 万亿</td>'
            f'<td class="num">{row["sz_mcap_yi"] / 1e4:.2f} 万亿</td>'
            f'<td class="num">{row["gdp_ttm_yi"] / 1e4:.2f} 万亿</td>'
            f'<td class="num">{hs_text}</td>'
            f'</tr>'
        )

    payload_json = json.dumps(
        {
            "dates": [d.strftime("%Y-%m-%d") for d in frame["date"]],
            "buffett": [round(float(v), 2) for v in frame["buffett_index_pct"]],
            "pct10y": [round(float(v), 1) for v in frame["pct_10y"]],
            "q20": [round(float(v), 2) for v in frame["q20_10y"]],
            "q40": [round(float(v), 2) for v in frame["q40_10y"]],
            "q60": [round(float(v), 2) for v in frame["q60_10y"]],
            "q80": [round(float(v), 2) for v in frame["q80_10y"]],
            "mcap": [round(float(v), 2) for v in frame["total_mcap_yi"]],
            "gdp": [round(float(v), 2) for v in frame["gdp_ttm_yi"]],
            "hs300": [round(float(v), 2) if pd.notna(v) else None for v in frame["hs300_close"]],
            "est": [bool(v) for v in frame["is_estimate"]],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    latest_tag = "当月估算" if meta["latest_is_estimate"] else "官方月度"
    doc = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>巴菲特指数（沪深股票市值总额 / GDP）</title>
<link rel="stylesheet" href="/assets/market-site.css">
<style>
body{{margin:0;background:#f6f8fb;color:#182b49;font:14px -apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif}}
main{{max-width:1140px;margin:auto;padding:24px 20px 56px}}
h1{{margin:0;font-size:26px}}h2{{font-size:17px;margin:0}}
.sub,.note,small{{color:#68778d}}
.bf-top-bar{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}}
.bf-back-link{{color:#2869b2;text-decoration:none;font-weight:600;font-size:13px;white-space:nowrap}}
.bf-back-link:hover{{text-decoration:underline}}
.bf-kpis{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin:16px 0}}
.bf-card,section{{background:#fff;border:1px solid #e6edf5;border-radius:12px;padding:18px;box-sizing:border-box}}
section{{margin-top:16px}}
.bf-card .val{{font-size:26px;font-weight:750;margin:6px 0;font-variant-numeric:tabular-nums}}
.bf-badge{{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700}}
.bf-section-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}}
.bf-toolbar{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.bf-btn-group{{display:inline-flex;border:1px solid #d9e3f0;border-radius:8px;overflow:hidden;background:#f8fafd}}
.bf-btn-group button{{appearance:none;border:0;background:transparent;padding:5px 10px;font:inherit;font-size:12px;color:#475569;cursor:pointer;font-weight:600}}
.bf-btn-group button[aria-pressed="true"]{{background:#1d4ed8;color:#fff}}
.bf-btn-group button:hover:not([aria-pressed="true"]){{background:#eaf0f8}}
.bf-chip{{appearance:none;border:1px solid #d9e3f0;border-radius:999px;padding:4px 10px;background:#fff;font:inherit;font-size:12px;color:#334155;cursor:pointer;display:inline-flex;align-items:center;gap:6px}}
.bf-chip i{{width:9px;height:9px;border-radius:50%;display:inline-block}}
.bf-chip[aria-pressed="false"]{{opacity:.45;text-decoration:line-through}}
.bf-action-btn{{appearance:none;border:1px solid #cdd9f0;border-radius:7px;padding:5px 11px;background:#f5f8ff;color:#1d4ed8;font:inherit;font-size:12px;font-weight:600;cursor:pointer}}
.bf-action-btn:hover{{background:#e8f0fe}}
.bf-range-text{{font-size:12px;color:#68778d;font-variant-numeric:tabular-nums}}
.bf-svg{{width:100%;display:block;user-select:none;-webkit-user-select:none;cursor:crosshair}}
.bf-svg.is-zoomed{{cursor:grab}}.bf-svg.is-dragging{{cursor:grabbing}}
.bf-svg text{{font-size:11px;fill:#68778d}}
.bf-axis{{stroke:#e2e8f0;stroke-width:1}}
.bf-ref-line{{stroke-width:1.2;stroke-dasharray:5 4;opacity:.75}}
.bf-ref-text{{font-size:10px!important;font-weight:700;paint-order:stroke;stroke:#fff;stroke-width:3px}}
.bf-extrema-text{{font-size:11px!important;font-weight:750;fill:#1d4ed8!important;paint-order:stroke;stroke:#fff;stroke-width:3px}}
.bf-line-main{{fill:none;stroke:#2563eb;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}}
.bf-line-idx{{fill:none;stroke:#dc2626;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;opacity:.85}}
.bf-crosshair{{stroke:#64748b;stroke-width:1;stroke-dasharray:4 3;pointer-events:none}}
.bf-bar-mcap{{fill:#2563eb;opacity:.85}}.bf-bar-gdp{{fill:#f59e0b;opacity:.85}}
.bf-bar-group:hover .bf-bar-mcap,.bf-bar-group:hover .bf-bar-gdp{{opacity:1}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:9px 8px;border-bottom:1px solid #edf1f6;text-align:left}}
th{{color:#68778d;font-weight:500}}td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums}}
.bf-formula{{margin:10px 0;padding:12px 14px;border-left:4px solid #2563eb;border-radius:8px;background:#f5f8ff;font-size:13px;line-height:1.8}}
.bf-list{{margin:8px 0 0 18px;padding:0;line-height:1.8;font-size:13px;color:#334155}}
.bf-note{{font-size:12px;color:#68778d;line-height:1.6;margin:10px 0 0}}
.bf-popover{{position:fixed;z-index:300;min-width:225px;max-width:300px;padding:11px 13px;border:1px solid #d5e0ee;border-radius:10px;background:rgba(255,255,255,.97);box-shadow:0 10px 28px rgba(15,23,42,.18);pointer-events:none;font-size:12px}}
.bf-popover[hidden]{{display:none}}
.bf-popover-head{{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:6px;padding-bottom:5px;border-bottom:1px solid #edf1f6}}
.bf-tip-row{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:4px 0;font-variant-numeric:tabular-nums}}
.bf-tip-row span{{display:inline-flex;align-items:center;gap:6px;color:#475569}}
.bf-tip-row i{{width:8px;height:8px;border-radius:50%;display:inline-block}}
@media(max-width:960px){{.bf-kpis{{grid-template-columns:repeat(2,1fr)}}}}
@media(max-width:600px){{.bf-kpis{{grid-template-columns:1fr}}}}
</style>
<body><main>
<!-- SITE_NAVIGATION -->
<div class="bf-top-bar">
  <div>
    <h1>巴菲特指数（沪深股票市值总额 / GDP）</h1>
    <p class="sub" style="margin:5px 0 0;line-height:1.6">衡量 A 股证券化率与整体市场相对宏观经济体量的历史位置（数据自 {meta["first_date"]} 起，共 {meta["total_points"]} 期；历史极值 {meta["min_val"]:.2f}% ~ {meta["max_val"]:.2f}%）。</p>
  </div>
  <a class="bf-back-link" href="/">← 返回市场仪表盘</a>
</div>

<div class="bf-kpis">
  <div class="bf-card"><small>最新数据日期</small><div class="val" style="font-size:22px">{meta["latest_date"]}</div><small>{latest_tag} · 上期官方 {meta["latest_official_month"]} ({meta["latest_official_pct"]:.2f}%)</small></div>
  <div class="bf-card"><small>巴菲特指数（总市值/GDP）</small><div class="val" style="color:{zone_color}">{meta["buffett_index_pct"]:.2f}%</div><span class="bf-badge" style="color:{zone_color};background:{zone_bg}">{zone_label}</span></div>
  <div class="bf-card"><small>历史分位（近10年 / 全历史）</small><div class="val" style="color:#2563eb">{meta["pct_10y"]:.1f}%</div><small>2008年至今分位 {meta["pct_all"]:.1f}%</small></div>
  <div class="bf-card"><small>沪深股票总市值</small><div class="val">{meta["total_mcap_wan_yi"]:.2f}万亿</div><small>沪 {meta["sh_mcap_wan_yi"]:.2f}万亿 · 深 {meta["sz_mcap_wan_yi"]:.2f}万亿</small></div>
  <div class="bf-card"><small>滚动4季度 GDP（TTM）</small><div class="val">{meta["gdp_ttm_wan_yi"]:.2f}万亿</div><small>截至 {html.escape(meta["gdp_period"])}</small></div>
</div>

<section>
  <div class="bf-section-head">
    <h2>一、巴菲特指数（沪深股票市价总值 / GDP）与沪深300历史走势</h2>
    <div class="bf-toolbar">
      <div class="bf-btn-group" role="group" aria-label="时间区间">
        <button type="button" data-range-years="all" aria-pressed="true">全部(2008+)</button>
        <button type="button" data-range-years="10" aria-pressed="false">最近10年</button>
        <button type="button" data-range-years="5" aria-pressed="false">最近5年</button>
        <button type="button" data-range-years="3" aria-pressed="false">最近3年</button>
      </div>
      <span class="bf-range-text" data-chart-range></span>
      <button type="button" class="bf-action-btn" data-chart-reset hidden>重置视图</button>
      <button type="button" class="bf-action-btn" id="bf-download-csv">导出 CSV</button>
    </div>
  </div>
  <div class="bf-toolbar" style="margin-bottom:8px">
    <button type="button" class="bf-chip" data-series-toggle="main" aria-pressed="true"><i style="background:#2563eb"></i>沪深总市值 / GDP（左轴，最新 {meta["buffett_index_pct"]:.2f}%）</button>
    <button type="button" class="bf-chip" data-series-toggle="index" aria-pressed="true"><i style="background:#dc2626"></i>沪深300指数（右轴）</button>
    <button type="button" class="bf-chip" data-series-toggle="bands" aria-pressed="true"><i style="background:#94a3b8"></i>近10年五档分位背景 &amp; 参考线</button>
    <span style="display:inline-flex;align-items:center;gap:8px;font-size:11px;color:#475569;margin-left:auto;flex-wrap:wrap">
      <span style="display:inline-flex;align-items:center;gap:3px"><i style="width:10px;height:10px;border-radius:2px;background:rgba(220,38,38,.42);display:inline-block"></i>高位警示 ≥80%</span>
      <span style="display:inline-flex;align-items:center;gap:3px"><i style="width:10px;height:10px;border-radius:2px;background:rgba(251,113,133,.32);display:inline-block"></i>偏高 60–80%</span>
      <span style="display:inline-flex;align-items:center;gap:3px"><i style="width:10px;height:10px;border-radius:2px;background:rgba(245,158,11,.42);display:inline-block"></i>合理 40–60%</span>
      <span style="display:inline-flex;align-items:center;gap:3px"><i style="width:10px;height:10px;border-radius:2px;background:rgba(132,204,22,.48);display:inline-block"></i>偏低 20–40%</span>
      <span style="display:inline-flex;align-items:center;gap:3px"><i style="width:10px;height:10px;border-radius:2px;background:rgba(16,185,129,.45);display:inline-block"></i>显著偏低 &lt;20%</span>
    </span>
  </div>
  <svg id="bf-main-svg" class="bf-svg" viewBox="0 0 1080 410">
    <defs><clipPath id="bf-plot-clip"><rect x="62" y="26" width="954" height="342"/></clipPath></defs>
    <g data-zone-bands clip-path="url(#bf-plot-clip)"></g>
    <line class="bf-axis" x1="62" y1="26" x2="1016" y2="26"/><line class="bf-axis" x1="62" y1="111.5" x2="1016" y2="111.5"/><line class="bf-axis" x1="62" y1="197" x2="1016" y2="197"/><line class="bf-axis" x1="62" y1="282.5" x2="1016" y2="282.5"/><line class="bf-axis" x1="62" y1="368" x2="1016" y2="368"/>
    <text x="54" y="30" text-anchor="end" data-ly="0"></text><text x="54" y="115" text-anchor="end" data-ly="1"></text><text x="54" y="201" text-anchor="end" data-ly="2"></text><text x="54" y="286" text-anchor="end" data-ly="3"></text><text x="54" y="372" text-anchor="end" data-ly="4"></text>
    <text x="1024" y="30" data-ry="0"></text><text x="1024" y="115" data-ry="1"></text><text x="1024" y="201" data-ry="2"></text><text x="1024" y="286" data-ry="3"></text><text x="1024" y="372" data-ry="4"></text>
    <line class="bf-ref-line" data-p80-line x1="62" x2="1016" stroke="#dc2626" hidden/><text class="bf-ref-text" data-p80-text x="68" fill="#b91c1c" hidden></text>
    <line class="bf-ref-line" data-p50-line x1="62" x2="1016" stroke="#64748b" hidden/><text class="bf-ref-text" data-p50-text x="68" fill="#475569" hidden></text>
    <line class="bf-ref-line" data-p20-line x1="62" x2="1016" stroke="#16a34a" hidden/><text class="bf-ref-text" data-p20-text x="68" fill="#15803d" hidden></text>
    <path class="bf-line-idx" data-line="index"/>
    <path class="bf-line-main" data-line="main"/>
    <g data-extrema></g>
    <line class="bf-crosshair" data-crosshair x1="0" y1="26" x2="0" y2="368" hidden/>
    <g data-dates></g>
  </svg>
  <p class="bf-note">左轴：沪深股票市价总值 ÷ 滚动4季度 GDP（%）· 右轴：沪深300收盘点位。历史最高 {meta["max_val"]:.2f}%（{meta["max_date"]}），最低 {meta["min_val"]:.2f}%（{meta["min_date"]}）。支持 <b>Ctrl + 滚轮缩放</b>、<b>左键拖动平移</b> 与 <b>鼠标悬停实时查值</b>。</p>
</section>

<section>
  <div class="bf-section-head">
    <h2>二、历年沪深股票市价总值与国内生产总值（GDP）对比</h2>
    <div class="bf-toolbar" style="font-size:12px;color:#475569">
      <span style="display:inline-flex;align-items:center;gap:5px"><i style="width:10px;height:10px;border-radius:2px;background:#2563eb;display:inline-block"></i>沪深股票总市值（万亿元）</span>
      <span style="display:inline-flex;align-items:center;gap:5px"><i style="width:10px;height:10px;border-radius:2px;background:#f59e0b;display:inline-block"></i>全年/滚动 GDP（万亿元）</span>
    </div>
  </div>
  {annual_svg}
  <p class="bf-note">展示 2008 年至最新年度（带 * 号为当前年度最新估值估算）的年末沪深股票总市值与 GDP 规模对比，鼠标悬停柱组可查看当年具体数值与巴菲特指数。</p>
</section>

<section>
  <div class="bf-section-head"><h2>三、最近 12 期巴菲特指数明细</h2><small>按时间倒序排列</small></div>
  <table>
    <thead><tr><th>统计期</th><th class="num">巴菲特指数</th><th class="num">近10年分位</th><th>历史区间</th><th class="num">沪深总市值</th><th class="num">上交所总市值</th><th class="num">深交所总市值</th><th class="num">滚动4季GDP</th><th class="num">沪深300</th></tr></thead>
    <tbody>{''.join(recent_rows)}</tbody>
  </table>
</section>

<section>
  <h2>四、巴菲特指数起源、口径说明与 GDP/GNP 区别</h2>
  <div class="bf-formula">
    <strong>计算公式：</strong><code>中国巴菲特指数（证券化率） = 沪深两市股票市价总值（含 A、B 股） ÷ 中国最近4季度滚动 GDP × 100%</code>
  </div>
  <ul class="bf-list">
    <li><strong>指标起源：</strong>2001 年 12 月，沃伦·巴菲特在《财富》杂志发表《巴菲特论股票市场》一文，指出“所有公开交易股票总市值与国民生产总值（GNP）的比率，可能是衡量任何时刻股市估值水平的最佳单一指标”。在美股历史上，该比率在 1975 年熊市附近约 75%，2000 年互联网泡沫高点达 183%，2007 年次贷危机前约 135%，2009 年底部回落至 50% 附近。</li>
    <li><strong>GDP 与 GNP 的区别：</strong>由于中国宏观数据以国内生产总值（GDP）定期披露为主，A 股巴菲特指数普遍用 GDP 替代 GNP。<strong>GDP</strong> 是地域概念（一国境内生产的全部最终产品与服务价值），<strong>GNP</strong> 是国民概念（<code>GNP = GDP + 本国公民在国外创造的价值 − 外国公民在本国创造的价值</code>），两者在中国体量接近。</li>
    <li><strong>如何正确看待中国巴菲特指数：</strong>随着注册制推进、新股持续上市以及产业证券化率提升，A 股总市值/GDP 的长期中枢呈缓慢上移趋势，因此本页「历史区间」按<strong>近 10 年滚动分位</strong>（无未来函数）划分为五档：<code>&lt;20%</code> 显著偏低、<code>20%–40%</code> 偏低区间、<code>40%–60%</code> 合理区间、<code>60%–80%</code> 偏高区间、<code>≥80%</code> 高位警示，并与估值（PE-TTM）及股债利差口径保持一致，避免机械套用固定绝对阈值。</li>
    <li><strong>数据来源：</strong>沪深两市月度股票市价总值来自东方财富全国股票交易统计表（<code>RPT_ECONOMY_STOCK_STATISTICS</code>，源自交易所与证监会统计），中国季度 GDP 来自东方财富宏观数据表（<code>RPT_ECONOMY_GDP</code>，源自国家统计局），当月未公布月末数据时按中证全指涨跌幅同步估算；本页仅作历史数据观察，不构成投资建议。</li>
  </ul>
</section>

<div id="bf-popover" class="bf-popover" hidden>
  <div class="bf-popover-head"><strong data-tip-title></strong><small data-tip-date></small></div>
  <div data-tip-rows></div>
</div>
<script type="application/json" id="bf-data">{payload_json}</script>
{BUFFETT_SCRIPT}
</main></body></html>'''

    tmp = output_html.with_name(f".{output_html.name}.tmp")
    tmp.write_text(doc, encoding="utf-8")
    tmp.replace(output_html)


def build_buffett_index(
    mcap_cache: Path = DEFAULT_MCAP_CACHE,
    gdp_cache: Path = DEFAULT_GDP_CACHE,
    hs300_cache: Path = DEFAULT_HS300_CACHE,
    output_csv: Path = DEFAULT_HISTORY_CSV,
    output_html: Path = DEFAULT_OUTPUT_HTML,
    force: bool = False,
) -> dict:
    """执行完整采集、计算、CSV 导出与 HTML 渲染，返回最新快照元数据。"""
    mcap_df = load_or_update_mcap(mcap_cache, force=force)
    gdp_df = load_or_update_gdp(gdp_cache, force=force)
    hs300_df = _load_hs300_closes(hs300_cache)
    frame, annual, meta = calculate_buffett_history(mcap_df, gdp_df, hs300_df)
    save_history_csv(frame, output_csv)
    render_buffett_page(frame, annual, meta, output_html)
    return meta


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成中国巴菲特指数（沪深股票市值总额/GDP）页面")
    parser.add_argument("--force", action="store_true", help="强制重新拉取数据缓存")
    parser.add_argument("--output-html", default=str(DEFAULT_OUTPUT_HTML))
    parser.add_argument("--output-csv", default=str(DEFAULT_HISTORY_CSV))
    args = parser.parse_args()
    snap = build_buffett_index(
        output_csv=Path(args.output_csv),
        output_html=Path(args.output_html),
        force=args.force,
    )
    print(
        f"buffett-index written: {args.output_html}; csv={args.output_csv}; "
        f"points={snap['total_points']} ({snap['first_date']}~{snap['latest_date']}); "
        f"latest={snap['buffett_index_pct']:.2f}% (10y_pct={snap['pct_10y']:.1f}%, all_pct={snap['pct_all']:.1f}%)"
    )
