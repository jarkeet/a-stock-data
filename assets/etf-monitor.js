"use strict";

const el = id => document.getElementById(id);
const daySelect = el("etf-day");
const replaySelect = el("etf-replay");
const filter = el("etf-filter");
const search = el("etf-search");
const sort = el("etf-sort");
const canvas = el("etf-chart");
const roleNames = {main:"主预警", history:"历史持仓观察", control:"市场对照", theme:"央企主题"};
const format = new Intl.NumberFormat("zh-CN", {maximumFractionDigits:2});
const number = value => Number.isFinite(value) ? format.format(value) : "—";
const amount = value => Number.isFinite(value)
  ? value >= 1e8 ? number(value / 1e8) + " 亿" : number(value / 1e4) + " 万"
  : "—";
const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => (
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]
));
const hhmm = minute => String(Math.floor(minute / 60)).padStart(2,"0") + ":" + String(minute % 60).padStart(2,"0");
const minutes = value => Number(value.slice(0, 2)) * 60 + Number(value.slice(3, 5));
const plotMinute = label => {
  const minute = minutes(label);
  return minute - 570 - (minute > 690 ? 90 : 0);
};

let dashboard;
let replay;
let focusTime = "";
let dashboardRequest = 0;
let replayRequest = 0;

async function getJSON(url, params = {}) {
  const query = new URLSearchParams(params).toString();
  const response = await fetch(url + (query ? "?" + query : ""), {cache:"no-store"});
  if (!response.ok) throw new Error("行情读取失败");
  return response.json();
}

function status(data) {
  const target = el("etf-status");
  const collector = data.collector;
  const recent = collector && Date.now() - Date.parse(collector.stamp) < 95000;
  const phase = recent ? collector.phase : "waiting";
  const label = daySelect.value ? "历史回放" : {
    live:"盘中采集", auction:"收盘集合竞价", closed:data.window === "15:00" ? "已收盘" : "非连续竞价时段",
    waiting:"等待有效行情"
  }[phase] || "等待有效行情";
  const parts = [
    "<strong>" + label + "</strong>",
    data.day && "<span>行情日 " + esc(data.day) + "</span>",
    data.window && "<span>分钟数据至 " + esc(data.window) + "</span>",
    recent && !daySelect.value && phase === "live"
      && "<span>新鲜行情 " + number(collector.count) + "/" + data.funds.length + "</span>",
  ];
  target.dataset.state = daySelect.value ? "history" : phase;
  target.innerHTML = parts.filter(Boolean).join("");
}

function signed(value, formatValue) {
  return Number.isFinite(value) ? (value > 0 ? "+" : value < 0 ? "−" : "") + formatValue(Math.abs(value)) : "—";
}

function signClass(value) {
  return value > 0 ? "etf-up" : value < 0 ? "etf-down" : "";
}

function turnoverSpark(points) {
  if (!points?.length) return "—";
  const max = Math.max(1, ...points.map(([,value]) => value ?? 0));
  const bars = points.map(([day,value]) =>
    `<i title="${esc(day)} · ${value == null ? "缺少可比区间" : amount(value) + "元"}" style="--height:${value == null ? 4 : Math.max(4, value / max * 100)}%" ${value == null ? "data-gap" : ""}></i>`).join("");
  return `<div class="etf-spark" role="img" aria-label="最近历史交易日固定时段成交额">${bars}</div>`;
}

function renderTable() {
  if (!dashboard) return;
  const query = search.value.trim().toLowerCase();
  const orders = {
    multiple: f => f.dayTurnover?.multiple,
    buy: f => f.direction?.net_buy_estimate,
    sell: f => f.direction?.net_buy_estimate == null ? null : -f.direction.net_buy_estimate,
    turnover: f => f.quote?.amount ?? f.direction?.amount,
  };
  const key = orders[sort.value];
  const funds = dashboard.funds.filter(fund => (!filter.value || filter.value === fund.role)
    && (!query || `${fund.code} ${fund.index} ${fund.provider}`.toLowerCase().includes(query)))
    .sort((a,b) => (key(b) ?? -Infinity) - (key(a) ?? -Infinity));
  el("etf-funds").innerHTML = funds.length ? funds.map(fund => {
    const metric = fund.metrics;
    const day = fund.dayTurnover;
    const price = fund.quote?.price ?? fund.bar?.price;
    const change = fund.quote?.change;
    const last = fund.quote?.stamp?.slice(11,19) ?? (fund.bar ? hhmm(fund.bar.minute) : "");
    const net = fund.direction?.net_buy_estimate;
    const share = fund.share;
    const evidence = fund.evidence;
    const turnover = fund.quote?.amount ?? fund.direction?.amount;
    const size = share && Number.isFinite(price) ? share.shares * price : null;
    return `<tr data-selected="${fund.code === replaySelect.value}">
      <td class="etf-identity"><button type="button" data-code="${fund.code}">${esc(fund.index)} · ${esc(fund.provider)}</button><small>${fund.code} · ${roleNames[fund.role]}</small></td>
      <td data-label="价格 / 涨跌">${Number.isFinite(price) ? price.toFixed(3) : "—"} <span class="${signClass(change)}">${signed(change,v=>number(v)+"%")}</span>${last ? `<small>${esc(last)}</small>` : ""}</td>
      <td data-label="当日成交额">${amount(turnover)}</td>
      <td data-label="同期倍数"><strong class="${day?.multiple >= 1.5 ? "etf-up" : ""}">${day?.multiple == null ? "—" : number(day.multiple) + "×"}</strong>${day?.comparableThrough ? `<small>${esc(day.comparableThrough)} · ${day.baselineDays} 日基线</small>` : ""}</td>
      <td data-label="主动净买卖估算" class="${signClass(net)}">${signed(net,amount)}</td>
      <td data-label="近 5 分钟">${amount(metric?.amount)}${metric?.percentile == null ? "" : ` <small>P${number(metric.percentile)} · ${esc(metric.clock)}</small>`}</td>
      <td data-label="官方份额 / 变化">${share ? `${number(share.shares/1e8)} 亿份 <span class="${share.unusual ? "" : signClass(share.change)}">${share.unusual ? "待核对" : signed(share.change,v=>number(v/1e8)+" 亿")}</span><small>${esc(share.day)} · ${share.source==="SSE"?"上交所":"深交所"}${share.previousDay ? " · 较 " + esc(share.previousDay.slice(5)) : " · 相邻披露缺口"}</small>` : "—"}</td>
      <td data-label="参考规模">${amount(size)}</td>
      <td data-label="近 30 日固定时段成交">${turnoverSpark(fund.recentTurnover)}</td>
      <td data-label="公开持仓">${evidence ? `<a href="${esc(evidence.url)}" target="_blank" rel="noopener">${esc(evidence.holder)}</a><small>${evidence.shares ? number(evidence.shares/1e8)+" 亿份 · " : ""}${esc(evidence.day)}</small>` : "—"}</td>
    </tr>`;
  }).join("") : '<tr><td colspan="10" class="etf-no-matches">没有符合条件的 ETF</td></tr>';
}

function renderEvents() {
  const events = dashboard.events;
  const ready = dashboard.funds.some(f => f.role === "main" && f.metrics?.baselineDays >= 30);
  el("etf-events").innerHTML = events.length ? events.map(event => {
    const fund = dashboard.funds.find(item => item.group === event.group);
    const codes = event.codes.map(esc).join("、");
    return `<button type="button" class="etf-event" data-code="${esc(event.codes[0])}" data-at="${esc(event.peak)}">
      <strong>${esc(event.first === event.last ? event.first : event.first + "–" + event.last)} · ${esc(fund?.index || event.group)}</strong><span class="etf-tag" data-alert="${esc(event.kind)}">${event.kind === "group" ? "组级放量" : "单品放量"}</span>
      <p>${codes} · ${amount(event.amount)}元 · P${number(event.percentile)}</p>
      <p>峰值 ${esc(event.peak)} · 买方待核验</p>
    </button>`;
  }).join("") : `<p class="etf-empty">${ready ? "所选交易日暂无放量事件。" : "正在积累可用基线，样本就绪后显示放量事件。"}</p>`;
}

const verdicts = {
  strong_buy: ["明显买入", "buy"],
  lean_buy: ["偏买入", "buy"],
  strong_sell: ["明显卖出", "sell"],
  lean_sell: ["偏卖出", "sell"],
  neutral: ["看不清", "neutral"],
  unknown: ["看不清", "neutral"],
};

function renderAssessment(data) {
  const result = data.assessment ?? {};
  const historical = Boolean(data.day && data.day !== data.today);
  el("etf-verdict-period").textContent = historical
    ? (daySelect.value ? "当日结论" : "最近交易日结论") + " · " + data.day
    : result.quoteBasis === "close" ? "今日结论 · 收盘" : "今日结论";
  const [title, tone] = verdicts[result.rating] ?? verdicts.unknown;
  el("etf-verdict").dataset.tone = tone;
  el("etf-verdict-heading").textContent = title;
  const scored = Number.isFinite(result.score);
  el("etf-verdict-score").textContent = scored ? number(result.score) : "—";
  const meter = el("etf-verdict-meter");
  meter.hidden = !scored;
  if (scored) {
    meter.style.setProperty("--score", result.score + "%");
    meter.setAttribute("aria-valuenow", result.score);
    meter.setAttribute("aria-valuetext", `${result.score} 分，${title}`);
  }

  let description = "主池的净额占比或同向指数数量偏低，盘面方向暂不清晰。";
  if (tone !== "neutral") {
    const side = tone === "buy" ? "买入" : "卖出";
    let detail = `主池 ${result.buyGroups} 个指数偏买入、${result.sellGroups} 个偏卖出，主动净${side}估算占成交额 ${number(Math.abs(result.netRatio))}%。`;
    if (Number.isFinite(result.multiple)) detail += `成交额为历史同期的 ${number(result.multiple)} 倍。`;
    description = detail;
  } else if (!scored) {
    description = {
      stale:"行情暂未更新，等同步数据恢复后再评级。",
      closing_pending:historical ? "该交易日缺少经核对的收盘方向数据，可查看成交回放。" : "正在用 15:00 的收盘价量核对行情，核对完成后会更新结论。",
      limited_coverage:"能估算买卖方向的 ETF 还不够多，等主池行情补齐后再评级。",
      unavailable:"页面暂未连上行情服务，稍后会自动重试。",
    }[result.reason] ?? (historical ? "该交易日缺少买卖方向快照，可继续查看成交回放。" : "正在等主池的有效买卖方向数据。");
  }
  el("etf-verdict-text").textContent = description;
  const net = result.netBuyEstimate;
  const cells = [
    ["主池合计成交额", Number.isFinite(result.turnover) ? amount(result.turnover) + "元" : "—"],
    ["成交额放大倍数（同期）", Number.isFinite(result.multiple) ? number(result.multiple) + "×" : "—"],
    [net < 0 ? "主动净卖额估算" : "主动净买额估算",
      signed(net,value=>amount(value)+"元"), true],
    ["对照池放量倍数", Number.isFinite(result.controlMultiple) ? number(result.controlMultiple) + "×" : "—"],
  ];
  el("etf-verdict-metrics").innerHTML = cells.map(([name, value, signed]) =>
    `<div><dt>${name}</dt><dd${signed ? " data-signed" : ""}>${value}</dd></div>`).join("");
  const info = [
    result.quoteBasis === "close" ? "已核对 15:00 收盘价量"
      : result.asOf && result.reason !== "closing_pending" && "截至 " + result.asOf.slice(11, 16),
    result.universe && `主池方向覆盖 ${result.coverage}/${result.universe} 只`,
    Number.isFinite(result.controlCoverage) && `对照可比 ${result.controlCoverage}/5 只`,
    Number.isFinite(result.baseline) && `基线 ${amount(result.baseline)}元（${result.baselineDays} 日，截至 ${result.comparableThrough}）`,
    "外盘与内盘推算 · 资金身份以公开披露为准",
  ];
  el("etf-verdict-meta").textContent = info.filter(Boolean).join(" · ");
}

function render(data) {
  dashboard = data;
  if (replaySelect.options.length === 0) {
    replaySelect.innerHTML = data.funds.map(f => `<option value="${f.code}">${f.code} ${esc(f.index)} · ${esc(f.provider)}</option>`).join("");
  }
  const days = ["", ...data.days];
  if (daySelect.value && !days.includes(daySelect.value)) days.push(daySelect.value);
  if ([...daySelect.options].map(option => option.value).join() !== days.join()) {
    const current = daySelect.value;
    daySelect.innerHTML = '<option value="">跟随最新</option>' + data.days.map(day => `<option>${esc(day)}</option>`).join("");
    daySelect.value = days.includes(current) ? current : "";
  }
  status(data);
  renderAssessment(data);
  const ready = data.funds.filter(f => f.role === "main" && f.metrics?.baselineDays >= 30).length;
  el("etf-summary").innerHTML = [
    ["监测 ETF", data.funds.length, "页面持续跟踪的 ETF 总数。"],
    ["主池基线就绪", ready + " / " + data.funds.filter(f => f.role === "main").length,
      "前面的数字是已攒够 30 天同期成交记录的只数，后面是主池总数。"],
    ["组级放量事件", data.events.filter(event => event.kind === "group").length,
      "当天同一指数下，多只 ETF 同时成交放大的次数。"],
  ].map(([label, value, description]) => `
    <article><p>${label}</p><strong>${value}</strong><p class="etf-summary-description">${description}</p></article>`
  ).join("");
  renderTable();
  renderEvents();
}

function drawChart() {
  const points = replay?.points ?? [];
  const span = replay?.span ?? 1;
  el("etf-chart-empty").hidden = points.length > 0;
  el("etf-chart-meta").textContent = points.length
    ? `${points[0][0]} 至 ${points.at(-1)[0]} · 柱为成交额，蓝线为价格${replay.closeAuction ? " · 橙柱为 14:57 至 15:00 收盘段成交额" : ""}`
    : "";
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  if (!points.length) return;
  const left = 56, right = width - 50, top = 18, bottom = height - 28;
  const x = label => left + plotMinute(label) / 240 * (right - left);
  const max = Math.max(1, ...points.map(p => p[1]));
  const prices = points.map(p => p[2]);
  const minPrice = Math.min(...prices);
  const maxPrice = Math.max(...prices);
  const yPrice = value => top + (bottom - top) * (maxPrice === minPrice ? .5 : (maxPrice - value) / (maxPrice - minPrice));
  ctx.font = '11px "Microsoft YaHei", sans-serif';
  for (let tick = 0; tick <= 4; tick++) {
    const y = bottom - tick * (bottom - top) / 4;
    ctx.strokeStyle = "#e4ebf2";
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(right, y);
    ctx.stroke();
    ctx.fillStyle = "#516178";
    ctx.textAlign = "right";
    ctx.fillText(amount(max * tick / 4), left - 6, y + 4);
  }
  ctx.textAlign = "left";
  ctx.fillText(maxPrice.toFixed(3), right + 5, top + 4);
  ctx.fillText(minPrice.toFixed(3), right + 5, bottom + 4);
  ctx.textAlign = "center";
  for (const label of ["09:30", "10:30", "11:30", "14:00", "15:00"]) {
    ctx.fillText(label, x(label), height - 9);
  }
  const barWidth = Math.max(1, (right - left) * span / 240);
  for (const [label, value] of points) {
    const h = (bottom - top) * value / max;
    ctx.fillStyle = label === focusTime || (label === "15:00" && replay.closeAuction) ? "#a44917" : "#86b1d0";
    ctx.fillRect(x(label) - barWidth / 2, bottom - h, barWidth, h);
  }
  ctx.strokeStyle = "#24578c";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  let previous = 0;
  for (const [label, , price] of points) {
    const minute = minutes(label);
    if (minute === previous + span) ctx.lineTo(x(label), yPrice(price));
    else ctx.moveTo(x(label), yPrice(price));
    previous = minute;
  }
  ctx.stroke();
  if (replay.closeAuction) {
    ctx.fillStyle = "#24578c";
    ctx.beginPath();
    ctx.arc(x("15:00"), yPrice(points.at(-1)[2]), 3, 0, 2 * Math.PI);
    ctx.fill();
  }
  if (focusTime) {
    ctx.strokeStyle = "#a44917";
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(x(focusTime), top);
    ctx.lineTo(x(focusTime), bottom);
    ctx.stroke();
    ctx.setLineDash([]);
  }
}

async function loadReplay() {
  const request = ++replayRequest;
  const key = replaySelect.value;
  const day = dashboard?.day;
  if (!day || !key) {
    replay = null;
    drawChart();
    return;
  }
  el("etf-chart-note").textContent = day + (focusTime ? " · 事件峰值 " + focusTime : " · 成交额单位为元");
  try {
    const result = await getJSON("/api/etf-monitor/replay", {day, code:key});
    if (request !== replayRequest) return;
    replay = result;
    const heading = el("replay-heading");
    heading.textContent = result.span === 5 ? "5 分钟成交回放" : "分钟成交回放";
    el("etf-chart-empty").textContent = "该交易日暂无分钟数据";
    drawChart();
  } catch {
    if (request !== replayRequest) return;
    replay = null;
    el("etf-chart-empty").textContent = "回放读取失败";
    drawChart();
  }
}

async function refresh() {
  const request = ++dashboardRequest;
  const day = daySelect.value;
  try {
    const result = await getJSON("/api/etf-monitor", day ? {day} : {});
    if (request !== dashboardRequest) return;
    render(result);
    await loadReplay();
  } catch {
    if (request !== dashboardRequest) return;
    el("etf-status").textContent = "ETF 行情读取失败 · 稍后重试";
    el("etf-status").dataset.state = "waiting";
    renderAssessment({day:dashboard?.day, today:dashboard?.today, assessment:{rating:"unknown", reason:"unavailable"}});
  }
}

function selectFund(event) {
  const target = event.target.closest("button[data-code]");
  if (!target) return;
  replaySelect.value = target.dataset.code;
  focusTime = target.dataset.at || "";
  renderTable();
  loadReplay();
  if (target.closest("#etf-funds")) canvas.scrollIntoView({block:"nearest"});
}

el("etf-funds").addEventListener("click", selectFund);
el("etf-events").addEventListener("click", selectFund);
filter.addEventListener("change", renderTable);
sort.addEventListener("change", renderTable);
search.addEventListener("input", renderTable);
daySelect.addEventListener("change", () => {
  focusTime = "";
  replay = null;
  drawChart();
  refresh();
});
replaySelect.addEventListener("change", () => {
  focusTime = "";
  renderTable();
  loadReplay();
});
new ResizeObserver(drawChart).observe(canvas);
setInterval(() => {
  if (!document.hidden && !daySelect.value) refresh();
}, 15000);
refresh();
