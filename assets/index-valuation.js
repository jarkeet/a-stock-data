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

function miniBadge(value) {
  if (value == null) return "";
  const [tone, label] = temperature(value);
  return `<span class="iv-mini-badge iv-${tone}">${label}</span>`;
}

function methodology(csiSource, scope = "mainland") {
  const mainland = `<p class="iv-note"><a href="${csiSource}">中证指数官网</a>提供其覆盖指数的 PE-TTM 和日收盘点位。中证 PE 分位以最近 2,430 个有效交易日为上限，
    至少 1,458 个观察值后计算。分位表示窗口内历史 PE 严格低于当前值的比例，走势图按周取样。</p>
    <p class="iv-note">部分指标采用<a href="https://xushilu.com/valuation">平安估值数据</a>，分位按过去最多 10 年的可用周样本计算，至少需要 52 个历史周样本。各指标的来源和数据日见详情。</p>
    <p class="iv-note">PE-TTM、PB、分位和 ROE 优先采用<a href="https://danjuanfunds.com/article/337.html">雪球基金估值快照</a>。雪球曲线从本地首次保存快照起累计。其他来源及日期在估值列和详情中标注。</p>
    <p class="iv-note">最新价和当日涨跌优先采用腾讯财经或东方财富行情，单元格标注各自的时间和来源。东方财富主力净流入为大单与超大单的日内累计净额。成交额按行情原币展示，来源未返回的字段留空。</p>
    <p class="iv-note">迷你走势和区间涨跌幅使用指数收盘点位，黄金使用 ETF 场内收盘价。今年以来以上年最后一个交易日为基准。近 1 周、1 月、3 月分别回看 7、30、90 个自然日，近 1 年回看上年同日，均取基准日当日或之前最近交易日的收盘。</p>
    <p class="iv-note">月末 PB 估算使用<a href="${csiSource}">中证公布的成分权重</a>和<a href="https://data.eastmoney.com/gzfx/">东方财富同日个股 PB</a>计算加权调和平均数。使用有效正值，权重覆盖不足 95% 时留空。
    估算分位以最近十年的同口径月末样本计算，至少需要 72 期。</p>`;
  const hongKong = `<p class="iv-note">港股及跨境主题按标的指数展示估值。ETF 代码用于标识跟踪产品。</p>
    <p class="iv-note">有雪球快照时优先显示其估值和分位。其他周频分位表示过去最多 10 年内严格低于当期估值的有效周样本比例，至少需要 52 个历史周样本。
    各指标的数据来源、日期和实际历史覆盖见指数详情。</p>`;
  return `<details class="iv-panel iv-method">
    <summary>估值口径与数据来源</summary>
    ${scope === "hongKong" ? "" : mainland}
    ${scope === "mainland" ? "" : hongKong}
    <p class="iv-note">PE 温度按分位划分。0% 至 10% 为极冷，10% 至 30% 为偏冷，30% 至 70% 为中性，70% 至 90% 为偏热，90% 及以上为极热。历史分位仅描述估值位置。</p>
  </details>`;
}

const valuationSources = {danjuan:"雪球基金", csi:"中证指数", balanced:"平安数据", estimate:"月末估算"};

const marketSources = {
  tencent:"腾讯财经", eastmoney:"东方财富", "csi-close":"中证指数收盘", "cni-close":"国证指数收盘"
};

function direction(value) {
  return value > 0 ? "up" : value < 0 ? "down" : "flat";
}

function signedPercent(value, daily = false) {
  if (value == null) return '<span class="iv-empty">—</span>';
  return `<span class="iv-return iv-return-${direction(value)}${daily ? " iv-daily-change" : ""}">${value > 0 ? "+" : ""}${number(value, 2)}%</span>`;
}

function marketAmount(value, currency) {
  if (value == null) return "—";
  const large = Math.abs(value) >= 1e8;
  return number(value / (large ? 1e8 : 1e4)) + (large ? "亿" : "万") + (currency === "HKD" ? "港元" : "");
}

function marketCells(index) {
  const date = index.quoteDate || index.priceDate;
  const time = index.quoteTime?.slice(0, 5) || "收盘";
  const name = marketSources[index.quoteSource || index.closeSource] || "指数收盘";
  const quoteTitle = date ? ` title="${name} · ${date} ${time}"` : "";
  const flowTitle = index.flowDate ? ` title="东方财富 · ${index.flowDate} · 大单与超大单净额"` : "";
  return `
    <td class="iv-market-price"${quoteTitle}>
      <span class="iv-return iv-return-${direction(index.changePct)}">${number(index.latestPrice, index.priceOnly ? 3 : 2)}</span>
      ${date ? `<small><time datetime="${date}">${date.slice(5)} ${time}</time></small>` : ""}
    </td>
    <td${quoteTitle}>${signedPercent(index.changePct, true)}</td>
    <td${flowTitle}><span class="iv-return iv-return-${direction(index.mainFlow)}">${marketAmount(index.mainFlow)}</span>
      ${index.flowDate && index.flowDate !== date ? `<small>${index.flowDate.slice(5)}</small>` : ""}</td>
    <td title="${marketSources[index.amountSource] || name}${date ? " · " + date : ""}">${marketAmount(index.amount, index.currency)}</td>
    <td title="${marketSources[index.turnoverRateSource] || name}${date ? " · " + date : ""}">${index.turnoverRate == null ? "—" : number(index.turnoverRate) + "%"}</td>`;
}

function drawSparklines(tableBody, indices) {
  const trends = new Map(indices.map(index => [index.slug, index.trend]));
  tableBody.querySelectorAll("canvas[data-trend]").forEach(canvas => {
    const values = trends.get(canvas.dataset.trend);
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (!width || !height) return;
    const scale = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const context = canvas.getContext("2d");
    if (!context) return;
    context.setTransform(scale, 0, 0, scale, 0, 0);
    const change = values.at(-1) - values[0];
    const color = change > 0 ? "#eb5252" : change < 0 ? "#48b14a" : "#7d7d7d";
    const min = Math.min(...values), span = Math.max(...values) - min;
    const points = values.map((value, i) => [
      2 + (width - 4) * i / (values.length - 1),
      span ? height - 4 - (height - 8) * (value - min) / span : height / 2
    ]);
    const trace = () => {
      context.beginPath();
      context.moveTo(...points[0]);
      points.slice(1).forEach(point => context.lineTo(...point));
    };
    trace();
    context.lineTo(width - 2, height);
    context.lineTo(2, height);
    context.closePath();
    const shade = context.createLinearGradient(0, 0, 0, height);
    shade.addColorStop(0, color + "28");
    shade.addColorStop(1, color + "00");
    context.fillStyle = shade;
    context.fill();
    trace();
    context.strokeStyle = color;
    context.lineWidth = 1.5;
    context.lineJoin = "round";
    context.stroke();
  });
}

function renderList(indices) {
  const baseIndices = indices;
  const columns = [
    [["latestPrice", "最新价"]], [["changePct", "涨跌幅"]], [["mainFlow", "主力净流入"]],
    [["amount", "成交额"]], [["turnoverRate", "换手率"]], [["returnYtd", "今年以来"]], [["pePct", "估值水平"]], [["pe", "PE-TTM"], ["pePct", "分位", "PE 分位"]],
    [["pb", "PB"], ["pbPct", "分位", "PB 分位"]], [["roe", "ROE"]],
    [["return1w", "近1周"]], [["return1m", "近1月"]], [["return3m", "近3月"]], [["return1y", "近1年"]],
    [["peDate", "估值来源", "估值数据日"]]
  ];
  const categories = ["全部", "宽基", "行业", "港股", "风格"];
  const counts = new Map(categories.map(key => [
    key, indices.filter(index => key === "全部" || index.category === key).length
  ]));
  const categoryNames = {"行业":"行业主题", "风格":"风格策略"};
  const label = category => categoryNames[category] || category;
  const sortHeader = ([key, name, description]) => `<button type="button" data-sort="${key}" aria-label="按${description || name}排序">${name}<span aria-hidden="true">↕</span></button>`;
  const metricCell = (index, key) => `<td class="iv-table-metric">
    <div class="iv-multiple">${number(index[key])}</div>
    <div class="iv-table-rank">${rank(index[key + "Pct"])}</div></td>`;
  const sourceLine = (source, day) => source && day
    ? `<span>${valuationSources[source]} <time datetime="${day}">${day}</time></span>`
    : '<span class="iv-empty">—</span>';

  root.innerHTML = `
    <header class="iv-heading iv-list-heading">
      <div><h1>指数估值</h1><p>指数行情 · 日终估值与长期表现</p></div>
      <a class="iv-utility" href="/a-share-pe-valuation-report.html">A 股估值回测</a>
    </header>
    <section class="iv-panel iv-board">
      <div class="iv-table-controls">
        <div class="iv-filter" role="group" aria-label="指数分类">
          ${categories.filter(key => counts.get(key)).map(key => `<button type="button" data-category="${key}"
            aria-pressed="${key === "全部"}">${label(key)} <span>${counts.get(key)}</span></button>`).join("")}
        </div>
        <span class="iv-count" role="status"></span>
      </div>
      <div class="iv-banner" role="note">本页展示各指数最新行情、PE/PB 历史分位与长期阶段表现；估值温度按历史分位划分为极冷、偏冷、中性、偏热、极热五档，仅供参考。</div>
      <div class="iv-table-wrap" tabindex="0" role="region" aria-label="指数估值表">
        <table class="iv-table">
          <thead><tr>
            <th scope="col" class="iv-table-identity">指数</th>
            <th scope="col" class="iv-trend-cell">近1月走势</th>
            ${columns.map(column => `<th scope="col">${column.map(sortHeader).join('<span class="iv-sort-divider" aria-hidden="true">/</span>')}</th>`).join("")}
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </section>
    ${methodology("https://www.csindex.com.cn/", "all")}`;

  const tableBody = root.querySelector("tbody");
  const sortButtons = [...root.querySelectorAll("[data-sort]")];
  let category = "全部";
  let activeSort = null;
  let ascending = false;
  let visible = [];

  function rows() {
    visible = indices.filter(index => category === "全部" || index.category === category);
    if (activeSort) {
      const key = activeSort.dataset.sort;
      visible.sort((left, right) => {
        const a = left[key], b = right[key];
        if (a == null || b == null) return Number(a == null) - Number(b == null);
        return (typeof a === "string" ? a.localeCompare(b) : a - b) * (ascending ? 1 : -1);
      });
    }
    tableBody.innerHTML = visible.map(index => `<tr>
      <th scope="row" class="iv-table-identity">
        <a class="iv-index-link" href="/index/${index.slug}/">${index.name}</a>
        <div class="iv-index-meta">${miniBadge(index.pePct)}<span>${index.priceOnly ? "ETF " : ""}${index.code}${index.etfCode ? ` · ETF ${index.etfCode}` : ""}</span></div>
      </th>
      <td class="iv-trend-cell">
        ${index.trend?.length > 1 ? `<canvas data-trend="${index.slug}" class="iv-sparkline"
          role="img" aria-label="${index.name}近1月收盘走势图，数据截至 ${index.priceDate}"></canvas>` : "—"}
      </td>
      ${marketCells(index)}
      <td>${signedPercent(index.returnYtd)}</td>
      <td>${badge(index.pePct)}</td>
      ${metricCell(index, "pe")}${metricCell(index, "pb")}
      <td>${index.roe == null ? '<span class="iv-empty">—</span>' : `<span title="雪球基金 · ${index.roeDate}">${number(index.roe, 2)}%</span>`}</td>
      <td>${signedPercent(index.return1w)}</td>
      <td>${signedPercent(index.return1m)}</td>
      <td>${signedPercent(index.return3m)}</td>
      <td>${signedPercent(index.return1y)}</td>
      <td class="iv-table-source">${sourceLine(index.peSource, index.peDate)}
        ${index.pbDate && (index.pbSource !== index.peSource || index.pbDate !== index.peDate)
          ? `<small>PB ${sourceLine(index.pbSource, index.pbDate)}</small>` : ""}
      </td>
    </tr>`).join("");
    root.querySelector(".iv-count").textContent = `${visible.length} 个指数`;
    for (const button of sortButtons) {
      button.closest("th").removeAttribute("aria-sort");
      button.querySelector("span").textContent = "↕";
      button.classList.toggle("iv-sort-active", button === activeSort);
    }
    if (activeSort) {
      activeSort.closest("th").setAttribute("aria-sort", ascending ? "ascending" : "descending");
      activeSort.querySelector("span").textContent = ascending ? "↑" : "↓";
    }
    drawSparklines(tableBody, visible);
  }

  root.querySelectorAll("[data-category]").forEach(button => {
    button.addEventListener("click", () => {
      category = button.dataset.category;
      root.querySelectorAll("[data-category]").forEach(tab =>
        tab.setAttribute("aria-pressed", tab === button));
      rows();
    });
  });
  for (const button of sortButtons) {
    button.addEventListener("click", () => {
      ascending = activeSort === button ? !ascending : false;
      activeSort = button;
      rows();
    });
  }
  rows();
  new ResizeObserver(() => drawSparklines(tableBody, visible)).observe(tableBody);

  let previousMarket = "";
  async function refreshMarket() {
    let delay = 20_000;
    if (!document.hidden) {
      try {
        const response = await fetch("/api/index-market");
        if (!response.ok) throw new Error("Index market data is unavailable");
        const {quotes, flows, updating} = await response.json();
        delay = updating ? 5_000 : delay;
        const snapshot = JSON.stringify([quotes, flows]);
        if (snapshot !== previousMarket) {
          previousMarket = snapshot;
          indices = baseIndices.map(index => {
            const quote = quotes[index.slug];
            const current = quote && (!index.priceDate || quote.quoteDate >= index.priceDate);
            const flow = flows[index.slug];
            const sameOrNewerDay = flow && flow.flowDate >= ((current ? quote.quoteDate : index.priceDate) || "");
            return {...index, ...(current ? quote : {}), ...(sameOrNewerDay ? flow : {})};
          });
          rows();
        }
      } catch {
        // The price and returns already rendered from verified closing data remain available.
      }
    }
    window.setTimeout(refreshMarket, delay);
  }
  refreshMarket();
}

const metrics = [
  {key:"pe", label:"PE", name:"市盈率", color:"#f96c24"},
  {key:"pb", label:"PB", name:"市净率", color:"#2f7cf6"}
];

const metricSources = {danjuan:"雪球基金历史分位", csi:"中证指数估值分位", balanced:"平安数据周样本分位"};

function renderDetail(index) {
  if (index.priceOnly) {
    document.title = `${index.name} ETF 行情 · 市场先生`;
    root.innerHTML = `
      <div class="iv-back"><a class="iv-utility" href="/indices/">← 指数估值</a></div>
      <div class="iv-heading"><div><h1>${index.name} ETF 行情</h1>
        <p>ETF ${index.code} · 收盘价 ${number(index.latestPrice, 3)} 元 · ${index.priceDate || "—"}</p>
        <p>跟踪标的为 <a href="${index.benchmarkSource}">${index.benchmark}</a>，PE、PB 和 ROE 不适用。</p>
      </div></div>
      <section class="iv-panel iv-chart">
        <div class="iv-chart-head">
          <h2 id="chart-heading">ETF 收盘价走势</h2>
          <div class="iv-mode" role="group" aria-label="时间范围">
            ${[1, 3, 5, 10].map(years => `<button type="button" data-years="${years}"
              aria-pressed="${years === 10}">${years} 年</button>`).join("")}
          </div>
        </div>
        <div class="iv-chart-legend"></div>
        <div class="iv-plot">
          <canvas tabindex="0" role="img" aria-label="ETF 收盘价，按左右方向键查看数据点"></canvas>
          <div class="iv-tooltip" aria-live="polite" hidden></div>
        </div>
        <p class="iv-note" id="chart-sample"></p>
        <p class="iv-note">收盘价为腾讯财经的 ETF 场内价格，按周取样。</p>
      </section>`;
    chart(index);
    return;
  }
  const hongKong = index.market === "HK";
  const notes = [];
  const historySources = {PE:index.peHistorySource, PB:index.pbHistorySource};
  const balanced = Object.keys(historySources).filter(key => historySources[key] === "balanced");
  const savedSnapshots = Object.keys(historySources).filter(key => historySources[key] === "danjuan");
  if (historySources.PE === "csi" || index.referencePe?.source === "csi") {
    const metric = historySources.PE === "csi" ? "PE 估值" : "参考 PE 曲线";
    notes.push(`${metric}和收盘点位来自<a href="${index.csiSource}">中证指数</a>，按周取样。`);
  }
  if (balanced.length) {
    notes.push(`${balanced.join("、")} 卡片与曲线使用<a href="https://xushilu.com/valuation">Balanced Portfolio</a>公开的平安估值数据。分位按可用周样本计算。`);
  }
  if (savedSnapshots.length) {
    notes.push(`雪球 ${savedSnapshots.join("、")} 曲线按本地已保存的日终快照累计。`);
  }
  const snapshotCards = metrics.filter(metric => index[metric.key + "Source"] === "danjuan");
  if (snapshotCards.length) {
    notes.push(`顶部 ${snapshotCards.map(metric => metric.label).join("、")} 卡片使用<a href="https://danjuanfunds.com/djapi/index_eva/detail/${index.snapshotSymbol}">雪球基金</a>快照。`);
  }
  if (index.cniSource) {
    notes.push(`收盘点位来自<a href="${index.cniSource}">国证指数</a>。`);
  } else if (index.closeSource === "tencent") {
    notes.push(`收盘点位来自<a href="https://gu.qq.com/sh${index.code}/zs">腾讯财经</a>。`);
  }
  if (hongKong && historySources.PE !== "csi") {
    const priceSources = {
      hsi:['腾讯财经', 'https://gu.qq.com/hkHSI'],
      hstech:['东方财富', 'https://quote.eastmoney.com/gb/zsHSTECH.html'],
      h30533:['中证指数', index.csiSource]
    };
    const [label, url] = priceSources[index.slug] || [];
    if (url) notes.push(`收盘点位来自<a href="${url}">${label}</a>。`);
  }
  const codeLabel = index.etfCode ? `标的指数 ${index.code} · ETF ${index.etfCode}` :
    `${index.code}${index.indexSymbol ? ` · ${index.indexSymbol}` : ""}`;
  document.title = `${index.name}估值 · 市场先生`;
  root.innerHTML = `
    <div class="iv-back"><a class="iv-utility" href="/indices/">← 指数估值</a></div>
    <div class="iv-heading">
      <div><h1>${index.name}估值</h1>
        <p>${codeLabel} · ${index.category} · PE 数据日 ${index.peDate || "—"}
        · 收盘点位 ${number(index.close)}${index.closeDate && index.closeDate !== index.peDate ? `（${index.closeDate}）` : ""}</p>
        ${index.benchmark ? `<p>${index.benchmark}</p>` : ""}</div>
    </div>
    <div class="iv-cards">
      ${metrics.map(({key, label, name, color}) => `<article class="iv-panel iv-card" style="--iv-series:${color}">
        <h2>${label} · ${name}</h2>
        <div class="iv-metric"><span><strong>${number(index[key])}</strong>${index[key] == null ? "" : " 倍"}</span>
          ${badge(index[key + "Pct"])}</div>
        <p class="iv-meta">${index[key] == null ? "暂无数据" : index[key + "Pct"] == null ? "历史分位样本不足" :
          metricSources[index[key + "Source"]] || "可用历史分位"}</p>
        ${index[key + "Pct"] == null ? "" : rank(index[key + "Pct"])}
        <p class="iv-meta">${valuationSources[index[key + "Source"]] || "数据日"} · ${index[key + "Date"] || "—"}
        ${index[key + "Coverage"] == null ? "" : ` · 月末估算 · 权重覆盖 ${percent(index[key + "Coverage"])}`}</p>
      </article>`).join("")}
    </div>
    <section class="iv-panel iv-chart">
      <div class="iv-chart-head">
        <h2 id="chart-heading">估值分位走势</h2>
        <div class="iv-mode" role="group" aria-label="走势指标">
          <button type="button" data-mode="pct" aria-pressed="true">估值分位</button>
          <button type="button" data-mode="peValue" aria-pressed="false">PE-TTM</button>
          ${index.pbValues?.some(([, value]) => value != null) ? '<button type="button" data-mode="pbValue" aria-pressed="false">PB</button>' : ""}
          ${index.referencePe ? `<button type="button" data-mode="referencePe" aria-pressed="false">${valuationSources[index.referencePe.source]} PE 参考</button>` : ""}
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
      ${notes.length ? `<p class="iv-note">${notes.join("")}</p>` : ""}
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
  const source = [index.peHistory || [], index.pbHistory || []];
  const hongKong = index.market === "HK";
  const pctLines = metrics.map((metric, i) => ({
    ...metric,
    label: metric.label + " 分位", points: source[i], format: percent
  }));
  const priceOnly = index.priceOnly;
  const price = {
    key:"close", label:priceOnly ? "ETF 收盘价" : "收盘点位（右轴）",
    color:priceOnly ? "#ac542c" : "#8c8c8c", points:index.closeHistory,
    format:priceOnly ? value => number(value, 3) + " 元" : number
  };
  const valueLines = pctLines.map((metric, i) => ({
    key:metric.key + "Value", label:["PE-TTM", "PB"][i], color:metric.color,
    points:index[metric.key + "Values"] || [], format:v => number(v) + " 倍"
  }));
  if (index.referencePe) valueLines.push({
    key:"referencePe", label:`${valuationSources[index.referencePe.source]} PE 参考`, color:"#8261a6",
    points:index.referencePe.values, format:value => number(value) + " 倍"
  });
  const allDates = [...source, ...valueLines.map(line => line.points), index.closeHistory].flatMap(points => points.map(([d]) => d));
  const latest = allDates.sort().at(-1);
  let mode = priceOnly ? "close" : "pct";
  let years = 10;
  let selected = null;
  let view = null;
  const hidden = new Set(hongKong || priceOnly ? [] : ["close"]);
  const options = () => priceOnly ? [price] :
    [...(mode === "pct" ? pctLines : valueLines.filter(line => line.key === mode)), price];

  function clearSelection() {
    selected = null;
    tooltip.hidden = true;
  }

  function buildLegend() {
    legend.innerHTML = options().map(line =>
      `<label style="--iv-series:${line.color}"><input type="checkbox" data-line="${line.key}"
      ${line.points.length && !hidden.has(line.key) ? "checked" : ""} ${line.points.length ? "" : 'disabled title="暂无历史数据"'}>${line.label}</label>`).join("");
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
    context.font = '11px "Alibaba Sans", -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
    context.fillStyle = "#7d7d7d";
    const cutoffDate = latest ? new Date(latest + "T00:00:00Z") : new Date();
    cutoffDate.setUTCFullYear(cutoffDate.getUTCFullYear() - years);
    const cutoff = cutoffDate.toISOString().slice(0,10);
    const left = 44, top = 13, right = width - (hidden.has("close") ? 16 : 53), bottom = height - 30;
    const lines = options()
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
    const ratios = [...(lines.find(line => line.key === mode)?.byDate.values() || [])];
    const axisDigits = ratios.length && Math.max(...ratios) - Math.min(...ratios) < 4 ? 2 : 0;
    const precision = axisDigits ? 10 : 1;
    const low = ratios.length ? Math.floor(Math.min(...ratios) * precision) / precision : 0;
    const high = ratios.length ? Math.max(low + 1 / precision, Math.ceil(Math.max(...ratios) * precision) / precision) : 100;
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
      context.strokeStyle = mode === "pct" && (tick === 1 || tick === 4) ? "#dedede" : "#f0f0f0";
      context.stroke();
      if (!priceOnly) {
        context.textAlign = "right";
        context.fillText(number(low + share * (high - low), axisDigits) + (mode === "pct" ? "%" : ""), left - 6, row + 4);
      }
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
      context.strokeStyle = "#bfbfbf";
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
      const label = priceOnly || line.key !== "close" ? line.label : "收盘点位";
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
        root.querySelector("#chart-heading").textContent = mode === "pct" ? "估值分位走势" :
          `${valueLines.find(line => line.key === mode).label} 走势`;
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
