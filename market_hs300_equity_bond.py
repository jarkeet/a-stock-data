"""沪深300 & 中证500 股债利差（ERP）与股债比走势图：采集、计算与交互页面生成。"""
from __future__ import annotations

import argparse
from datetime import datetime
import html
import json
from pathlib import Path
import random
import time

import numpy as np
import pandas as pd
import requests


EASTMONEY_DATA_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
CSI_PERF_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
DANJUAN_PE_URL = "https://danjuanfunds.com/djapi/index_eva/pe_history/{symbol}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"

DEFAULT_CSI300_CSV = Path("market_valuation_csi300.csv")
DEFAULT_CSI500_CSV = Path("market_valuation_csi500.csv")
DEFAULT_HS300_MCW_CACHE = Path("market_pe_ttm_mcw_hs300.csv")
DEFAULT_ZZ500_MCW_CACHE = Path("market_pe_ttm_mcw_zz500.csv")
DEFAULT_BOND_CACHE = Path("market_cn10y_bond_yield_cache.csv")
DEFAULT_HS300_HISTORY_CSV = Path("market_hs300_equity_bond_history.csv")
DEFAULT_ZZ500_HISTORY_CSV = Path("market_zz500_equity_bond_history.csv")
DEFAULT_OUTPUT_HTML = Path("market_hs300_equity_bond.html")

PERCENTILE_WINDOWS = [
    ("10y", "近10年", 10),
    ("5y", "近5年", 5),
    ("3y", "近3年", 3),
    ("1y", "近1年", 1),
]


def _fetch_cn10y_bond_yield(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """从东财中债国债收益率表分页拉取中国10年期国债收益率（%）。"""
    headers = {
        "User-Agent": UA,
        "Referer": "https://data.eastmoney.com/cjsj/zgzs.html",
    }
    frames = []
    page = 1
    while True:
        params = {
            "reportName": "RPTA_WEB_TREASURYYIELD",
            "columns": "SOLAR_DATE,EMM00166466",
            "source": "WEB",
            "sortColumns": "SOLAR_DATE",
            "sortTypes": "-1",
            "pageNumber": str(page),
            "pageSize": "500",
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
        rows = result.get("data") or []
        if not payload or not payload.get("success") or not rows:
            if page == 1:
                raise ValueError(f"东财未返回10年期国债收益率数据：{payload}")
            break
        raw = pd.DataFrame(rows)
        chunk = pd.DataFrame(
            {
                "date": pd.to_datetime(raw["SOLAR_DATE"], errors="coerce").dt.normalize(),
                "cn10y_yield_pct": pd.to_numeric(raw["EMM00166466"], errors="coerce"),
            }
        ).dropna()
        if chunk.empty:
            break
        frames.append(chunk)
        total_pages = int(result.get("pages") or page)
        if chunk["date"].min() <= start or page >= total_pages:
            break
        page += 1
        time.sleep(0.25 + random.random() * 0.15)
    frame = (
        pd.concat(frames, ignore_index=True)
        .sort_values("date")
        .drop_duplicates("date")
    )
    return frame[frame["date"].between(start, end)].reset_index(drop=True)


def load_or_update_bond_yield(
    cache_path: Path = DEFAULT_BOND_CACHE,
    start_date: str = "2011-01-01",
    force: bool = False,
) -> pd.DataFrame:
    """加载并增量更新中国 10 年期国债收益率本地缓存。"""
    end_ts = pd.Timestamp.now().normalize()
    start_ts = pd.Timestamp(start_date).normalize()
    existing = pd.DataFrame()
    if cache_path.exists() and not force:
        existing = pd.read_csv(cache_path, parse_dates=["date"])
        existing["date"] = existing["date"].dt.normalize()

    if not existing.empty and existing["date"].min() <= start_ts + pd.DateOffset(days=30):
        fetch_start = existing["date"].max() - pd.DateOffset(days=30)
    else:
        fetch_start = start_ts

    fresh = _fetch_cn10y_bond_yield(fetch_start, end_ts)
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


def load_or_update_csi_valuation(
    index_code: str,
    cache_path: Path,
    start_date: str = "2011-01-01",
    force: bool = False,
) -> pd.DataFrame:
    """从中证指数官网拉取并缓存指定指数（如 000905）收盘价与官方 PE。"""
    today = pd.Timestamp.now().normalize()
    existing = pd.DataFrame()
    if cache_path.exists() and not force:
        existing = pd.read_csv(cache_path, parse_dates=["date"])
        existing["date"] = existing["date"].dt.normalize()
        # 当天已经刷新过缓存则直接复用
        mtime = datetime.fromtimestamp(cache_path.stat().st_mtime).date()
        if mtime == datetime.now().date() and not existing.empty:
            return existing

    fetch_start = (
        (existing["date"].max() - pd.DateOffset(days=30)).strftime("%Y%m%d")
        if not existing.empty and existing["date"].min() <= pd.Timestamp("2011-07-15")
        else pd.Timestamp(start_date).strftime("%Y%m%d")
    )
    fetch_end = today.strftime("%Y%m%d")
    payload = None
    for attempt in range(3):
        try:
            resp = requests.get(
                CSI_PERF_URL,
                params={"indexCode": index_code, "startDate": fetch_start, "endDate": fetch_end},
                headers={"User-Agent": UA},
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
            break
        except Exception:
            if attempt == 2:
                if not existing.empty:
                    return existing
                raise
            time.sleep(0.6 * (attempt + 1))

    rows = (payload or {}).get("data") or []
    fresh = pd.DataFrame(
        {
            "date": pd.to_datetime([r.get("tradeDate") for r in rows], format="%Y%m%d", errors="coerce"),
            "code": index_code,
            "close": pd.to_numeric([r.get("close") for r in rows], errors="coerce"),
            "pe": pd.to_numeric([r.get("peg") for r in rows], errors="coerce"),
        }
    ).dropna(subset=["date", "close", "pe"])
    fresh = fresh[fresh["pe"] > 0]

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


def load_or_update_danjuan_pe_ttm(
    symbol: str,
    cache_path: Path,
    force: bool = False,
) -> pd.DataFrame:
    """从蛋卷基金获取市值加权整体法 PE-TTM（含亏损成分股，近10年周频+最新日）。"""
    existing = pd.DataFrame()
    if cache_path.exists() and not force:
        existing = pd.read_csv(cache_path, parse_dates=["date"])
        existing["date"] = existing["date"].dt.normalize()
        mtime = datetime.fromtimestamp(cache_path.stat().st_mtime).date()
        if mtime == datetime.now().date() and not existing.empty:
            return existing

    payload = None
    for attempt in range(3):
        try:
            resp = requests.get(
                DANJUAN_PE_URL.format(symbol=symbol),
                params={"day": "all"},
                headers={"User-Agent": UA},
                timeout=20,
            )
            resp.raise_for_status()
            payload = resp.json()
            break
        except Exception:
            if attempt == 2:
                if not existing.empty:
                    return existing
                raise
            time.sleep(0.5 * (attempt + 1))

    rows = ((payload or {}).get("data") or {}).get("index_eva_pe_growths") or []
    fresh = pd.DataFrame(
        {
            "date": [
                pd.Timestamp(datetime.fromtimestamp(r["ts"] / 1000)).normalize()
                for r in rows
                if r.get("ts") is not None
            ],
            "pe": [
                pd.to_numeric(r.get("pe"), errors="coerce")
                for r in rows
                if r.get("ts") is not None
            ],
        }
    ).dropna(subset=["date", "pe"])
    fresh = fresh[fresh["pe"] > 0]
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


def _weak_percentile_by_years(frame: pd.DataFrame, col: str, years: int | None) -> float:
    """按自然年回溯计算最新值在窗口内的弱秩百分位（0–100%）。"""
    sub = frame[["date", col]].dropna(subset=[col])
    if sub.empty:
        return float("nan")
    if years is not None:
        cutoff = sub["date"].max() - pd.DateOffset(years=years)
        sub = sub[sub["date"] >= cutoff]
    latest = float(sub[col].iloc[-1])
    return float((sub[col] <= latest).mean() * 100.0)


def calculate_equity_bond_history(
    pe_df: pd.DataFrame,
    close_df: pd.DataFrame,
    bond_df: pd.DataFrame,
    code: str,
    name: str,
) -> tuple[pd.DataFrame, dict]:
    """合并整体法 PE-TTM、指数收盘价与10年期国债收益率，计算股债利差与股债比。"""
    pe_clean = pe_df[["date", "pe"]].copy()
    pe_clean["date"] = pd.to_datetime(pe_clean["date"], errors="coerce").dt.normalize()
    pe_clean["pe"] = pd.to_numeric(pe_clean["pe"], errors="coerce")
    pe_clean = (
        pe_clean.dropna(subset=["date", "pe"])
        .query("pe > 0")
        .sort_values("date")
        .drop_duplicates("date")
        .reset_index(drop=True)
    )

    closes = close_df[["date", "close"]].copy()
    closes["date"] = pd.to_datetime(closes["date"], errors="coerce").dt.normalize()
    closes["close"] = pd.to_numeric(closes["close"], errors="coerce")
    closes = closes.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date")

    bond = bond_df[["date", "cn10y_yield_pct"]].sort_values("date").drop_duplicates("date")

    frame = pd.merge_asof(
        pe_clean,
        closes,
        on="date",
        direction="backward",
        tolerance=pd.Timedelta(days=10),
    )
    frame = pd.merge_asof(
        frame,
        bond,
        on="date",
        direction="backward",
        tolerance=pd.Timedelta(days=10),
    ).dropna(subset=["close", "cn10y_yield_pct"]).reset_index(drop=True)

    frame["earnings_yield_pct"] = 100.0 / frame["pe"]
    frame["equity_bond_spread_pct"] = frame["earnings_yield_pct"] - frame["cn10y_yield_pct"]
    frame["equity_bond_ratio"] = np.where(
        frame["cn10y_yield_pct"] > 0,
        frame["earnings_yield_pct"] / frame["cn10y_yield_pct"],
        np.nan,
    )

    percentiles = []
    for key, label, years in PERCENTILE_WINDOWS:
        percentiles.append(
            {
                "key": key,
                "label": label,
                "spread_pct": round(_weak_percentile_by_years(frame, "equity_bond_spread_pct", years), 2),
                "ratio_pct": round(_weak_percentile_by_years(frame, "equity_bond_ratio", years), 2),
            }
        )

    latest = frame.iloc[-1]
    meta = {
        "code": code,
        "name": name,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "first_date": frame["date"].iloc[0].strftime("%Y-%m-%d"),
        "latest_date": latest["date"].strftime("%Y-%m-%d"),
        "total_days": int(len(frame)),
        "close": round(float(latest["close"]), 2),
        "pe": round(float(latest["pe"]), 2),
        "earnings_yield_pct": round(float(latest["earnings_yield_pct"]), 2),
        "cn10y_yield_pct": round(float(latest["cn10y_yield_pct"]), 4),
        "spread_pct": round(float(latest["equity_bond_spread_pct"]), 2),
        "ratio": round(float(latest["equity_bond_ratio"]), 2),
        "spread_pct_10y": percentiles[0]["spread_pct"],
        "ratio_pct_10y": percentiles[0]["ratio_pct"],
        "percentiles": percentiles,
    }
    return frame, meta


def save_history_csv(frame: pd.DataFrame, output_csv: Path) -> None:
    """导出指定指数的股债利差与股债比历史 CSV。"""
    out = frame.copy()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    out["close"] = out["close"].round(2)
    out["pe"] = out["pe"].round(2)
    out["earnings_yield_pct"] = out["earnings_yield_pct"].round(4)
    out["cn10y_yield_pct"] = out["cn10y_yield_pct"].round(4)
    out["equity_bond_spread_pct"] = out["equity_bond_spread_pct"].round(4)
    out["equity_bond_ratio"] = out["equity_bond_ratio"].round(4)
    out.to_csv(output_csv, index=False, encoding="utf-8-sig")


EQUITY_BOND_SCRIPT = r"""<script>
(function(){
  var dataNode=document.getElementById("eb-data");
  if(!dataNode)return;
  var rootPayload=null;
  try{rootPayload=JSON.parse(dataNode.textContent||"null")}catch(e){return}
  if(!rootPayload)return;

  var SVG_NS="http://www.w3.org/2000/svg";
  var tip=document.getElementById("eb-popover");

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
    var head=tip.querySelector("[data-tip-title]");
    var dateEl=tip.querySelector("[data-tip-date]");
    var list=tip.querySelector("[data-tip-rows]");
    if(head)head.textContent=title;
    if(dateEl)dateEl.textContent=dateText;
    if(list){
      list.textContent="";
      rows.forEach(function(r){
        var item=document.createElement("div");
        item.className="eb-tip-row";
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

  function createDualAxisChart(payload,cfg){
    var svg=document.getElementById(cfg.svgId);
    if(!svg||!payload||!Array.isArray(payload.dates)||payload.dates.length<2)return null;
    var dates=payload.dates;
    var total=dates.length,maxIndex=total-1;
    var W=1080,H=390,L=62,R=64,T=24,B=42;
    var PW=W-L-R,PH=H-T-B;
    var minSpan=Math.min(12,maxIndex);
    var viewStart=0,viewEnd=maxIndex;
    var dateTimes=dates.map(function(d){return new Date(d+"T00:00:00").getTime()});
    var latestTime=dateTimes[maxIndex];

    function indexForYears(years){
      if(years==="all")return 0;
      var cutoff=latestTime-Number(years)*365.25*86400000;
      for(var i=0;i<=maxIndex;i++){
        if(dateTimes[i]>=cutoff)return i;
      }
      return 0;
    }

    var crosshair=svg.querySelector("[data-crosshair]");
    var datesGroup=svg.querySelector("[data-dates]");
    var leftAxisLabels=[0,1,2,3,4].map(function(i){return svg.querySelector('[data-ly="'+i+'"]')});
    var rightAxisLabels=[0,1,2,3,4].map(function(i){return svg.querySelector('[data-ry="'+i+'"]')});
    var p80Line=svg.querySelector("[data-p80-line]");
    var p50Line=svg.querySelector("[data-p50-line]");
    var p20Line=svg.querySelector("[data-p20-line]");
    var p80Text=svg.querySelector("[data-p80-text]");
    var p50Text=svg.querySelector("[data-p50-text]");
    var p20Text=svg.querySelector("[data-p20-text]");
    var mainPath=svg.querySelector('[data-line="main"]');
    var idxPath=svg.querySelector('[data-line="index"]');
    var section=svg.closest("section");
    var rangeEl=section?section.querySelector("[data-chart-range]"):null;
    var resetBtn=section?section.querySelector("[data-chart-reset]"):null;
    var rangeBtns=section?section.querySelectorAll("[data-range-years]"):[];
    var toggles=section?section.querySelectorAll("[data-series-toggle]"):[];

    var visible={main:true,index:true,bands:true};
    var leftMin=0,leftMax=1,rightMin=0,rightMax=1;

    function xFor(idx){return L+((idx-viewStart)/Math.max(viewEnd-viewStart,1e-9))*PW}
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

    function syncRangeButtons(){
      rangeBtns.forEach(function(btn){
        var y=btn.getAttribute("data-range-years");
        var targetStart=indexForYears(y);
        var active=Math.abs(viewStart-targetStart)<=1.5&&viewEnd>=maxIndex-1;
        btn.setAttribute("aria-pressed",String(active));
      });
    }

    function setRefLine(lineEl,textEl,val,labelPrefix){
      if(!lineEl||!textEl)return;
      if(!visible.bands||val===null||val<leftMin||val>leftMax){
        lineEl.setAttribute("hidden","");
        textEl.setAttribute("hidden","");
        return;
      }
      var y=yLeft(val).toFixed(1);
      lineEl.setAttribute("y1",y);
      lineEl.setAttribute("y2",y);
      lineEl.removeAttribute("hidden");
      textEl.setAttribute("y",String(Number(y)-4));
      textEl.textContent=labelPrefix+" "+cfg.formatMain(val);
      textEl.removeAttribute("hidden");
    }

    function render(){
      var i0=Math.max(0,Math.floor(viewStart));
      var i1=Math.min(maxIndex,Math.ceil(viewEnd));
      var lLo=Infinity,lHi=-Infinity,rLo=Infinity,rHi=-Infinity;
      var mainSlice=[];
      for(var i=i0;i<=i1;i++){
        var mv=cfg.mainValues[i],iv=payload.close[i];
        if(mv!==null&&mv!==undefined&&!isNaN(mv)){
          if(mv<lLo)lLo=mv;if(mv>lHi)lHi=mv;mainSlice.push(mv);
        }
        if(iv!==null&&iv!==undefined&&!isNaN(iv)){
          if(iv<rLo)rLo=iv;if(iv>rHi)rHi=iv;
        }
      }
      if(!isFinite(lLo)||!isFinite(lHi)){lLo=0;lHi=1}
      if(!isFinite(rLo)||!isFinite(rHi)){rLo=0;rHi=1}
      var lPad=Math.max((lHi-lLo)*0.1,0.2);
      leftMin=cfg.allowNegative?lLo-lPad:Math.max(0,lLo-lPad);
      leftMax=lHi+lPad;
      var rPad=Math.max((rHi-rLo)*0.08,rHi*0.005);
      rightMin=Math.max(0,rLo-rPad);
      rightMax=rHi+rPad;

      for(var k=0;k<5;k++){
        var frac=k/4;
        var lv=leftMax-(leftMax-leftMin)*frac;
        var rv=rightMax-(rightMax-rightMin)*frac;
        if(leftAxisLabels[k])leftAxisLabels[k].textContent=cfg.formatAxis(lv);
        if(rightAxisLabels[k])rightAxisLabels[k].textContent=Math.round(rv).toLocaleString("zh-CN");
      }

      mainSlice.sort(function(a,b){return a-b});
      setRefLine(p80Line,p80Text,quantile(mainSlice,0.8),"80%分位");
      setRefLine(p50Line,p50Text,quantile(mainSlice,0.5),"中位数");
      setRefLine(p20Line,p20Text,quantile(mainSlice,0.2),"20%分位");

      if(mainPath)mainPath.setAttribute("d",visible.main?buildPath(cfg.mainValues,i0,i1,yLeft):"");
      if(idxPath)idxPath.setAttribute("d",visible.index?buildPath(payload.close,i0,i1,yRight):"");

      if(datesGroup){
        datesGroup.textContent="";
        var span=Math.max(i1-i0,1);
        var count=Math.min(7,span+1);
        var seen={};
        for(var dIdx=0;dIdx<count;dIdx++){
          var idx=Math.round(i0+dIdx*span/Math.max(count-1,1));
          if(seen[idx])continue;
          seen[idx]=true;
          var txt=document.createElementNS(SVG_NS,"text");
          txt.setAttribute("x",xFor(idx).toFixed(1));
          txt.setAttribute("y",String(H-14));
          txt.setAttribute("text-anchor",dIdx===0?"start":(dIdx===count-1?"end":"middle"));
          txt.textContent=dates[idx];
          datesGroup.appendChild(txt);
        }
      }

      var isFull=viewStart<=0.5&&viewEnd>=maxIndex-0.5;
      svg.classList.toggle("is-zoomed",!isFull);
      if(resetBtn)resetBtn.hidden=isFull;
      if(rangeEl){
        var ratio=maxIndex/Math.max(viewEnd-viewStart,1);
        rangeEl.textContent=dates[i0]+" ~ "+dates[i1]+"（"+(i1-i0+1)+"个数据点"+(!isFull?" · "+ratio.toFixed(1)+"×":"")+"）";
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
      if(visible.main){
        rows.push({
          name:cfg.mainName,
          color:cfg.mainColor,
          value:cfg.formatMain(cfg.mainValues[idx]),
          valueColor:cfg.mainColor
        });
      }
      if(visible.index){
        rows.push({
          name:cfg.indexLabel,
          color:"#dc2626",
          value:fmt(payload.close[idx],2)+" 点"
        });
      }
      rows.push({name:"PE-TTM（市值加权）",color:"#64748b",value:fmt(payload.pe[idx],2)});
      rows.push({name:"盈利收益率 (100/PE)",color:"#0ea5e9",value:fmt(payload.ey[idx],2)+"%"});
      rows.push({name:"10年期国债收益率",color:"#f59e0b",value:fmt(payload.bond[idx],2)+"%"});
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

    rangeBtns.forEach(function(btn){
      btn.addEventListener("click",function(){
        var years=btn.getAttribute("data-range-years");
        viewEnd=maxIndex;
        viewStart=indexForYears(years);
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

  function bindCsvDownload(btnId,payload,filename){
    var btn=document.getElementById(btnId);
    if(!btn||!payload)return;
    btn.addEventListener("click",function(){
      var lines=["date,index_close,pe_ttm,earnings_yield_pct,cn10y_yield_pct,equity_bond_spread_pct,equity_bond_ratio"];
      for(var i=0;i<payload.dates.length;i++){
        lines.push([
          payload.dates[i],payload.close[i],payload.pe[i],payload.ey[i],payload.bond[i],payload.spread[i],payload.ratio[i]
        ].join(","));
      }
      var blob=new Blob(["\ufeff"+lines.join("\n")],{type:"text/csv;charset=utf-8;"});
      var url=URL.createObjectURL(blob);
      var a=document.createElement("a");
      a.href=url;
      a.download=filename;
      a.click();
      setTimeout(function(){URL.revokeObjectURL(url)},1000);
    });
  }

  // 沪深300 两张双轴图
  if(rootPayload.hs300){
    createDualAxisChart(rootPayload.hs300,{
      svgId:"eb-hs300-spread-svg",
      title:"沪深300股债利差（ERP）",
      mainName:"股债利差",
      indexLabel:"沪深300收盘",
      mainColor:"#2563eb",
      mainValues:rootPayload.hs300.spread,
      allowNegative:true,
      formatMain:function(v){return fmt(v,2)+"%"},
      formatAxis:function(v){return fmt(v,1)+"%"}
    });
    createDualAxisChart(rootPayload.hs300,{
      svgId:"eb-hs300-ratio-svg",
      title:"沪深300股债比",
      mainName:"股债比",
      indexLabel:"沪深300收盘",
      mainColor:"#7c3aed",
      mainValues:rootPayload.hs300.ratio,
      allowNegative:false,
      formatMain:function(v){return fmt(v,2)+"倍"},
      formatAxis:function(v){return fmt(v,1)+"倍"}
    });
    bindCsvDownload("eb-download-hs300",rootPayload.hs300,"hs300-equity-bond-spread-history.csv");
  }

  // 中证500 两张双轴图
  if(rootPayload.zz500){
    createDualAxisChart(rootPayload.zz500,{
      svgId:"eb-zz500-spread-svg",
      title:"中证500股债利差（ERP）",
      mainName:"股债利差",
      indexLabel:"中证500收盘",
      mainColor:"#0d9488",
      mainValues:rootPayload.zz500.spread,
      allowNegative:true,
      formatMain:function(v){return fmt(v,2)+"%"},
      formatAxis:function(v){return fmt(v,1)+"%"}
    });
    createDualAxisChart(rootPayload.zz500,{
      svgId:"eb-zz500-ratio-svg",
      title:"中证500股债比",
      mainName:"股债比",
      indexLabel:"中证500收盘",
      mainColor:"#9333ea",
      mainValues:rootPayload.zz500.ratio,
      allowNegative:false,
      formatMain:function(v){return fmt(v,2)+"倍"},
      formatAxis:function(v){return fmt(v,1)+"倍"}
    });
    bindCsvDownload("eb-download-zz500",rootPayload.zz500,"zz500-equity-bond-spread-history.csv");
  }
})();
</script>"""


def _series_json(series: pd.Series, digits: int = 2) -> list:
    return [round(float(v), digits) if pd.notna(v) else None for v in series]


def _pack_index_payload(frame: pd.DataFrame) -> dict:
    return {
        "dates": [d.strftime("%Y-%m-%d") for d in frame["date"]],
        "close": _series_json(frame["close"], 2),
        "pe": _series_json(frame["pe"], 2),
        "ey": _series_json(frame["earnings_yield_pct"], 2),
        "bond": _series_json(frame["cn10y_yield_pct"], 2),
        "spread": _series_json(frame["equity_bond_spread_pct"], 2),
        "ratio": _series_json(frame["equity_bond_ratio"], 2),
    }


def _render_index_block(
    key: str,
    meta: dict,
    spread_color: str,
    ratio_color: str,
) -> str:
    """渲染单个指数（沪深300或中证500）的百分位表 + 股债利差图 + 股债比图。"""
    pct_rows = []
    for item in meta["percentiles"]:
        sp = item["spread_pct"]
        rp = item["ratio_pct"]
        pct_rows.append(
            f'<tr>'
            f'<td><strong>{html.escape(item["label"])}</strong></td>'
            f'<td class="num"><strong style="color:{spread_color}">{sp:.2f}%</strong></td>'
            f'<td><div class="eb-bar"><i style="width:{min(100, max(0, sp)):.1f}%;background:{spread_color}"></i></div></td>'
            f'<td class="num"><strong style="color:{ratio_color}">{rp:.2f}%</strong></td>'
            f'<td><div class="eb-bar"><i style="width:{min(100, max(0, rp)):.1f}%;background:{ratio_color}"></i></div></td>'
            f'</tr>'
        )

    svg_axes = (
        '<line class="eb-axis" x1="62" y1="24" x2="1016" y2="24"/>'
        '<line class="eb-axis" x1="62" y1="105" x2="1016" y2="105"/>'
        '<line class="eb-axis" x1="62" y1="186" x2="1016" y2="186"/>'
        '<line class="eb-axis" x1="62" y1="267" x2="1016" y2="267"/>'
        '<line class="eb-axis" x1="62" y1="348" x2="1016" y2="348"/>'
        '<text x="54" y="28" text-anchor="end" data-ly="0"></text>'
        '<text x="54" y="109" text-anchor="end" data-ly="1"></text>'
        '<text x="54" y="190" text-anchor="end" data-ly="2"></text>'
        '<text x="54" y="271" text-anchor="end" data-ly="3"></text>'
        '<text x="54" y="352" text-anchor="end" data-ly="4"></text>'
        '<text x="1024" y="28" data-ry="0"></text>'
        '<text x="1024" y="109" data-ry="1"></text>'
        '<text x="1024" y="190" data-ry="2"></text>'
        '<text x="1024" y="271" data-ry="3"></text>'
        '<text x="1024" y="352" data-ry="4"></text>'
        '<line class="eb-ref-line" data-p80-line x1="62" x2="1016" stroke="#16a34a" hidden/>'
        '<text class="eb-ref-text" data-p80-text x="68" fill="#15803d" hidden></text>'
        '<line class="eb-ref-line" data-p50-line x1="62" x2="1016" stroke="#64748b" hidden/>'
        '<text class="eb-ref-text" data-p50-text x="68" fill="#475569" hidden></text>'
        '<line class="eb-ref-line" data-p20-line x1="62" x2="1016" stroke="#dc2626" hidden/>'
        '<text class="eb-ref-text" data-p20-text x="68" fill="#b91c1c" hidden></text>'
    )

    name = html.escape(meta["name"])
    code = html.escape(meta["code"])
    return f'''<div id="section-{key}" class="eb-index-block">
  <div class="eb-block-banner">
    <div><h2>{name}（{code}）股债利差与股债比</h2><small>历史自 {meta["first_date"]} 起（共 {meta["total_days"]:,} 个交易日）· 最新 {meta["latest_date"]} · 收盘 {meta["close"]:,.2f} · PE-TTM {meta["pe"]:.2f}（盈利收益率 {meta["earnings_yield_pct"]:.2f}%）· 10Y国债 {meta["cn10y_yield_pct"]:.2f}%</small></div>
    <button type="button" class="eb-action-btn" id="eb-download-{key}">导出 {name} CSV</button>
  </div>

  <section>
    <div class="eb-section-head">
      <h3>{name} 历史百分位（切换下方图表区间不改变百分位样本）</h3>
      <small>最新股债利差 <strong style="color:{spread_color}">{meta["spread_pct"]:.2f}%</strong> · 股债比 <strong style="color:{ratio_color}">{meta["ratio"]:.2f}倍</strong></small>
    </div>
    <table>
      <thead><tr><th>统计窗口</th><th class="num">股债利差历史分位</th><th>利差位置</th><th class="num">股债比历史分位</th><th>股债比位置</th></tr></thead>
      <tbody>{''.join(pct_rows)}</tbody>
    </table>
  </section>

  <section>
    <div class="eb-section-head">
      <h3>{name} 股债利差与指数历史走势</h3>
      <div class="eb-toolbar">
        <div class="eb-btn-group" role="group" aria-label="{name}利差时间区间">
          <button type="button" data-range-years="all" aria-pressed="true">全部数据(近10年)</button>
          <button type="button" data-range-years="5" aria-pressed="false">最近5年</button>
          <button type="button" data-range-years="3" aria-pressed="false">最近3年</button>
          <button type="button" data-range-years="1" aria-pressed="false">最近1年</button>
        </div>
        <span class="eb-range-text" data-chart-range></span>
        <button type="button" class="eb-action-btn" data-chart-reset hidden>重置视图</button>
      </div>
    </div>
    <div class="eb-toolbar" style="margin-bottom:8px">
      <button type="button" class="eb-chip" data-series-toggle="main" aria-pressed="true"><i style="background:{spread_color}"></i>股债利差（左轴，最新 {meta["spread_pct"]:.2f}%）</button>
      <button type="button" class="eb-chip" data-series-toggle="index" aria-pressed="true"><i style="background:#dc2626"></i>{name}点位（右轴）</button>
      <button type="button" class="eb-chip" data-series-toggle="bands" aria-pressed="true"><i style="background:#94a3b8"></i>区间 80% / 中位 / 20% 参考线</button>
    </div>
    <svg id="eb-{key}-spread-svg" class="eb-svg" viewBox="0 0 1080 390">
      {svg_axes}
      <path class="eb-line-idx" data-line="index"/>
      <path class="eb-line-main" data-line="main" stroke="{spread_color}"/>
      <line class="eb-crosshair" data-crosshair x1="0" y1="24" x2="0" y2="348" hidden/>
      <g data-dates></g>
    </svg>
    <p class="eb-note">左轴：{name}股债利差（盈利收益率 − 10年期国债收益率，%）· 右轴：{name}收盘点位。支持 <b>Ctrl + 滚轮缩放</b>、<b>左键拖动平移</b> 与 <b>鼠标移动实时查值</b>。</p>
  </section>

  <section>
    <div class="eb-section-head">
      <h3>{name} 股债比与指数历史走势</h3>
      <div class="eb-toolbar">
        <div class="eb-btn-group" role="group" aria-label="{name}股债比时间区间">
          <button type="button" data-range-years="all" aria-pressed="true">全部数据(近10年)</button>
          <button type="button" data-range-years="5" aria-pressed="false">最近5年</button>
          <button type="button" data-range-years="3" aria-pressed="false">最近3年</button>
          <button type="button" data-range-years="1" aria-pressed="false">最近1年</button>
        </div>
        <span class="eb-range-text" data-chart-range></span>
        <button type="button" class="eb-action-btn" data-chart-reset hidden>重置视图</button>
      </div>
    </div>
    <div class="eb-toolbar" style="margin-bottom:8px">
      <button type="button" class="eb-chip" data-series-toggle="main" aria-pressed="true"><i style="background:{ratio_color}"></i>股债比（左轴，最新 {meta["ratio"]:.2f}倍）</button>
      <button type="button" class="eb-chip" data-series-toggle="index" aria-pressed="true"><i style="background:#dc2626"></i>{name}点位（右轴）</button>
      <button type="button" class="eb-chip" data-series-toggle="bands" aria-pressed="true"><i style="background:#94a3b8"></i>区间 80% / 中位 / 20% 参考线</button>
    </div>
    <svg id="eb-{key}-ratio-svg" class="eb-svg" viewBox="0 0 1080 390">
      {svg_axes}
      <path class="eb-line-idx" data-line="index"/>
      <path class="eb-line-main" data-line="main" stroke="{ratio_color}"/>
      <line class="eb-crosshair" data-crosshair x1="0" y1="24" x2="0" y2="348" hidden/>
      <g data-dates></g>
    </svg>
    <p class="eb-note">左轴：{name}股债比（盈利收益率 ÷ 10年期国债收益率，倍）· 右轴：{name}收盘点位。</p>
  </section>
</div>'''


def render_equity_bond_page(
    hs300_frame: pd.DataFrame,
    hs300_meta: dict,
    zz500_frame: pd.DataFrame,
    zz500_meta: dict,
    output_html: Path = DEFAULT_OUTPUT_HTML,
) -> None:
    """渲染沪深300与中证500股债利差、股债比及完整读图说明页面。"""
    # 最近 20 个交易日双指数对照表
    merged_recent = (
        hs300_frame[["date", "close", "pe", "cn10y_yield_pct", "equity_bond_spread_pct", "equity_bond_ratio"]]
        .rename(
            columns={
                "close": "hs300_close",
                "pe": "hs300_pe",
                "equity_bond_spread_pct": "hs300_spread",
                "equity_bond_ratio": "hs300_ratio",
            }
        )
        .merge(
            zz500_frame[["date", "close", "pe", "equity_bond_spread_pct", "equity_bond_ratio"]].rename(
                columns={
                    "close": "zz500_close",
                    "pe": "zz500_pe",
                    "equity_bond_spread_pct": "zz500_spread",
                    "equity_bond_ratio": "zz500_ratio",
                }
            ),
            on="date",
            how="inner",
        )
        .tail(20)
        .iloc[::-1]
    )
    recent_rows = []
    for _, row in merged_recent.iterrows():
        recent_rows.append(
            f'<tr>'
            f'<td>{row["date"]:%Y-%m-%d}</td>'
            f'<td class="num">{row["cn10y_yield_pct"]:.2f}%</td>'
            f'<td class="num">{row["hs300_pe"]:.2f}</td>'
            f'<td class="num"><strong style="color:#2563eb">{row["hs300_spread"]:.2f}%</strong></td>'
            f'<td class="num"><strong style="color:#7c3aed">{row["hs300_ratio"]:.2f}倍</strong></td>'
            f'<td class="num">{row["zz500_pe"]:.2f}</td>'
            f'<td class="num"><strong style="color:#0d9488">{row["zz500_spread"]:.2f}%</strong></td>'
            f'<td class="num"><strong style="color:#9333ea">{row["zz500_ratio"]:.2f}倍</strong></td>'
            f'</tr>'
        )

    payload_json = json.dumps(
        {
            "hs300": _pack_index_payload(hs300_frame),
            "zz500": _pack_index_payload(zz500_frame),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    hs300_block = _render_index_block("hs300", hs300_meta, "#2563eb", "#7c3aed")
    zz500_block = _render_index_block("zz500", zz500_meta, "#0d9488", "#9333ea")

    doc = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>股债利差、股债比与股权风险溢价 ERP（沪深300 / 中证500）</title>
<link rel="stylesheet" href="/assets/market-site.css">
<style>
body{{margin:0;background:#f6f8fb;color:#182b49;font:14px -apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif}}
main{{max-width:1140px;margin:auto;padding:24px 20px 56px}}
h1{{margin:0;font-size:26px}}h2{{font-size:18px;margin:0}}h3{{font-size:16px;margin:0}}
.sub,.note,small{{color:#68778d}}
.eb-top-bar{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}}
.eb-back-link{{color:#2869b2;text-decoration:none;font-weight:600;font-size:13px;white-space:nowrap}}
.eb-back-link:hover{{text-decoration:underline}}
.eb-jump-nav{{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 14px}}
.eb-jump-nav a{{display:inline-flex;align-items:center;padding:6px 13px;border:1px solid #d5e0ee;border-radius:999px;background:#fff;color:#1e293b;text-decoration:none;font-size:13px;font-weight:600}}
.eb-jump-nav a:hover{{border-color:#2563eb;color:#2563eb;background:#f5f8ff}}
.eb-overview-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin:14px 0 20px}}
.eb-overview-card{{background:#fff;border:1px solid #e6edf5;border-radius:12px;padding:18px;box-sizing:border-box}}
.eb-overview-head{{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:12px}}
.eb-overview-head a{{font-size:12px;color:#2563eb;text-decoration:none;font-weight:600}}
.eb-overview-metrics{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}}
.eb-metric-item{{background:#f8fafd;border:1px solid #edf2f7;border-radius:9px;padding:10px 12px}}
.eb-metric-item .v{{font-size:22px;font-weight:750;margin:4px 0;font-variant-numeric:tabular-nums}}
.eb-index-block{{margin-top:26px}}
.eb-block-banner{{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px;padding-bottom:8px;border-bottom:2px solid #e2e8f0}}
section{{background:#fff;border:1px solid #e6edf5;border-radius:12px;padding:18px;box-sizing:border-box;margin-top:14px}}
.eb-section-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}}
.eb-toolbar{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.eb-btn-group{{display:inline-flex;border:1px solid #d9e3f0;border-radius:8px;overflow:hidden;background:#f8fafd}}
.eb-btn-group button{{appearance:none;border:0;background:transparent;padding:5px 10px;font:inherit;font-size:12px;color:#475569;cursor:pointer;font-weight:600}}
.eb-btn-group button[aria-pressed="true"]{{background:#1d4ed8;color:#fff}}
.eb-btn-group button:hover:not([aria-pressed="true"]){{background:#eaf0f8}}
.eb-chip{{appearance:none;border:1px solid #d9e3f0;border-radius:999px;padding:4px 10px;background:#fff;font:inherit;font-size:12px;color:#334155;cursor:pointer;display:inline-flex;align-items:center;gap:6px}}
.eb-chip i{{width:9px;height:9px;border-radius:50%;display:inline-block}}
.eb-chip[aria-pressed="false"]{{opacity:.45;text-decoration:line-through}}
.eb-action-btn{{appearance:none;border:1px solid #cdd9f0;border-radius:7px;padding:5px 11px;background:#f5f8ff;color:#1d4ed8;font:inherit;font-size:12px;font-weight:600;cursor:pointer}}
.eb-action-btn:hover{{background:#e8f0fe}}
.eb-range-text{{font-size:12px;color:#68778d;font-variant-numeric:tabular-nums}}
.eb-svg{{width:100%;display:block;user-select:none;-webkit-user-select:none;cursor:crosshair}}
.eb-svg.is-zoomed{{cursor:grab}}.eb-svg.is-dragging{{cursor:grabbing}}
.eb-svg text{{font-size:11px;fill:#68778d}}
.eb-axis{{stroke:#e2e8f0;stroke-width:1}}
.eb-ref-line{{stroke-width:1.2;stroke-dasharray:5 4;opacity:.75}}
.eb-ref-text{{font-size:10px!important;font-weight:700;paint-order:stroke;stroke:#fff;stroke-width:3px}}
.eb-line-main{{fill:none;stroke-width:2.3;stroke-linecap:round;stroke-linejoin:round}}
.eb-line-idx{{fill:none;stroke:#dc2626;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;opacity:.85}}
.eb-crosshair{{stroke:#64748b;stroke-width:1;stroke-dasharray:4 3;pointer-events:none}}
.eb-bar{{height:8px;background:#edf2f7;border-radius:999px;overflow:hidden;min-width:90px}}
.eb-bar i{{display:block;height:100%;border-radius:999px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:9px 8px;border-bottom:1px solid #edf1f6;text-align:left}}
th{{color:#68778d;font-weight:500}}td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums}}
.eb-guide-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:12px}}
.eb-guide-card{{background:#f8fafd;border:1px solid #e6edf5;border-radius:10px;padding:14px}}
.eb-guide-card b{{display:block;font-size:14px;margin-bottom:6px;color:#182b49}}
.eb-guide-card p{{margin:0;font-size:13px;line-height:1.65;color:#475569}}
.eb-formula-box{{margin:10px 0;padding:12px 14px;border-left:4px solid #2563eb;border-radius:8px;background:#f5f8ff;font-size:13px;line-height:1.8;color:#1e293b}}
.eb-formula-box code{{font-weight:700;color:#1d4ed8}}
.eb-list{{margin:8px 0 0 18px;padding:0;line-height:1.8;font-size:13px;color:#334155}}
.eb-faq-list{{display:flex;flex-direction:column;gap:8px;margin-top:10px}}
.eb-faq-list details{{border:1px solid #e6edf5;border-radius:9px;padding:11px 14px;background:#fcfdff}}
.eb-faq-list summary{{cursor:pointer;font-weight:700;font-size:13px;color:#182b49}}
.eb-faq-list details p{{margin:8px 0 0;font-size:13px;line-height:1.65;color:#475569}}
.eb-popover{{position:fixed;z-index:300;min-width:225px;max-width:300px;padding:11px 13px;border:1px solid #d5e0ee;border-radius:10px;background:rgba(255,255,255,.97);box-shadow:0 10px 28px rgba(15,23,42,.18);pointer-events:none;font-size:12px}}
.eb-popover[hidden]{{display:none}}
.eb-popover-head{{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:6px;padding-bottom:5px;border-bottom:1px solid #edf1f6}}
.eb-tip-row{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:4px 0;font-variant-numeric:tabular-nums}}
.eb-tip-row span{{display:inline-flex;align-items:center;gap:6px;color:#475569}}
.eb-tip-row i{{width:8px;height:8px;border-radius:50%;display:inline-block}}
.eb-note{{font-size:12px;color:#68778d;line-height:1.6;margin:10px 0 0}}
@media(max-width:900px){{.eb-overview-grid,.eb-guide-grid{{grid-template-columns:1fr}}}}
@media(max-width:600px){{.eb-overview-metrics{{grid-template-columns:1fr}}}}
</style>
<body><main>
<!-- SITE_NAVIGATION -->
<div class="eb-top-bar">
  <div>
    <h1>股债利差、股债比与股权风险溢价 ERP</h1>
    <p class="sub" style="margin:5px 0 0;line-height:1.6">提供沪深300（000300）与中证500（000905）股债利差、股债比与股权风险溢价 ERP 历史走势：股债利差 = 市值加权 PE-TTM 的盈利收益率（100/PE）减去中国十年期国债收益率，股债比 = 盈利收益率除以十年期国债收益率。</p>
  </div>
  <a class="eb-back-link" href="/">← 返回市场仪表盘</a>
</div>

<nav class="eb-jump-nav" aria-label="页内跳转">
  <a href="#section-hs300">沪深300（000300）图表</a>
  <a href="#section-zz500">中证500（000905）图表</a>
  <a href="#section-recent">双指数近20日对照</a>
  <a href="#section-reading">如何读图</a>
  <a href="#section-erp">股债利差与 ERP 关系</a>
  <a href="#section-howto">股债利差怎么看</a>
  <a href="#section-faq">常见问题</a>
</nav>

<div class="eb-overview-grid">
  <article class="eb-overview-card">
    <div class="eb-overview-head">
      <div><strong>沪深300</strong> <small>代码 000300 · 最新 {hs300_meta["latest_date"]}</small></div>
      <a href="#section-hs300">查看完整图表 ↓</a>
    </div>
    <div class="eb-overview-metrics">
      <div class="eb-metric-item"><small>股债利差（ERP）</small><div class="v" style="color:#2563eb">{hs300_meta["spread_pct"]:.2f}%</div><small>近10年分位 {hs300_meta["spread_pct_10y"]:.1f}%</small></div>
      <div class="eb-metric-item"><small>股债比</small><div class="v" style="color:#7c3aed">{hs300_meta["ratio"]:.2f}倍</div><small>近10年分位 {hs300_meta["ratio_pct_10y"]:.1f}%</small></div>
      <div class="eb-metric-item"><small>PE-TTM / 收盘</small><div class="v" style="font-size:19px">{hs300_meta["pe"]:.2f}</div><small>{hs300_meta["close"]:,.2f} 点 · 国债 {hs300_meta["cn10y_yield_pct"]:.2f}%</small></div>
    </div>
  </article>
  <article class="eb-overview-card">
    <div class="eb-overview-head">
      <div><strong>中证500</strong> <small>代码 000905 · 最新 {zz500_meta["latest_date"]}</small></div>
      <a href="#section-zz500">查看完整图表 ↓</a>
    </div>
    <div class="eb-overview-metrics">
      <div class="eb-metric-item"><small>股债利差（ERP）</small><div class="v" style="color:#0d9488">{zz500_meta["spread_pct"]:.2f}%</div><small>近10年分位 {zz500_meta["spread_pct_10y"]:.1f}%</small></div>
      <div class="eb-metric-item"><small>股债比</small><div class="v" style="color:#9333ea">{zz500_meta["ratio"]:.2f}倍</div><small>近10年分位 {zz500_meta["ratio_pct_10y"]:.1f}%</small></div>
      <div class="eb-metric-item"><small>PE-TTM / 收盘</small><div class="v" style="font-size:19px">{zz500_meta["pe"]:.2f}</div><small>{zz500_meta["close"]:,.2f} 点 · 国债 {zz500_meta["cn10y_yield_pct"]:.2f}%</small></div>
    </div>
  </article>
</div>

{hs300_block}

{zz500_block}

<section id="section-recent">
  <div class="eb-section-head"><h2>最近 20 个交易日双指数对照明细</h2><small>同日并列对比沪深300与中证500</small></div>
  <table>
    <thead>
      <tr>
        <th>日期</th><th class="num">10年期国债</th>
        <th class="num">沪深300 PE</th><th class="num">沪深300利差</th><th class="num">沪深300股债比</th>
        <th class="num">中证500 PE</th><th class="num">中证500利差</th><th class="num">中证500股债比</th>
      </tr>
    </thead>
    <tbody>{''.join(recent_rows)}</tbody>
  </table>
</section>

<section id="section-reading">
  <h2>如何读图</h2>
  <ul class="eb-list">
    <li>每个指数均提供<strong>股债利差</strong>与<strong>股债比</strong>两张双轴图：股债利差看盈利收益率减去十年期国债收益率（%），股债比看盈利收益率除以十年期国债收益率（倍）；红线均为对应指数的收盘点位。</li>
    <li>每个模块顶部的表格为对应指标在不同时间窗口（近10年、近5年、近3年、近1年、全历史）下的<strong>历史百分位</strong>，用于衡量当前数值在对应历史样本中的相对位置。</li>
    <li>切换图表上方的时间范围按钮，或按住 <b>Ctrl + 滚轮缩放</b>、<b>鼠标左键拖动平移</b>，仅改变图表展示区间与区间参考线，<strong>不会改变顶部历史百分位的计算样本定义</strong>。</li>
    <li>股债利差受盈利周期、利率水平与市场风险偏好共同影响，极端读数仅作历史位置参考，不构成投资建议。</li>
  </ul>
</section>

<section id="section-erp">
  <h2>股债利差和股权风险溢价 ERP 是什么关系？</h2>
  <p style="margin:10px 0 0;line-height:1.7;font-size:13px;color:#334155">本页展示的股债利差，使用沪深300、中证500指数的市值加权 PE-TTM 计算盈利收益率（<code>100 / PE-TTM</code>），再减去中国十年期国债收益率：</p>
  <div class="eb-formula-box">
    <div><code>股债利差（简化 ERP） = 100 / PE-TTM（市值加权） − 中国十年期国债收益率</code>（单位：%）</div>
    <div><code>股债比 = (100 / PE-TTM（市值加权）) ÷ 中国十年期国债收益率</code>（单位：倍）</div>
  </div>
  <p style="margin:8px 0 0;line-height:1.7;font-size:13px;color:#334155">它接近于一种简化的<strong>股权风险溢价（Equity Risk Premium, ERP）</strong>观察方式，用来衡量股票隐含盈利收益率相对于无风险利率的超额补偿。由于真实 ERP 还会受到未来盈利预期、风险偏好、利率期限结构和指数成分变化等因素影响，本页不把它视为完整的资产定价模型，只作为客观的历史相对位置观察工具。</p>
</section>

<section id="section-howto">
  <h2>股债利差怎么看？</h2>
  <div class="eb-guide-grid">
    <div class="eb-guide-card">
      <b>1. 利差较高</b>
      <p>表示股票盈利收益率相对十年期国债收益率的收益补偿更高，通常反映权益资产相对债券在历史上处于更有吸引力的配置区间。</p>
    </div>
    <div class="eb-guide-card">
      <b>2. 利差较低</b>
      <p>表示股票相对债券的超额收益补偿变小，权益性价比相对走弱，需要结合盈利周期、利率水平与市场风险偏好综合观察。</p>
    </div>
    <div class="eb-guide-card">
      <b>3. 结合历史百分位看位置</b>
      <p>历史百分位用于回答“当前利差在过去样本中处于什么位置”，而非对未来涨跌方向的确定预测；低利率环境下建议同时对照股债比倍数观察。</p>
    </div>
  </div>
</section>

<section id="section-faq">
  <h2>常见问题</h2>
  <div class="eb-faq-list">
    <details>
      <summary>股债利差和股权风险溢价 ERP 是一回事吗？</summary>
      <p>不完全等同。本页股债利差使用“盈利收益率减十年期国债收益率”的简化口径，可作为观察股权风险溢价的一种直观方式，但不是完整的资产定价模型。</p>
    </details>
    <details>
      <summary>为什么用 PE 的倒数表示盈利收益率？</summary>
      <p>PE（市盈率）表示价格相对盈利的倍数，其倒数 <code>100 / PE</code> 可以近似转化为盈利收益率（E/P），便于以百分比形式直接与债券收益率横向对比。</p>
    </details>
    <details>
      <summary>为什么要减十年期国债收益率？</summary>
      <p>中国十年期国债收益率常被用作长期无风险利率的锚。用股票盈利收益率减去它，可以直接观察承担股票波动风险所获得的相对收益补偿。</p>
    </details>
    <details>
      <summary>股债利差越高代表什么？</summary>
      <p>代表股票盈利收益率相对国债收益率的超额补偿更高。它反映的是历史相对位置变化，实际决策仍需结合盈利趋势、利率环境、情绪指标与指数结构一起观察。</p>
    </details>
    <details>
      <summary>股债利差越低代表什么？</summary>
      <p>代表股票相对债券的静态收益补偿变小。低利差并不等同于短期必然下跌，应结合其他估值和市场情绪指标综合评估。</p>
    </details>
    <details>
      <summary>沪深300和中证500股债利差为什么不同？</summary>
      <p>两个指数的成分股规模、行业权重分布、盈利稳定性与历史估值中枢不同（沪深300偏大盘蓝筹、中证500偏中盘成长与制造），因此在同一利率环境下算出的股债利差和历史百分位也会不同。</p>
    </details>
    <details>
      <summary>股债利差百分位和指数估值百分位有什么区别？</summary>
      <p>指数估值百分位只看 PE、PB 本身在历史中的高低位置；股债利差百分位把十年期国债收益率纳入比较，更强调股票相对无风险债券的配置性价比。</p>
    </details>
  </div>
  <p class="eb-note">数据来源：沪深300（000300）与中证500（000905）市值加权整体法 PE-TTM（含亏损成分股）来自蛋卷基金近10年估值历史；指数收盘价来自中证指数官网；中国 10 年期国债收益率来自东方财富中债国债收益率曲线（<code>RPTA_WEB_TREASURYYIELD</code>）。风险提示：本页面仅展示历史估值位置与数据参考，不构成投资建议。</p>
</section>

<div id="eb-popover" class="eb-popover" hidden>
  <div class="eb-popover-head"><strong data-tip-title></strong><small data-tip-date></small></div>
  <div data-tip-rows></div>
</div>
<script type="application/json" id="eb-data">{payload_json}</script>
{EQUITY_BOND_SCRIPT}
</main></body></html>'''

    tmp = output_html.with_name(f".{output_html.name}.tmp")
    tmp.write_text(doc, encoding="utf-8")
    tmp.replace(output_html)


def build_equity_bond(
    csi300_csv: Path = DEFAULT_CSI300_CSV,
    csi500_csv: Path = DEFAULT_CSI500_CSV,
    hs300_mcw_cache: Path = DEFAULT_HS300_MCW_CACHE,
    zz500_mcw_cache: Path = DEFAULT_ZZ500_MCW_CACHE,
    bond_cache: Path = DEFAULT_BOND_CACHE,
    hs300_csv: Path = DEFAULT_HS300_HISTORY_CSV,
    zz500_csv: Path = DEFAULT_ZZ500_HISTORY_CSV,
    output_html: Path = DEFAULT_OUTPUT_HTML,
    force: bool = False,
) -> dict:
    """执行完整采集、计算、CSV 导出与 HTML 渲染，返回双指数最新快照元数据。"""
    bond_df = load_or_update_bond_yield(bond_cache, force=force)
    csi300_close = (
        pd.read_csv(csi300_csv, parse_dates=["date"])
        if csi300_csv.exists() and not force
        else load_or_update_csi_valuation("000300", csi300_csv, force=force)
    )
    csi500_close = (
        pd.read_csv(csi500_csv, parse_dates=["date"])
        if csi500_csv.exists() and not force
        else load_or_update_csi_valuation("000905", csi500_csv, force=force)
    )
    hs300_pe = load_or_update_danjuan_pe_ttm("SH000300", hs300_mcw_cache, force=force)
    zz500_pe = load_or_update_danjuan_pe_ttm("SH000905", zz500_mcw_cache, force=force)

    hs300_frame, hs300_meta = calculate_equity_bond_history(hs300_pe, csi300_close, bond_df, "000300", "沪深300")
    zz500_frame, zz500_meta = calculate_equity_bond_history(zz500_pe, csi500_close, bond_df, "000905", "中证500")

    save_history_csv(hs300_frame, hs300_csv)
    save_history_csv(zz500_frame, zz500_csv)
    render_equity_bond_page(hs300_frame, hs300_meta, zz500_frame, zz500_meta, output_html)
    return {"hs300": hs300_meta, "zz500": zz500_meta}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成沪深300与中证500股债利差、股债比走势图页面")
    parser.add_argument("--force", action="store_true", help="强制全量重拉缓存")
    parser.add_argument("--output-html", default=str(DEFAULT_OUTPUT_HTML))
    args = parser.parse_args()
    snap = build_equity_bond(output_html=Path(args.output_html), force=args.force)
    h, z = snap["hs300"], snap["zz500"]
    print(
        f"equity-bond written: {args.output_html}; "
        f"hs300={h['total_days']}d (spread={h['spread_pct']:.2f}%, ratio={h['ratio']:.2f}x); "
        f"zz500={z['total_days']}d (spread={z['spread_pct']:.2f}%, ratio={z['ratio']:.2f}x)"
    )
