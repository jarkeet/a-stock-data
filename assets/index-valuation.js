"use strict";

const root = document.getElementById("valuation");
const route = location.pathname.match(/^\/index\/([a-z0-9]+)\/?$/);
const number = (value, digits = 2) => value == null ? "—" :
  value.toLocaleString("zh-CN", {minimumFractionDigits: digits, maximumFractionDigits: digits});
const percent = value => value == null ? "—" : number(value, 1) + "%";

function temperature(value) {
  if (value == null) return ["missing", "—"];
  if (value < 10) return ["coldest", "极冷"];
  if (value < 30) return ["cold", "偏冷"];
  if (value < 70) return ["neutral", "中性"];
  if (value < 90) return ["warm", "偏热"];
  return ["hottest", "极热"];
}

function weekOf(date) {
  const day = new Date(date + "T00:00:00Z");
  day.setUTCDate(day.getUTCDate() - (day.getUTCDay() + 6) % 7);
  return day.toISOString().slice(0,10);
}

function rank(value) {
  if (value == null) return "—";
  const [tone] = temperature(value);
  return `<div class="iv-rank iv-${tone}">
    <span class="iv-rank-track" aria-hidden="true"><span style="width:${value}%"></span></span>
    <span class="iv-rank-text">${percent(value)}</span>
  </div>`;
}

function badge(value) {
  const [tone, label] = temperature(value);
  return `<span class="iv-temperature iv-${tone}">${label}</span>`;
}

function methodology(csiSource, scope = "mainland") {
  const mainland = `<p class="iv-note"><a href="${csiSource}">中证指数官网</a>提供 A 股 PE-TTM 和日收盘点位。PE 分位以最近 2,430 个有效交易日为上限，
    至少 1,458 个观察值后计算。分位表示窗口内历史 PE 严格低于当前值的比例，走势图按周取样。</p>
    <p class="iv-note">A 股 PB 和 PB 分位优先使用<a href="https://danjuanfunds.com/article/337.html">雪球基金估值接口</a>。历史曲线从本地首次保存接口快照起累计。雪球未覆盖的指数使用估算 PB。</p>
    <p class="iv-note">A 股 PS 为月末估算值。用<a href="${csiSource}">中证公布的完整成分权重</a>和<a href="https://data.eastmoney.com/gzfx/">东方财富同日个股估值</a>计算加权调和平均数。只使用有效正值，权重覆盖不足 95% 留空。
    估算分位以最近十年的同口径月末样本计算，至少需要 72 期。</p>`;
  const hongKong = `<p class="iv-note"><a href="https://www.hsi.com.hk/eng/indexes/all-indexes/hstech">恒生科技指数（HSTECH）</a>的列表与顶部估值卡片来自<a href="https://danjuanfunds.com/djapi/index_eva/detail/HKHSTECH">雪球基金</a>。收盘点位来自<a href="https://quote.eastmoney.com/gb/zsHSTECH.html">东方财富</a>。</p>
    <p class="iv-note">PE、PB 历史曲线采用<a href="https://etf.whatsid.me/indices/valuation">投资工作台</a>公开的市值加权周频数据。分位是过去最多 10 年内严格低于当期估值的有效周样本比例，计算至少需要 52 个历史周样本。
    两家估值源的计算口径和更新日期可能不同。暂无已核实的公开 PS 数据。</p>`;
  return `<details class="iv-panel iv-method">
    <summary>估值口径与数据来源</summary>
    ${scope === "hongKong" ? "" : mainland}
    ${scope === "mainland" ? "" : hongKong}
    <p class="iv-note">PE 温度按分位划分。0% 至 10% 为极冷，10% 至 30% 为偏冷，30% 至 70% 为中性，70% 至 90% 为偏热，90% 及以上为极热。历史分位仅描述估值位置。</p>
  </details>`;
}

function renderList(indices) {
  const columns = [
    [null, "指数"], ["peDate", "数据日"], ["pe", "PE"], ["pePct", "PE 分位"],
    ["pb", "PB"], ["pbPct", "PB 分位"], ["ps", "PS"], ["psPct", "PS 分位"],
    ["temperature", "PE 温度"]
  ];
  const categories = ["全部", "宽基", "行业", "港股", "风格"];
  const count = new Map(categories.map(key => [
    key, indices.filter(index => key === "全部" || index.category === key).length
  ]));
  const categoryNames = {"行业":"行业主题", "风格":"风格策略"};
  root.innerHTML = `
    <div class="iv-heading">
      <div><h1>指数估值</h1><p>精选 A 股与港股指数 · 日终估值及历史分位</p></div>
      <a class="iv-utility" href="/a-share-pe-valuation-report.html">查看 A 股估值回测 ↗</a>
    </div>
    <div class="iv-panel iv-toolbar">
      <div class="iv-toolbar-head">
        <h2>快速查看</h2>
        <span class="iv-count" role="status"></span>
      </div>
        <div class="iv-filter" role="group" aria-label="指数分类">
          ${categories.filter(key => count.get(key)).map(category => `<button type="button" data-category="${category}"
            aria-pressed="${category === "全部"}">${categoryNames[category] || category}
            <span>${count.get(category)}</span></button>`).join("")}
        </div>
    </div>
    <p class="iv-list-help">点击指数名称查看详情，点击表头排序。各项估值沿用各自的数据日。</p>
    <div class="iv-panel iv-board">
      <div class="iv-table-wrap" tabindex="0" role="region" aria-label="指数估值表">
        <table class="iv-table">
          <thead><tr>${columns.map(([key, label]) => key
            ? `<th scope="col" data-column="${key}"><button type="button" data-sort="${key}">${label}<span aria-hidden="true">↕</span></button></th>`
            : `<th scope="col">${label}</th>`).join("")}</tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
    ${methodology("https://www.csindex.com.cn/", "all")}`;

  const tableBody = root.querySelector("tbody");
  let category = "全部";
  let sort = null;
  let ascending = true;

  function rows() {
    const visible = indices.filter(index => category === "全部" || index.category === category);
    if (sort) {
      const key = sort === "temperature" ? "pePct" : sort;
      visible.sort((left, right) => {
        const a = left[key], b = right[key];
        if (a == null || b == null) return Number(a == null) - Number(b == null);
        return (typeof a === "string" ? a.localeCompare(b) : a - b) * (ascending ? 1 : -1);
      });
    }
    tableBody.innerHTML = visible.map(index => `<tr>
      <td><a class="iv-index-link" href="/index/${index.slug}/">${index.name}</a>
        <div class="iv-index-meta"><span>${index.code}${index.category === "港股" ? " · HK" : ""}</span>
        <span class="iv-category-chip">${categoryNames[index.category] || index.category}</span></div></td>
      <td>${index.peDate || "—"}${index.pbDate && index.pbDate !== index.peDate ? `<small>PB ${index.pbDate}</small>` : ""}
        ${index.psDate && index.psDate !== index.peDate ? `<small>PS ${index.psDate}</small>` : ""}</td>
      <td class="iv-number">${number(index.pe)}</td><td>${rank(index.pePct)}</td>
      <td class="iv-number">${number(index.pb)}${index.pbSource === "estimate" ? "<small>估算</small>" : ""}</td><td>${rank(index.pbPct)}</td>
      <td class="iv-number">${number(index.ps)}${index.ps == null ? "" : "<small>月末估算</small>"}</td><td>${rank(index.psPct)}</td>
      <td>${badge(index.pePct)}</td>
    </tr>`).join("");
    root.querySelector(".iv-count").textContent = `${categoryNames[category] || category} · ${visible.length} 个指数`;
    root.querySelectorAll("[data-column]").forEach(th => {
      if (th.dataset.column === sort) th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
      th.querySelector("span").textContent = th.dataset.column === sort ? (ascending ? "↑" : "↓") : "↕";
    });
  }

  root.querySelectorAll("[data-category]").forEach(button => {
    button.addEventListener("click", () => {
      category = button.dataset.category;
      root.querySelectorAll("[data-category]").forEach(tab =>
        tab.setAttribute("aria-pressed", tab === button));
      rows();
    });
  });
  root.querySelectorAll("[data-sort]").forEach(button => {
    button.addEventListener("click", () => {
      ascending = sort === button.dataset.sort ? !ascending : true;
      sort = button.dataset.sort;
      rows();
    });
  });
  rows();
}

const metrics = [
  {key:"pe", label:"PE", name:"市盈率", color:"#2856ad"},
  {key:"pb", label:"PB", name:"市净率", color:"#008374"},
  {key:"ps", label:"PS", name:"市销率", color:"#a15d13"}
];

function renderDetail(index) {
  const hongKong = index.category === "港股";
  let historyNote = "";
  if (index.valuationHistorySource === "investmentWorkbench") {
    historyNote = `<p class="iv-note">历史估值来自<a href="https://etf.whatsid.me/indices/valuation">投资工作台</a>的市值加权周频数据，样本始于 ${index.valuationHistoryStart}。顶部卡片显示雪球基金快照。</p>`;
  } else if (index.valuationHistorySource === "danjuan") {
    historyNote = '<p class="iv-note">历史接口暂未返回可用数据，图中估值来自本地已保存的雪球基金快照。</p>';
  }
  document.title = `${index.name}估值 · 市场先生`;
  root.innerHTML = `
    <div class="iv-back"><a class="iv-utility" href="/indices/">← 指数估值</a></div>
    <div class="iv-heading">
      <div><h1>${index.name}估值</h1>
        <p>${index.code}${hongKong ? " · HSTECH" : ""} · ${index.category} · PE 数据日 ${index.peDate || "—"}
        · 收盘点位 ${number(index.close)}${hongKong && index.start ? ` · 雪球分位样本自 ${index.start}` : ""}</p></div>
    </div>
    <div class="iv-cards">
      ${metrics.map(({key, label, name, color}) => `<article class="iv-panel iv-card" style="--iv-series:${color}">
        <h2>${label} · ${name}</h2>
        <div class="iv-metric"><span><strong>${number(index[key])}</strong>${index[key] == null ? "" : " 倍"}</span>
          ${badge(index[key + "Pct"])}</div>
        <p class="iv-meta">${index[key] == null ? "暂无数据" :
          index[key + "Pct"] == null ? "历史分位样本不足" : hongKong ? "雪球基金历史分位" : "近 10 年分位"}</p>
        ${index[key + "Pct"] == null ? "" : rank(index[key + "Pct"])}
        <p class="iv-meta">数据日 ${index[key + "Date"] || "—"}
        ${index[key + "Coverage"] == null ? "" : ` · 月末估算 · 权重覆盖 ${percent(index[key + "Coverage"])}`}</p>
      </article>`).join("")}
    </div>
    <section class="iv-panel iv-chart">
      <div class="iv-chart-head">
        <h2 id="chart-heading">估值分位走势</h2>
        <div class="iv-mode" role="group" aria-label="走势指标">
          <button type="button" data-mode="pct" aria-pressed="true">估值分位</button>
          <button type="button" data-mode="value" aria-pressed="false">PE-TTM</button>
        </div>
        <div class="iv-mode" role="group" aria-label="时间范围">
          ${[1, 3, 5, 10].map(years => `<button type="button" data-years="${years}"
            aria-pressed="${years === 10}">${years} 年</button>`).join("")}
        </div>
      </div>
      <div class="iv-chart-legend"></div>
      <div class="iv-plot">
        <canvas tabindex="0" role="img" aria-label="估值时间序列，按左右方向键查看数据点"></canvas>
        <div class="iv-tooltip" aria-live="polite" hidden></div>
      </div>
      <p class="iv-note" id="chart-sample"></p>
      ${historyNote}
    </section>
    ${methodology(index.csiSource, hongKong ? "hongKong" : "mainland")}`;
  chart(index);
}

function chart(index) {
  const canvas = root.querySelector("canvas");
  const context = canvas.getContext("2d");
  const tooltip = root.querySelector(".iv-tooltip");
  const legend = root.querySelector(".iv-chart-legend");
  const sample = root.querySelector("#chart-sample");
  const source = [index.peHistory, index.pbHistory, index.psHistory];
  const colors = index.slug === "hstech" ? {pe:"#d32f2f", pb:"#2563eb", ps:"#b78200"} : {};
  const pctLines = metrics.map((metric, i) => ({
    ...metric, color: colors[metric.key] ?? metric.color,
    label: metric.label + " 分位", points: source[i], format: percent
  }));
  const price = {key:"close", label:"收盘点位（右轴）", color:"#6b778d", points:index.closeHistory, format:number};
  const pe = {key:"peValue", label:"PE-TTM", color:pctLines[0].color, points:index.peValues, format:v => number(v) + " 倍"};
  const allDates = [...source, index.peValues, index.closeHistory].flatMap(points => points.map(([d]) => d));
  const latest = allDates.sort().at(-1);
  let mode = "pct";
  let years = 10;
  let selected = null;
  let view = null;
  const hidden = new Set(index.category === "港股" ? [] : ["close"]);

  function clearSelection() {
    selected = null;
    tooltip.hidden = true;
  }

  function buildLegend() {
    const options = [...(mode === "pct" ? pctLines : [pe]), price];
    legend.innerHTML = options.map(line =>
      `<label style="--iv-series:${line.color}"><input type="checkbox" data-line="${line.key}"
      ${line.points.length && !hidden.has(line.key) ? "checked" : ""} ${line.points.length ? "" : "disabled"}>${line.label}</label>`).join("");
    legend.querySelectorAll("input").forEach(input => {
      input.addEventListener("change", () => {
        if (input.checked) hidden.delete(input.dataset.line);
        else hidden.add(input.dataset.line);
        clearSelection();
        draw();
      });
    });
  }

  function draw() {
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (width === 0 || height === 0) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.font = "11px system-ui";
    context.fillStyle = "#52647c";
    const cutoffDate = latest ? new Date(latest + "T00:00:00Z") : new Date();
    cutoffDate.setUTCFullYear(cutoffDate.getUTCFullYear() - years);
    const cutoff = cutoffDate.toISOString().slice(0,10);
    const left = 44, top = 13, right = width - (hidden.has("close") ? 16 : 53), bottom = height - 30;
    const lines = [...(mode === "pct" ? pctLines : [pe]), price]
      .filter(line => !hidden.has(line.key))
      .map(line => {
        const points = line.points.filter(([date]) => date >= cutoff);
        const available = points.filter(([,value]) => value != null);
        return {...line, points, byDate:new Map(available),
          byWeek:new Map(available.map(point => [weekOf(point[0]), point]))};
      });
    const dates = [...new Set(lines.flatMap(line => [...line.byDate.keys()]))].sort();
    view = null;
    if (!dates.length) {
      context.fillText("所选指标在此区间暂无数据", left, 37);
      sample.textContent = "";
      return;
    }
    const first = dates[0], last = dates.at(-1);
    const start = Date.parse(first), span = Date.parse(last) - start;
    const x = date => span ? left + (Date.parse(date) - start) / span * (right - left) : (left + right) / 2;
    const peValues = [...(lines.find(line => line.key === "peValue")?.byDate.values() || [])];
    const low = peValues.length ? Math.floor(Math.min(...peValues)) : 0;
    const high = peValues.length ? Math.max(low + 1, Math.ceil(Math.max(...peValues))) : 100;
    const closes = lines.find(line => line.key === "close")?.points.map(([,v]) => v) || [];
    const closeLow = closes.length ? Math.floor(Math.min(...closes)) : 0;
    const closeHigh = closes.length ? Math.max(closeLow + 1, Math.ceil(Math.max(...closes))) : 1;
    const y = (value, key) => {
      const min = key === "close" ? closeLow : low;
      const max = key === "close" ? closeHigh : high;
      return bottom - (value - min) / (max - min) * (bottom - top);
    };

    for (let tick = 0; tick <= 5; tick++) {
      const share = tick / 5;
      const row = bottom - share * (bottom - top);
      context.beginPath();
      context.moveTo(left, row);
      context.lineTo(right, row);
      context.strokeStyle = mode === "pct" && (tick === 1 || tick === 4) ? "#b4c8e4" : "#e6edf6";
      context.stroke();
      context.textAlign = "right";
      context.fillText(number(low + share * (high - low), 0) + (mode === "pct" ? "%" : ""), left - 6, row + 4);
      if (closes.length) {
        context.textAlign = "left";
        context.fillText(number(closeLow + share * (closeHigh - closeLow), 0), right + 6, row + 4);
      }
    }
    context.textAlign = "center";
    const tickCount = Math.max(2, Math.min(7, Math.floor(width / 115)));
    const ticks = new Set(Array.from({length:tickCount}, (_, i) =>
      dates[Math.round(i * (dates.length - 1) / (tickCount - 1))]));
    ticks.forEach(date => context.fillText(date.slice(0,7), x(date), bottom + 18));
    lines.forEach(line => {
      context.beginPath();
      context.strokeStyle = line.color;
      context.lineWidth = line.key === "close" ? 1 : 1.7;
      let connected = false;
      line.points.forEach(([date, value]) => {
        if (value == null) {
          connected = false;
          return;
        }
        const px = x(date), py = y(value, line.key);
        if (connected) context.lineTo(px, py);
        else context.moveTo(px, py);
        connected = true;
      });
      context.stroke();
      if (line.byDate.size === 1) {
        const [date, value] = line.byDate.entries().next().value;
        context.beginPath();
        context.fillStyle = line.color;
        context.arc(x(date), y(value, line.key), 3.4, 0, 2 * Math.PI);
        context.fill();
      }
    });

    view = {dates, x};
    if (selected && dates.includes(selected)) {
      const px = x(selected);
      context.beginPath();
      context.moveTo(px, top);
      context.lineTo(px, bottom);
      context.lineWidth = 1;
      context.strokeStyle = "#8193ad";
      context.stroke();
      const week = weekOf(selected);
      const readouts = lines.flatMap(line => {
        if (line.byDate.has(selected)) return [{line, date:selected, value:line.byDate.get(selected)}];
        const point = line.byWeek.get(week);
        return point ? [{line, date:point[0], value:point[1]}] : [];
      });
      readouts.forEach(({line, date, value}) => {
        context.beginPath();
        context.fillStyle = line.color;
        context.arc(x(date), y(value, line.key), 3.4, 0, 2 * Math.PI);
        context.fill();
      });
      tooltip.innerHTML = `<time>${selected}</time>${readouts.map(({line, date, value}) =>
        `<div><span>${line.label}${date === selected ? "" : " " + date.slice(5)}</span>
        <strong>${line.format(value)}</strong></div>`).join("")}`;
      tooltip.hidden = false;
      const tipX = px + 12 + tooltip.offsetWidth < width ? px + 12 : px - tooltip.offsetWidth - 12;
      tooltip.style.left = Math.max(6, tipX) + "px";
      tooltip.style.top = "14px";
    }
    const coverage = lines.filter(line => line.byDate.size).map(line => {
      const days = [...line.byDate.keys()];
      const label = line.key === "close" ? "收盘点位" : line.label;
      return days.length === 1 ? `${label} ${days[0]}（1 个观测）` : `${label} ${days[0]} 至 ${days.at(-1)}`;
    });
    sample.textContent = `${coverage.join(" · ")}。移动鼠标、点击或按左右方向键查看读数。`;
  }

  function pointer(event) {
    if (!view) return;
    const px = event.clientX - canvas.getBoundingClientRect().left;
    const date = view.dates.reduce((best, d) => Math.abs(view.x(d) - px) < Math.abs(view.x(best) - px) ? d : best);
    if (date === selected) return;
    selected = date;
    draw();
  }

  canvas.addEventListener("pointermove", pointer);
  canvas.addEventListener("click", pointer);
  canvas.addEventListener("pointerleave", () => { clearSelection(); draw(); });
  canvas.addEventListener("keydown", event => {
    if (!view || !["ArrowLeft", "ArrowRight", "Escape"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Escape") clearSelection();
    else {
      const current = selected ? view.dates.indexOf(selected) : view.dates.length;
      const next = Math.max(0, Math.min(view.dates.length - 1, current + (event.key === "ArrowLeft" ? -1 : 1)));
      selected = view.dates[next];
    }
    draw();
  });
  canvas.addEventListener("blur", () => { clearSelection(); draw(); });
  root.querySelectorAll("[data-mode], [data-years]").forEach(button => {
    button.addEventListener("click", () => {
      if (button.dataset.mode) {
        mode = button.dataset.mode;
        root.querySelector("#chart-heading").textContent = mode === "pct" ? "估值分位走势" : "市盈率走势";
        buildLegend();
      } else years = Number(button.dataset.years);
      button.parentElement.querySelectorAll("button").forEach(tab =>
        tab.setAttribute("aria-pressed", tab === button));
      clearSelection();
      draw();
    });
  });
  buildLegend();
  draw();
  new ResizeObserver(draw).observe(canvas);
}

async function load() {
  try {
    const endpoint = route ? `/api/index-valuations/${route[1]}` : "/api/index-valuations";
    const response = await fetch(endpoint);
    if (!response.ok) throw new Error("Valuation data unavailable");
    const {indices} = await response.json();
    if (route) renderDetail(indices[0]);
    else renderList(indices);
  } catch (error) {
    console.error(error);
    root.innerHTML = '<p class="iv-message" role="status">指数估值暂时无法读取。<a href="/indices/">返回估值列表</a></p>';
  } finally {
    root.removeAttribute("aria-busy");
  }
}

load();
