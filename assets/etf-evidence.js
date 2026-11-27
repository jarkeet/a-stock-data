"use strict";
{
  const fundSelect = el("etf-holdings-fund");
  const holdingChart = el("etf-holdings-chart");
  const historyChart = el("etf-history-chart");
  const historyTooltip = el("etf-history-tooltip");
  const metricSelect = el("etf-history-metric");
  const rangeSelect = el("etf-history-range");
  const reportLabels = {
    named:"具名持仓", institutions:"机构份额上限",
    institutions_excluding_feeder:"机构份额上限（已扣联接基金）", fund:"基金总份额上限",
  };
  let evidenceData;
  let rows = [];
  let selectedDay = "";

  const dateNumber = day => Date.parse(day + "T00:00:00Z");
  const units = shares => number(shares / 1e8) + " 亿份";
  const markerClass = kind => kind === "named" ? "named" : kind === "fund" ? "fund" : "institutions";
  const widthOf = chart => Math.max(268, chart.clientWidth);

  function plotFrame(width, high, low, ticks, height = 246, rightMargin = 18) {
    const left = width < 500 ? 48 : 66;
    const right = width - rightMargin;
    const top = 24;
    const bottom = height - 36;
    const y = value => bottom - (value - low) / (high - low) * (bottom - top);
    const grid = ticks.map(value => `<line x1="${left}" x2="${right}" y1="${y(value)}" y2="${y(value)}" class="etf-gridline ${value === 0 ? "etf-zero" : ""}"/>
      <text x="${left-7}" y="${y(value)+4}" text-anchor="end">${number(value/1e8)}</text>`).join("");
    return {left,right,top,bottom,y,grid: ticks.length ? grid + '<text x="4" y="13">亿份</text>' : ""};
  }

  function renderReportCoverage(points) {
    const reportDays = evidenceData.holdings.flatMap(fund => fund.points.map(point => point.day)).sort();
    const first = new Date(dateNumber(reportDays[0]));
    const last = reportDays.at(-1);
    const included = new Set(points.map(point => point.day));
    const missing = [];
    for (let month = first.getUTCMonth(); ; month += 3) {
      const day = new Date(Date.UTC(first.getUTCFullYear(), month + 1, 0)).toISOString().slice(0,10);
      if (day > last) break;
      if (day >= reportDays[0] && !included.has(day)) missing.push(day);
    }
    el("etf-holdings-coverage").textContent = missing.length
      ? `所选 ETF 已收录 ${points.length} 个报告期。图表覆盖范围内尚缺 ${missing.join("、")} 的数据点。`
      : `所选 ETF 已收录 ${points.length} 个报告期，覆盖 ${reportDays[0]} 至 ${last} 的各季度末。`;
  }

  function renderHoldings() {
    if (!evidenceData) return;
    const fund = evidenceData.holdings.find(item => item.code === fundSelect.value);
    if (!fund?.points.length) return;
    renderReportCoverage(fund.points);
    const width = widthOf(holdingChart);
    const height = 238;
    const highest = Math.max(...fund.points.map(point => point.shares));
    const high = Math.ceil(highest / 4e8) * 4e8;
    const axis = plotFrame(width, high, 0, [0,high/4,high/2,high*3/4,high], height);
    const dates = fund.points.map(point => dateNumber(point.day));
    const earliest = Math.min(...dates);
    const latest = Math.max(...dates);
    const labels = [];
    const points = fund.points.map((point, index) => {
      const x = latest === earliest ? (axis.left + axis.right) / 2
        : axis.left + 16 + (dates[index] - earliest) / (latest - earliest) * (axis.right - axis.left - 32);
      const y = axis.y(point.shares);
      const type = markerClass(point.kind);
      const name = point.day + " · " + reportLabels[point.kind] + " · " + units(point.shares);
      if (index === 0 || x - labels.at(-1).x > 52) labels.push({x, day:point.day});
      const shape = type === "institutions" ? `<path d="M ${x} ${y-6} l 6 6 -6 6 -6 -6 Z"/>`
        : type === "fund" ? `<rect x="${x-5}" y="${y-5}" width="10" height="10" rx="1.5"/>`
        : `<circle cx="${x}" cy="${y}" r="5.5"/>`;
      return `<a class="etf-report-point" data-kind="${type}" href="${esc(point.url)}" target="_blank" rel="noopener" aria-label="${esc(name + "，打开原报告")}"><title>${esc(name)}</title>
        <circle class="etf-point-hit" cx="${x}" cy="${y}" r="17"/>${shape}</a>`;
    }).join("");
    holdingChart.innerHTML = `<svg class="etf-history-svg" viewBox="0 0 ${width} ${height}" aria-label="按报告日期绘制的汇金持仓证据">
      ${axis.grid}${labels.map(({x,day}) => `<text x="${x}" y="${height-13}" text-anchor="middle">${day.slice(2)}</text>`).join("")}${points}</svg>`;
    el("etf-holdings-reports").innerHTML = fund.points.map(point => `<a href="${esc(point.url)}" target="_blank" rel="noopener">
      <time datetime="${point.day}">${point.day}</time><span>${reportLabels[point.kind]}</span><strong>${point.kind === "named" ? "" : "≤ "}${units(point.shares)}</strong><span class="etf-report-link">原报告 ↗</span></a>`).join("");
  }

  function renderHistory() {
    if (!evidenceData) return;
    const all = evidenceData.shares;
    el("etf-history-inspect").hidden = !all.length;
    if (!all.length) {
      rows = [];
      historyChart.innerHTML = '<p class="etf-empty">等待交易所披露份额。</p>';
      return;
    }
    const end = all.at(-1).day;
    const cutoff = new Date(dateNumber(end));
    const endDay = cutoff.getUTCDate();
    cutoff.setUTCDate(1);
    cutoff.setUTCMonth(cutoff.getUTCMonth() - Number(rangeSelect.value));
    const lastDay = new Date(Date.UTC(cutoff.getUTCFullYear(), cutoff.getUTCMonth()+1, 0)).getUTCDate();
    cutoff.setUTCDate(Math.min(endDay, lastDay));
    const first = cutoff.toISOString().slice(0,10);
    rows = all.filter(row => row.day >= first);
    const showNet = metricSelect.value !== "total";
    const showTotal = metricSelect.value !== "net";
    el("etf-history-net-key").hidden = !showNet;
    el("etf-history-total-key").hidden = !showTotal;
    const netValues = rows.map(row => row.net).filter(Number.isFinite);
    const totals = rows.map(row => row.total).filter(Number.isFinite);
    const groupCount = rows.filter(row => row.events.some(event => event.kind === "group")).length;
    const eventDays = rows.filter(row => row.events.length);
    const coverage = [
      showNet && `每日增减 ${netValues.length}/${rows.length} 日`,
      showTotal && `合计份额 ${totals.length}/${rows.length} 日`,
    ].filter(Boolean).join(" · ");
    el("etf-history-coverage").textContent = `${rows[0].day} 至 ${end} · ${coverage} · ${eventDays.length} 日有放量记录，其中 ${groupCount} 日有组级放量。无标记的日期可能缺少可比分钟历史。`;
    const indexValues = rows.map(row => row.indexClose).filter(Number.isFinite);
    const reference = evidenceData.benchmark;
    const showIndex = Boolean(reference && indexValues.length);
    el("etf-history-index-key").hidden = !showIndex;
    el("etf-history-index-label").textContent = showIndex ? reference.name + " 收盘点位（下图）" : "";
    const benchmarkNote = el("etf-history-benchmark-note");
    benchmarkNote.hidden = !showIndex;
    if (showIndex) {
      benchmarkNote.innerHTML = `${esc(reference.name)}（${esc(reference.code)}）的日收盘点位来自 <a href="https://gu.qq.com/sh000300" target="_blank" rel="noopener">腾讯行情</a>。`;
    }
    const width = widthOf(historyChart);
    const height = showIndex ? 416 : 280;
    const netCap = Math.ceil(Math.max(1e8,...netValues.map(Math.abs)) / 4e8) * 4e8;
    const totalCap = Math.ceil(Math.max(1e8,...totals) / 4e8) * 4e8;
    const rightMargin = showTotal ? (width < 500 ? 50 : 64) : 18;
    const axis = plotFrame(width, netCap, -netCap, showNet ? [-netCap,-netCap/2,0,netCap/2,netCap] : [], 280, rightMargin);
    const totalAxis = plotFrame(width, totalCap, 0, [], 280, rightMargin);
    const rightTicks = showTotal ? [0,totalCap/4,totalCap/2,totalCap*3/4,totalCap].map(value => {
      const y = totalAxis.y(value);
      const grid = showNet ? "" : `<line x1="${axis.left}" x2="${axis.right}" y1="${y}" y2="${y}" class="etf-gridline"/>`;
      return grid + `<text x="${axis.right+7}" y="${y+4}" class="etf-total-axis">${number(value/1e8)}</text>`;
    }).join("") + `<text x="${width-4}" y="13" text-anchor="end" class="etf-total-axis">亿份</text>` : "";
    const step = (axis.right - axis.left) / rows.length;
    const px = index => axis.left + (index + .5) * step;
    const zero = axis.y(0);
    const reports = evidenceData.holdings.find(fund => fund.code === fundSelect.value)?.points ?? [];
    const referenceLines = reports.filter(point => point.day >= rows[0].day && point.day <= end).map(point => {
      const next = rows.findIndex(row => row.day > point.day);
      const x = px(next < 0 ? rows.length-1 : next-1);
      return `<line class="etf-history-report" x1="${x}" x2="${x}" y1="${axis.top}" y2="${axis.bottom}"><title>${point.day} 报告期末</title></line>`;
    }).join("");
    let continued = false;
    const line = [];
    const marks = [];
    const dots = [];
    const bars = rows.map((row, index) => {
      const x = px(index);
      if (showTotal && Number.isFinite(row.total)) {
        const y = totalAxis.y(row.total);
        line.push(`${continued ? "L" : "M"}${x} ${y}`);
        dots.push(`<circle cx="${x}" cy="${y}" r="${Math.min(1.1,step/2)}" class="etf-share-level-dot"/>`);
        continued = true;
      } else {
        continued = false;
      }
      for (const [kind, offset] of [["group", 9], ["single", 18]]) {
        if (!row.events.some(event => event.kind === kind)) continue;
        const label = `${row.day} ${kind === "group" ? "组级" : "单品"}放量`;
        marks.push(`<a href="#etf-history-reading" data-row="${index}" class="etf-event-point" data-kind="${kind}" aria-label="${label}，查看事件"><title>${label}</title>
          <rect x="${x-Math.min(5,step/2)}" y="${axis.bottom+offset-4}" width="${Math.min(10,step)}" height="9" fill="transparent"/><circle cx="${x}" cy="${axis.bottom+offset}" r="3.5" pointer-events="none"/></a>`);
      }
      if (!showNet) return "";
      if (!Number.isFinite(row.net)) {
        return `<line x1="${x}" x2="${x}" y1="${zero-2}" y2="${zero+2}" class="etf-history-gap"/>`;
      }
      const y = axis.y(row.net);
      const w = Math.min(18,step*.78);
      return `<rect x="${x-w/2}" y="${Math.min(zero,y)}" width="${w}" height="${Math.max(1,Math.abs(y-zero))}" class="${row.net >= 0 ? "etf-share-plus" : "etf-share-minus"}"/>`;
    }).join("");
    const indexTop = axis.bottom + 55;
    const indexBottom = height - 30;
    let indexPlot = "";
    if (showIndex) {
      const low = Math.floor(Math.min(...indexValues) / 100) * 100;
      const high = Math.max(low+100,Math.ceil(Math.max(...indexValues) / 100) * 100);
      const indexY = value => indexBottom - (value - low) / (high - low) * (indexBottom - indexTop);
      const grid = [low,(low+high)/2,high].map(value => `<line x1="${axis.left}" x2="${axis.right}" y1="${indexY(value)}" y2="${indexY(value)}" class="etf-gridline"/>
        <text x="${axis.left-7}" y="${indexY(value)+4}" class="etf-index-axis" text-anchor="end">${number(value)}</text>`).join("");
      let previous = false;
      const path = rows.map((row, index) => {
        if (!Number.isFinite(row.indexClose)) {
          previous = false;
          return "";
        }
        const command = `${previous ? "L" : "M"}${px(index)} ${indexY(row.indexClose)}`;
        previous = true;
        return command;
      }).join(" ");
      indexPlot = `<rect class="etf-history-plot" x="${axis.left}" y="${indexTop}" width="${axis.right-axis.left}" height="${indexBottom-indexTop}" fill="transparent"/>
        <text x="${axis.left}" y="${indexTop-9}" class="etf-index-axis">${esc(reference.name)} · 点</text>
        ${grid}<path class="etf-index-level" d="${path}"/>`;
    }
    const labelCount = Math.min(rows.length, width < 510 ? 3 : 6);
    const labels = Array.from({length:labelCount}, (_, i) => {
      const index = labelCount === 1 ? 0 : Math.round(i*(rows.length-1)/(labelCount-1));
      const anchor = i === 0 ? "start" : i === labelCount-1 ? "end" : "middle";
      return `<text x="${px(index)}" y="${height-4}" text-anchor="${anchor}">${rows[index].day.slice(2)}</text>`;
    }).join("");
    historyChart.innerHTML = `<svg class="etf-history-svg" viewBox="0 0 ${width} ${height}" role="group" tabindex="0"
      aria-label="主池份额日度走势" aria-describedby="etf-history-guidance"
      data-left="${axis.left}" data-right="${axis.right}" data-top="${axis.top}" data-bottom="${axis.bottom}" data-step="${step}"
      ${showIndex ? `data-index-top="${indexTop}" data-index-bottom="${indexBottom}"` : ""}>
      <rect class="etf-history-plot" x="${axis.left}" y="${axis.top}" width="${axis.right-axis.left}" height="${axis.bottom-axis.top}" fill="transparent"/>
      ${axis.grid}${rightTicks}${referenceLines}${bars}${line.length ? `<path class="etf-share-level" d="${line.join(" ")}"/>${dots.join("")}` : ""}
      ${indexPlot}<line class="etf-history-cursor" y1="${axis.top}" y2="${showIndex ? indexBottom : axis.bottom}"/>
      ${labels}${marks.join("")}</svg>`;
    historyTooltip.hidden = true;
    const saved = rows.findIndex(row => row.day === selectedDay);
    const latest = rows.findLastIndex(row => Number.isFinite(row.total) || Number.isFinite(row.net));
    inspect(saved >= 0 ? saved : Math.max(0,latest));
  }

  function inspect(index) {
    const row = rows[index];
    if (!row) return;
    const svg = historyChart.querySelector("svg");
    const x = Number(svg.dataset.left) + (index+.5) * Number(svg.dataset.step);
    const cursor = svg.querySelector(".etf-history-cursor");
    cursor.setAttribute("x1", x);
    cursor.setAttribute("x2", x);
    selectedDay = row.day;
    const change = Number.isFinite(row.net) ? signed(row.net,units)
      : `待核验（可比 ${row.coverage}/${evidenceData.fundCount} 只${row.flagged.length ? "，大幅变动 " + row.flagged.join("、") : ""}）`;
    const total = Number.isFinite(row.total) ? units(row.total) : `披露 ${row.reported}/${evidenceData.fundCount} 只`;
    const close = Number.isFinite(row.indexClose) ? number(row.indexClose) + " 点" : "暂无";
    const eventText = row.events.length ? "放量 " + row.events.length + " 段" : "无已记录的放量事件";
    const name = evidenceData.benchmark?.name ?? "参考指数";
    el("etf-history-tip-day").textContent = row.day;
    el("etf-history-tip-net").textContent = change;
    el("etf-history-tip-total").textContent = total;
    el("etf-history-tip-index-label").textContent = name;
    el("etf-history-tip-index").textContent = close;
    el("etf-history-tip-events").textContent = eventText;
    el("etf-history-reading").textContent = `${row.day} · 份额增减 ${change} · 合计份额 ${total} · ${name} ${close} · ${eventText}`;
    el("etf-history-inspect").hidden = !row.events.length;
    el("etf-history-events").innerHTML = row.events.map(event => {
      const indexName = dashboard?.funds.find(fund => fund.group === event.group)?.index ?? event.group;
      return `<button type="button" data-day="${row.day}" data-code="${esc(event.codes[0])}" data-at="${esc(event.peak)}">
        ${esc(event.peak)} · ${esc(indexName)} · ${event.kind === "group" ? "组级" : "单品"}放量 · ${amount(event.amount)}元</button>`;
    }).join("");
  }

  function placeTooltip(clientX, clientY) {
    const box = historyTooltip.parentElement.getBoundingClientRect();
    historyTooltip.hidden = false;
    const width = historyTooltip.offsetWidth;
    const height = historyTooltip.offsetHeight;
    const x = clientX - box.left;
    const y = clientY - box.top;
    const horizontal = x + width + 14 <= box.width ? x+12 : x-width-12;
    historyTooltip.style.left = Math.max(4,Math.min(box.width-width-4,horizontal)) + "px";
    historyTooltip.style.top = Math.max(4,Math.min(box.height-height-4,y+12)) + "px";
  }

  function placeSelectedTooltip(index) {
    const svg = historyChart.querySelector("svg");
    const bounds = svg.getBoundingClientRect();
    const x = Number(svg.dataset.left) + (index+.5) * Number(svg.dataset.step);
    placeTooltip(bounds.left + x * bounds.width / svg.viewBox.baseVal.width,bounds.top + 28);
  }

  function inspectPointer(event) {
    const svg = event.target.closest("svg");
    if (!svg || !rows.length) return;
    const bounds = svg.getBoundingClientRect();
    const x = (event.clientX - bounds.left) * svg.viewBox.baseVal.width / bounds.width;
    const y = (event.clientY - bounds.top) * svg.viewBox.baseVal.height / bounds.height;
    const withinShares = y >= Number(svg.dataset.top) && y <= Number(svg.dataset.bottom);
    const withinIndex = y >= Number(svg.dataset.indexTop) && y <= Number(svg.dataset.indexBottom);
    if (x < Number(svg.dataset.left) || x > Number(svg.dataset.right) || (!withinShares && !withinIndex)) {
      historyTooltip.hidden = true;
      return;
    }
    const index = Math.min(rows.length-1,Math.floor((x - Number(svg.dataset.left)) / Number(svg.dataset.step)));
    if (rows[index].day !== selectedDay) inspect(index);
    placeTooltip(event.clientX,event.clientY);
  }

  historyChart.addEventListener("pointermove", event => {
    if (event.pointerType !== "touch") inspectPointer(event);
  });

  historyChart.addEventListener("pointerleave", () => { historyTooltip.hidden = true; });

  historyChart.addEventListener("focusout", () => { historyTooltip.hidden = true; });

  historyChart.addEventListener("click", event => {
    const marker = event.target.closest("a[data-row]");
    if (marker) {
      event.preventDefault();
      const index = Number(marker.dataset.row);
      inspect(index);
      placeSelectedTooltip(index);
    } else {
      inspectPointer(event);
    }
  });

  historyChart.addEventListener("keydown", event => {
    if (!event.target.matches("svg") || !rows.length) return;
    const current = rows.findIndex(row => row.day === selectedDay);
    const indexes = {ArrowLeft:current-1, ArrowRight:current+1, Home:0, End:rows.length-1};
    if (!Object.hasOwn(indexes,event.key)) return;
    event.preventDefault();
    const index = Math.max(0,Math.min(rows.length-1,indexes[event.key]));
    inspect(index);
    placeSelectedTooltip(index);
  });

  el("etf-history-events").addEventListener("click", async event => {
    const button = event.target.closest("[data-day]");
    if (!button) return;
    const {day, code, at} = button.dataset;
    if (!Array.from(daySelect.options).some(option => option.value === day)) daySelect.add(new Option(day, day));
    daySelect.value = day;
    replaySelect.value = code;
    focusTime = at;
    await refresh();
    el("replay-heading").scrollIntoView({block:"center"});
  });

  fundSelect.addEventListener("change", () => { renderHoldings(); renderHistory(); });
  metricSelect.addEventListener("change", renderHistory);
  rangeSelect.addEventListener("change", renderHistory);
  new ResizeObserver(() => {
    if (holdingChart.querySelector("svg")?.viewBox.baseVal.width !== widthOf(holdingChart)) renderHoldings();
  }).observe(holdingChart);
  new ResizeObserver(() => {
    if (historyChart.querySelector("svg")?.viewBox.baseVal.width !== widthOf(historyChart)) renderHistory();
  }).observe(historyChart);

  async function loadEvidence() {
    try {
      const data = await getJSON("/api/etf-monitor/evidence");
      if (!evidenceData) {
        fundSelect.replaceChildren(...data.holdings.map(fund => new Option(`${fund.code} ${fund.label}`,fund.code)));
        fundSelect.disabled = !data.holdings.length;
      }
      const updateHistory = JSON.stringify(data.shares) !== JSON.stringify(evidenceData?.shares);
      const updateHoldings = !evidenceData;
      evidenceData = data;
      if (updateHoldings) renderHoldings();
      if (updateHistory) renderHistory();
    } catch {
      if (!evidenceData) {
        holdingChart.innerHTML = '<p class="etf-empty">持仓证据暂时无法读取，稍后重试。</p>';
        historyChart.innerHTML = '<p class="etf-empty">日度份额暂时无法读取，稍后重试。</p>';
      }
    }
  }

  loadEvidence();
  setInterval(() => { if (!document.hidden) loadEvidence(); }, 60000);
}
