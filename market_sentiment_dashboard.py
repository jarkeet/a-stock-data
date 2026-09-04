"""A 股市场情绪日度仪表盘：生成离线单文件 HTML。"""
from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path

import pandas as pd

from market_turnover_sentiment import turnover_sentiment
from market_valuation import DEFAULT_CACHE as DEFAULT_VALUATION_CACHE
from market_valuation import DEFAULT_REPORT as DEFAULT_VALUATION_REPORT
from market_valuation import load_market_valuation, render_market_valuation_report

COMPONENTS = {
    "liquidity_score": ("量能", 30),
    "breadth_score": ("市场广度", 20),
    "short_term_score": ("短线赚钱效应", 20),
    "capital_score": ("资金风险偏好", 10),
    "momentum_score": ("市场动量", 20),
}
COMPONENT_DEFINITIONS = {
    "liquidity_score": (
        "数据：沪深A股日成交额。量化：近120日绝对成交额历史分位占50%，"
        "相对20日均额强度占30%，相对前5日均额的成交加速度历史分位占20%。"
    ),
    "breadth_score": (
        "数据：涨跌家数、站上MA20/MA60比例、20日新高/新低及10日腾落趋势。"
        "量化：按30%/25%/15%/15%/15%加权合成为0–100分。"
    ),
    "short_term_score": (
        "数据：昨涨停股收益中位数、晋级率、封板率、涨跌停强弱及最高连板。"
        "量化：各项转为近120日历史分位，再按30%/20%/20%/15%/15%合成。"
    ),
    "capital_score": (
        "数据：沪深两市融资余额合计及其日变化。量化：融资余额日增减额在近60个交易日"
        "中的历史分位；分数越高代表杠杆资金净加仓越强。"
    ),
    "momentum_score": (
        "数据：全市场平均涨幅、个股涨幅中位数、涨超2%与跌超2%占比差、"
        "上证/深证/科创50/创业板/中证500等权涨幅。量化：分别转为近120日"
        "历史分位，再按30%/25%/20%/25%合成。"
    ),
}
MOMENTUM_METRICS = [
    "mean_return_pct",
    "median_return_pct",
    "strong_up_share_pct",
    "strong_down_share_pct",
    "strong_net_share_pct",
    "index_equal_weight_return_pct",
    "mean_return_pct_score",
    "median_return_pct_score",
    "strong_net_share_pct_score",
    "index_equal_weight_return_pct_score",
    "momentum_component_coverage_pct",
]
EXTRA_METRICS = ["hs_margin_balance_yi", *MOMENTUM_METRICS]
LIQUIDITY_METRICS = [
    "ma5_yi",
    "ma20_yi",
    "ma60_yi",
    "previous_ma5_yi",
    "vs_previous_ma5",
    "vs_ma20",
    "percentile_120",
    "liquidity_level_score",
    "liquidity_relative_score",
    "liquidity_acceleration_score",
    "liquidity_component_coverage_pct",
    "liquidity_score",
    "liquidity_state",
]


def build_daily_sentiment(
    turnover_file="hs_a_share_turnover.csv",
    extra_file="market_sentiment_daily.csv",
    intraday_file="market_sentiment_intraday.csv",
):
    """返回综合分和各分项；缺失分项不会按零分拉低综合分。"""
    df = turnover_sentiment(pd.read_csv(turnover_file, parse_dates=["date"]))
    if Path(extra_file).exists():
        extra = pd.read_csv(extra_file, parse_dates=["date"])
        keep = ["date", *[key for key in [*COMPONENTS, *EXTRA_METRICS] if key in extra]]
        df = df.merge(extra[keep], on="date", how="left", suffixes=("", "_input"))
        for key in COMPONENTS:
            if f"{key}_input" in df:
                df[key] = df[f"{key}_input"].combine_first(df[key])
                df.drop(columns=f"{key}_input", inplace=True)
    for key in COMPONENTS:
        if key not in df:
            df[key] = pd.NA
        df[key] = pd.to_numeric(df[key], errors="coerce").clip(0, 100)
    for key in EXTRA_METRICS:
        if key not in df:
            df[key] = pd.NA
        df[key] = pd.to_numeric(df[key], errors="coerce")
    intraday_path = Path(intraday_file)
    if intraday_path.exists():
        intraday = pd.read_csv(intraday_path, parse_dates=["date"])
        if not intraday.empty and "snapshot_time" in intraday:
            snapshot_time = pd.to_datetime(intraday.snapshot_time.iloc[-1], errors="coerce")
            if pd.notna(snapshot_time) and snapshot_time.date() == pd.Timestamp.today().date():
                row = intraday.tail(1).copy()
                # 即使盘中快照由旧版脚本生成，也按当前量能算法即时重算，
                # 避免页面刷新后继续展示旧口径分数。
                liquidity_input = pd.concat(
                    [
                        df.loc[df.date != row.date.iloc[0], ["date", "amount_yi"]],
                        row[["date", "amount_yi"]],
                    ],
                    ignore_index=True,
                )
                liquidity_row = turnover_sentiment(liquidity_input).iloc[-1]
                for column in LIQUIDITY_METRICS:
                    row[column] = liquidity_row.get(column)
                df = pd.concat([df[df.date != row.date.iloc[0]], row], ignore_index=True, sort=False)
                df = df.sort_values("date").reset_index(drop=True)
    for key in COMPONENTS:
        df[key] = pd.to_numeric(df[key], errors="coerce").clip(0, 100)
    weights = sum(df[key].notna() * weight for key, (_, weight) in COMPONENTS.items())
    total = sum(df[key].fillna(0) * weight for key, (_, weight) in COMPONENTS.items())
    df["sentiment_score"] = (total / weights).where(weights > 0)
    df["coverage_weight_pct"] = weights
    df["available_components"] = df[list(COMPONENTS)].notna().sum(axis=1)
    df["sentiment_state"] = pd.cut(df["sentiment_score"], [-1, 25, 45, 60, 75, 100],
                                    labels=["极冷", "偏冷", "中性", "偏热", "过热"]).astype("string")
    structural_components = {
        key: value for key, value in COMPONENTS.items() if key != "momentum_score"
    }
    structural_weights = sum(
        df[key].notna() * weight
        for key, (_, weight) in structural_components.items()
    )
    structural_total = sum(
        df[key].fillna(0) * weight
        for key, (_, weight) in structural_components.items()
    )
    df["structural_score"] = (
        structural_total / structural_weights
    ).where(structural_weights > 0)
    state_bins = [-1, 25, 45, 60, 75, 100]
    state_labels = ["极冷", "偏冷", "中性", "偏热", "过热"]
    df["structural_state"] = pd.cut(
        df["structural_score"], state_bins, labels=state_labels
    ).astype("string")
    df["momentum_state"] = pd.cut(
        df["momentum_score"], state_bins, labels=state_labels
    ).astype("string")

    def market_phase(row):
        structure = pd.to_numeric(row.get("structural_score"), errors="coerce")
        momentum = pd.to_numeric(row.get("momentum_score"), errors="coerce")
        sentiment = row.get("sentiment_state")
        fallback = "待判断" if pd.isna(sentiment) else str(sentiment)
        if pd.isna(structure) or pd.isna(momentum):
            return fallback
        if momentum > 75:
            if structure > 60:
                return "全面升温"
            if structure > 45:
                return "强反弹·修复行情"
            return "超跌反弹"
        if momentum > 60:
            if structure > 60:
                return "偏热共振"
            if structure <= 45:
                return "弱势反抽"
            return "温和修复"
        if momentum <= 25:
            if structure > 60:
                return "高位退潮"
            if structure <= 45:
                return "弱势共振"
            return "快速降温"
        return fallback

    df["market_phase"] = df.apply(market_phase, axis=1).astype("string")
    return df


def path_for(
    series,
    maximum,
    width=1080,
    height=250,
    left=56,
    top=24,
    bottom=38,
    right=22,
    minimum=0,
):
    out, n = [], max(len(series) - 1, 1)
    span = max(float(maximum) - float(minimum), 1e-9)
    for i, value in enumerate(series):
        if pd.isna(value):
            continue
        x = left + i * (width - left - right) / n
        y = top + (float(maximum) - float(value)) * (height - top - bottom) / span
        out.append(("M" if not out else "L") + f"{x:.1f},{y:.1f}")
    return " ".join(out)


def extrema_annotations(
    series,
    maximum,
    *,
    width=1080,
    height=250,
    left=56,
    top=24,
    bottom=38,
    right=22,
    minimum=0,
    formatter=None,
    css_class="extrema-score",
    prefix="",
    max_label_shift=0,
    min_label_shift=0,
):
    """标注折线最高/最低点；靠近边界时自动调整标签方向。"""
    values = pd.to_numeric(pd.Series(series), errors="coerce").reset_index(drop=True)
    valid = values.dropna()
    if valid.empty:
        return ""
    span = max(float(maximum) - float(minimum), 1e-9)
    count_span = max(len(values) - 1, 1)
    formatter = formatter or (lambda value: f"{value:.1f}")
    points = [("最高", int(valid.idxmax()), float(valid.max()))]
    if valid.idxmin() != valid.idxmax():
        points.append(("最低", int(valid.idxmin()), float(valid.min())))
    output = []
    for label, index, value in points:
        x = left + index * (width - left - right) / count_span
        y = top + (float(maximum) - value) * (height - top - bottom) / span
        if x < left + 95:
            text_x, anchor = x + 7, "start"
        elif x > width - right - 95:
            text_x, anchor = x - 7, "end"
        else:
            text_x, anchor = x, "middle"
        if label == "最高":
            text_y = y + 18 if y < top + 22 else y - 10
        else:
            text_y = y - 10 if y > height - bottom - 22 else y + 18
        text_y += max_label_shift if label == "最高" else min_label_shift
        text = f"{prefix}{label} {formatter(value)}"
        output.append(
            f'<circle class="extrema-dot {css_class}" cx="{x:.1f}" cy="{y:.1f}" r="4"/>'
            f'<text class="extrema-text {css_class}" x="{text_x:.1f}" y="{text_y:.1f}" '
            f'text-anchor="{anchor}">{html.escape(text)}</text>'
        )
    return "".join(output)


def turnover_bar_chart(
    series,
    previous_series,
    dates,
    maximum,
    *,
    width=1080,
    height=290,
    left=56,
    top=24,
    bottom=38,
    right=22,
):
    """成交额柱状图：较上一交易日放量为红色、缩量为绿色。"""
    values = pd.to_numeric(pd.Series(series), errors="coerce").reset_index(drop=True)
    previous = pd.to_numeric(
        pd.Series(previous_series), errors="coerce"
    ).reset_index(drop=True)
    dates = pd.to_datetime(pd.Series(dates), errors="coerce").reset_index(drop=True)
    plot_width = width - left - right
    plot_height = height - top - bottom
    step = plot_width / max(len(values) - 1, 1)
    bar_width = max(2.5, min(8.0, step * 0.68))
    output = []
    for index, value in values.items():
        if pd.isna(value):
            continue
        prior = previous.iloc[index] if index < len(previous) else float("nan")
        if pd.isna(prior) or abs(float(value) - float(prior)) < 1e-9:
            css_class, comparison = "turnover-bar-flat", "无变化或无可比"
        elif value > prior:
            css_class, comparison = "turnover-bar-up", "较昨日放量"
        else:
            css_class, comparison = "turnover-bar-down", "较昨日缩量"
        x = left + index * step
        y = top + (float(maximum) - float(value)) * plot_height / max(float(maximum), 1e-9)
        date_text = (
            dates.iloc[index].strftime("%Y-%m-%d")
            if index < len(dates) and pd.notna(dates.iloc[index]) else ""
        )
        title = f"{date_text}：{float(value) / 10000:.2f}万亿元，{comparison}"
        output.append(
            f'<rect class="turnover-bar {css_class}" x="{x - bar_width / 2:.2f}" '
            f'y="{y:.2f}" width="{bar_width:.2f}" height="{height - bottom - y:.2f}" '
            f'data-chart-date="{html.escape(date_text)}" data-chart-series="日成交额" '
            f'data-chart-value="{float(value) / 10000:.2f}万亿元" '
            f'data-chart-extra="{html.escape(comparison)}" tabindex="0" role="button" '
            f'aria-label="{html.escape(title)}">'
            f"<title>{html.escape(title)}</title></rect>"
        )
    return "".join(output)


def line_click_points(
    series,
    dates,
    maximum,
    *,
    series_name,
    formatter,
    css_class="chart-hit-score",
    width=1080,
    height=250,
    left=56,
    top=24,
    bottom=38,
    right=22,
    minimum=0,
):
    """为折线生成透明但易点击的数据点，并携带弹窗所需字段。"""
    values = pd.to_numeric(pd.Series(series), errors="coerce").reset_index(drop=True)
    dates = pd.to_datetime(pd.Series(dates), errors="coerce").reset_index(drop=True)
    span = max(float(maximum) - float(minimum), 1e-9)
    count_span = max(len(values) - 1, 1)
    output = []
    for index, value in values.items():
        if pd.isna(value):
            continue
        x = left + index * (width - left - right) / count_span
        y = top + (float(maximum) - float(value)) * (height - top - bottom) / span
        date_text = (
            dates.iloc[index].strftime("%Y-%m-%d")
            if index < len(dates) and pd.notna(dates.iloc[index]) else ""
        )
        value_text = formatter(float(value))
        label = f"{date_text}，{series_name} {value_text}"
        output.append(
            f'<circle class="chart-hit {css_class}" cx="{x:.2f}" cy="{y:.2f}" r="8" '
            f'data-chart-date="{html.escape(date_text)}" '
            f'data-chart-series="{html.escape(series_name)}" '
            f'data-chart-value="{html.escape(value_text)}" tabindex="0" role="button" '
            f'aria-label="{html.escape(label)}"><title>{html.escape(label)}</title></circle>'
        )
    return "".join(output)


def date_axis_labels(dates, width=1080, left=56, right=22, y=278, count=5):
    """为时间序列生成稀疏且不重叠的交易日期标签。"""
    if len(dates) == 0:
        return ""
    indexes = sorted({round(i * (len(dates) - 1) / max(count - 1, 1)) for i in range(count)})
    labels = []
    for index in indexes:
        x = left + index * (width - left - right) / max(len(dates) - 1, 1)
        anchor = "start" if index == 0 else ("end" if index == len(dates) - 1 else "middle")
        labels.append(
            f'<text x="{x:.1f}" y="{y}" text-anchor="{anchor}">{dates.iloc[index]:%Y-%m-%d}</text>'
        )
    return "".join(labels)


def market_valuation_section(cache_file=DEFAULT_VALUATION_CACHE):
    """仪表盘仅保留完整报告入口与沪深300 PE-TTM概览图。"""
    payload, status = load_market_valuation(cache_file)
    if not payload:
        warning = html.escape(status.get("warning") or "暂无可用估值数据")
        return (
            '<section class="valuation-market"><div class="section-head">'
            '<h2>A股 PE-TTM 历史分位与回测</h2>'
            '<small>申万A指代理万得全A · 中证官方沪深300</small></div>'
            f'<p class="note">{warning}</p></section>'
        )
    try:
        render_market_valuation_report(payload, DEFAULT_VALUATION_REPORT)
    except Exception as error:
        status = dict(status)
        prior = status.get("warning") or ""
        status["warning"] = "；".join(filter(None, [prior, f"完整报告生成失败：{error}"]))

    series = payload["series"]["hs300"]
    stats = series["stats"]
    points = series["data"]
    dates = pd.Series(pd.to_datetime([point[0] for point in points], unit="ms"))
    values = pd.Series([float(point[1]) for point in points])
    width, height = 1080, 360
    left, right, top, bottom = 56, 22, 34, 52
    raw_low = min(float(values.min()), float(stats["q20"]))
    raw_high = max(float(values.max()), float(stats["q80"]))
    padding = max((raw_high - raw_low) * 0.06, 0.5)
    minimum = max(0.0, math.floor(raw_low - padding))
    maximum = math.ceil(raw_high + padding)
    span = max(maximum - minimum, 1e-9)

    def chart_y(value):
        return top + (maximum - float(value)) * (height - top - bottom) / span

    pe_path = path_for(
        values,
        maximum,
        width=width,
        height=height,
        left=left,
        right=right,
        top=top,
        bottom=bottom,
        minimum=minimum,
    )
    extrema = extrema_annotations(
        values,
        maximum,
        width=width,
        height=height,
        left=left,
        right=right,
        top=top,
        bottom=bottom,
        minimum=minimum,
        formatter=lambda value: f"{value:.2f}倍",
        css_class="extrema-valuation",
        prefix="PE",
    )
    date_labels = date_axis_labels(dates, width=width, left=left, right=right, y=338, count=6)
    grid_values = [minimum + span * index / 4 for index in range(5)]
    grid_lines = "".join(
        f'<line class="axis" x1="{left}" y1="{chart_y(value):.1f}" '
        f'x2="{width-right}" y2="{chart_y(value):.1f}"/>'
        f'<text x="4" y="{chart_y(value)+4:.1f}">{value:.0f}</text>'
        for value in reversed(grid_values)
    )
    p20_y, p80_y = chart_y(stats["q20"]), chart_y(stats["q80"])
    latest_y = chart_y(stats["current"])
    warning = status.get("warning") or ""
    warning_html = (
        f'<span class="valuation-warning"> · {html.escape(warning)}</span>' if warning else ""
    )
    return f'''
<section class="valuation-market valuation-summary">
  <div class="section-head"><h2>沪深300 · PE-TTM 走势</h2>
    <small>中证指数官网官方口径 · {stats['start']} 至 {stats['end']}</small></div>
  <a class="valuation-report-link" href="{DEFAULT_VALUATION_REPORT.name}" target="_blank" rel="noopener">
    <span><b>打开 A股 PE-TTM 历史分位与未来收益回测</b><small>万得全A代理、沪深300、滚动10年分位、历史锚点及无前视回测</small></span>
    <strong>打开完整报告 →</strong>
  </a>
  <svg class="valuation-chart" viewBox="0 0 {width} {height}" role="img" aria-label="沪深300 PE-TTM走势，含20%和80%历史分位参考线">
    <rect class="valuation-zone-high" x="{left}" y="{top}" width="{width-left-right}" height="{max(p80_y-top, 0):.1f}"/>
    <rect class="valuation-zone-low" x="{left}" y="{p20_y:.1f}" width="{width-left-right}" height="{max(height-bottom-p20_y, 0):.1f}"/>
    {grid_lines}
    <line class="valuation-p80" x1="{left}" y1="{p80_y:.1f}" x2="{width-right}" y2="{p80_y:.1f}"/>
    <line class="valuation-p20" x1="{left}" y1="{p20_y:.1f}" x2="{width-right}" y2="{p20_y:.1f}"/>
    <text class="valuation-line-label valuation-high" x="{width-right-4}" y="{p80_y-6:.1f}" text-anchor="end">P80 {stats['q80']:.2f}倍</text>
    <text class="valuation-line-label valuation-low" x="{width-right-4}" y="{p20_y+16:.1f}" text-anchor="end">P20 {stats['q20']:.2f}倍</text>
    <path class="valuation-halo" d="{pe_path}"/><path class="valuation-pe" d="{pe_path}"/>{extrema}
    <circle class="valuation-latest" cx="{width-right}" cy="{latest_y:.1f}" r="5"><title>最新 {stats['current']:.2f}倍</title></circle>
    <text class="valuation-current" x="{width-right-9}" y="{latest_y-8:.1f}" text-anchor="end">最新 {stats['current']:.2f}倍 · 分位 {stats['pct']:.1f}%</text>
    {date_labels}<text x="4" y="18">PE-TTM（倍）</text>
  </svg>
  <p class="note">沪深300采用中证指数官网 000300 的日频 PE-TTM；官网可用序列自 {stats['start']} 起，因此实际历史长度约15年。P20/P80按全部 {stats['n']:,} 个日频观察值计算，分别为 {stats['q20']:.2f}倍与 {stats['q80']:.2f}倍；最新 {stats['current']:.2f}倍，处于 {stats['pct']:.1f}% 历史分位。完整报告每天最多生成一次，盘中仪表盘刷新不会重复改写{warning_html}。</p>
</section>'''


def sparkline_path(values, width=180, height=58, padding=4):
    """将指数分钟点位转换为卡片内迷你折线路径。"""
    values = [float(value) for value in values if pd.notna(value)]
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = max(high - low, max(abs(high), 1) * 0.0005)
    points = []
    for index, value in enumerate(values):
        x = padding + index * (width - padding * 2) / (len(values) - 1)
        y = padding + (high - value) * (height - padding * 2) / span
        points.append(("M" if index == 0 else "L") + f"{x:.1f},{y:.1f}")
    return " ".join(points)


def industry_treemap(items, layout_width=3.7, layout_height=1.0):
    """有序 Binary Treemap：递归切分长边，使宽度和高度都随面积变化。"""
    raw_amounts = {
        id(item): max(float(item.get("amount_yi") or 0), 0.01)
        for item in items
    }
    minimum = min(raw_amounts.values())
    maximum = max(raw_amounts.values())
    minimum_display_ratio = 0.25

    def amount_weight(item):
        """Min-Max 映射到 [25%, 100%]，保护小行业的可读面积。"""
        if maximum <= minimum:
            return 1.0
        normalized = (raw_amounts[id(item)] - minimum) / (maximum - minimum)
        return minimum_display_ratio + (1 - minimum_display_ratio) * normalized

    ordered = sorted(items, key=lambda item: float(item["change_pct"]), reverse=True)
    rectangles = []

    def place(group, x, y, width, height):
        if len(group) == 1:
            rectangles.append((
                group[0],
                x / layout_width * 100,
                y / layout_height * 100,
                width / layout_width * 100,
                height / layout_height * 100,
            ))
            return
        weights = [amount_weight(item) for item in group]
        total = sum(weights)
        running = 0.0
        split_at, best_gap = 1, float("inf")
        for index, weight in enumerate(weights[:-1], start=1):
            running += weight
            gap = abs(total / 2 - running)
            if gap < best_gap:
                split_at, best_gap = index, gap
        first, second = group[:split_at], group[split_at:]
        first_ratio = sum(weights[:split_at]) / total
        if width >= height:
            first_width = width * first_ratio
            place(first, x, y, first_width, height)
            place(second, x + first_width, y, width - first_width, height)
        else:
            first_height = height * first_ratio
            place(first, x, y, width, first_height)
            place(second, x, y + first_height, width, height - first_height)

    place(ordered, 0.0, 0.0, layout_width, layout_height)
    return rectangles


def industry_tile_html(item, rectangle):
    """生成行业热力单元：面积映射成交额，颜色映射涨跌幅。"""
    change_pct = float(item["change_pct"])
    magnitude = abs(change_pct)
    color_band = min(int(magnitude / 0.5), 10)
    alpha = 0.2 + 0.8 * color_band / 10
    if magnitude >= 5.0:
        alpha = 1.0
    if change_pct > 0:
        background = f"rgba(229,72,77,{alpha:.3f})"
        direction = "上涨"
    elif change_pct < 0:
        background = f"rgba(25,166,101,{alpha:.3f})"
        direction = "下跌"
    else:
        background = f"rgba(105,119,138,{alpha:.3f})"
        direction = "平盘"
    x, y, width, height = rectangle
    amount_yi = float(item.get("amount_yi") or 0)
    position = (
        f"left:{x:.3f}%;top:{y:.3f}%;width:{width:.3f}%;height:{height:.3f}%;"
    )
    title = (
        f'{item["name"]}：{direction}{abs(change_pct):.2f}%，'
        f'成交额 {amount_yi:,.2f} 亿元'
    )
    return (
        f'<article class="industry-tile" style="{position}background:{background};'
        'color:#ffffff" '
        f'title="{html.escape(title)}" '
        f'aria-label="{html.escape(title)}">'
        '<span class="industry-tile-label">'
        f'<b>{html.escape(item["name"])}</b>'
        f'<strong>{change_pct:+.2f}%</strong>'
        "</span>"
        "</article>"
    )


def format_heat(value):
    value = float(value or 0)
    if value >= 10000:
        return f"{value / 10000:.1f}万"
    return f"{value:,.0f}"


def hot_rank_row(item, kind):
    rank = int(item.get("rank") or 0)
    change = item.get("change_pct")
    if change is None or pd.isna(change):
        change_text, change_class = "待更新", "tone-missing"
    else:
        change = float(change)
        change_text = f"{change:+.2f}%"
        change_class = "rise" if change > 0 else ("fall" if change < 0 else "tone-neutral")
    reason_parts = []
    detail = str(item.get("reason") or "").strip()
    if kind == "stock":
        reason_title = str(item.get("reason_title") or "").strip()
        if reason_title:
            reason_parts.append(reason_title)
        popularity = str(item.get("popularity_tag") or "").strip()
        if popularity:
            reason_parts.append(popularity)
        reason_parts.extend(str(value) for value in (item.get("concepts") or []) if value)
    else:
        hot_tag = str(item.get("hot_tag") or "").strip()
        if hot_tag:
            reason_parts.append(hot_tag)
        reason = str(item.get("reason") or "").strip()
        if reason:
            reason_parts.append(reason)
        etf_name = str(item.get("etf_name") or "").strip()
        etf_change = item.get("etf_change_pct")
        if etf_name:
            etf_text = etf_name
            if etf_change is not None and not pd.isna(etf_change):
                etf_text += f" {float(etf_change):+.2f}%"
            reason_parts.append(etf_text)
    reason_parts = list(dict.fromkeys(reason_parts))
    reason_html = "".join(
        f"<span>{html.escape(value)}</span>" for value in reason_parts
    ) or '<span class="reason-missing">同花顺暂未提供原因</span>'
    title = detail or "；".join(reason_parts)
    rank_class = " rank-top" if 0 < rank <= 3 else ""
    return (
        "<tr>"
        f'<td><b class="rank-badge{rank_class}">{rank}</b></td>'
        f'<td><strong>{html.escape(str(item.get("name") or ""))}</strong>'
        f'<small class="stock-code">{html.escape(str(item.get("code") or ""))}</small></td>'
        f'<td class="{change_class} hot-number">{change_text}</td>'
        f'<td class="hot-number">{format_heat(item.get("heat"))}</td>'
        f'<td class="hot-reason" title="{html.escape(title)}">{reason_html}</td>'
        "</tr>"
    )


def hot_ranking_table(items, kind):
    rows = "".join(hot_rank_row(item, kind) for item in items)
    if not rows:
        rows = '<tr><td colspan="5" class="hot-empty">本次刷新未取得数据</td></tr>'
    return (
        '<div class="hot-table-wrap"><table class="hot-table"><thead><tr>'
        "<th>排名</th><th>标的</th><th>涨跌幅</th><th>热度</th><th>上榜原因 / 标签</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def hot_ranking_panel(title, items, kind):
    return (
        '<article class="hot-panel">'
        f'<div class="hot-panel-head"><h3>{html.escape(title)}</h3>'
        f"<small>实际返回 {len(items)} 名</small></div>"
        f"{hot_ranking_table(items, kind)}</article>"
    )


def hot_stock_panel(items):
    """热股榜与浏览器本地自选股共用一个卡片，通过页签切换。"""
    return (
        '<article class="hot-panel hot-stock-panel">'
        '<div class="hot-panel-head"><h3>股票榜</h3>'
        '<small>热股 / 自选股</small></div>'
        '<div class="hot-stock-tabs" role="tablist" aria-label="股票榜分类">'
        '<button type="button" role="tab" data-hot-stock-tab="ranking" '
        f'aria-selected="true">热股榜<small>{len(items)}</small></button>'
        '<button type="button" role="tab" data-hot-stock-tab="watchlist" '
        'aria-selected="false">自选股<small data-watchlist-count>0</small></button>'
        "</div>"
        '<div class="hot-stock-view" data-hot-stock-view="ranking">'
        f'{hot_ranking_table(items, "stock")}</div>'
        '<div class="hot-stock-view watchlist-view" '
        'data-hot-stock-view="watchlist" hidden>'
        '<form class="watchlist-form" data-watchlist-form>'
        '<label><span>市场</span><select data-watchlist-market>'
        '<option value="A">A股</option><option value="HK">港股</option>'
        '<option value="US">美股</option><option value="KR">韩股</option>'
        "</select></label>"
        '<label class="watchlist-code-field"><span>代码 / 名称 / 拼音 / 英文</span>'
        '<input data-watchlist-code autocomplete="off" maxlength="48" '
        'placeholder="600519 / 贵州茅台 / GZMT" aria-label="搜索股票" '
        'role="combobox" aria-autocomplete="list" aria-expanded="false" '
        'aria-controls="watchlist-suggestions">'
        '<div class="watchlist-suggestions" id="watchlist-suggestions" '
        'data-watchlist-suggestions role="listbox" hidden></div></label>'
        '<button type="submit">添加</button></form>'
        '<div class="watchlist-feedback">'
        '<span data-watchlist-feedback>自选保存在本机浏览器；行情每分钟刷新</span>'
        '<button type="button" class="watchlist-refresh" data-watchlist-refresh '
        'aria-label="手动刷新自选股行情" title="手动刷新自选股行情">刷新</button></div>'
        '<div class="hot-table-wrap watchlist-table-wrap">'
        '<table class="hot-table watchlist-table"><thead><tr>'
        '<th>标的</th><th>迷你走势</th><th>最新价</th>'
        '<th class="watchlist-change-sort-head" aria-sort="none">'
        '<button type="button" data-watchlist-change-sort '
        'data-sort-state="manual" aria-label="涨跌幅排序：默认顺序">'
        '涨跌幅<span class="watchlist-sort-icon" aria-hidden="true"></span>'
        '</button></th><th>成交额</th>'
        '<th title="当前每分钟均量相对近5日平均每分钟量">量比</th><th></th>'
        '</tr></thead><tbody data-watchlist-body>'
        '<tr><td colspan="7" class="hot-empty">还没有自选股</td></tr>'
        "</tbody></table></div></div></article>"
    )


def hot_plate_panel(concepts, industries, indices):
    categories = [
        ("concept", "概念板块", concepts),
        ("industry", "行业板块", industries),
        ("index", "指数板块", indices),
    ]
    tabs = "".join(
        f'<button type="button" role="tab" data-hot-plate-tab="{key}" '
        f'aria-selected="{"true" if index == 0 else "false"}">'
        f"{label}<small>{len(items)}</small></button>"
        for index, (key, label, items) in enumerate(categories)
    )
    views = "".join(
        f'<div class="hot-plate-view" data-hot-plate-view="{key}"'
        f'{" hidden" if index else ""}>'
        f'{hot_ranking_table(items, "plate")}</div>'
        for index, (key, _, items) in enumerate(categories)
    )
    return (
        '<article class="hot-panel hot-plate-panel">'
        '<div class="hot-panel-head"><h3>板块榜</h3><small>点击页签切换</small></div>'
        f'<div class="hot-plate-tabs" role="tablist" aria-label="板块榜分类">{tabs}</div>'
        f"{views}</article>"
    )


WATCHLIST_CSS = """<style>
.hot-stock-tabs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;padding:0 12px 12px}
.hot-stock-tabs button{appearance:none;border:1px solid #e3e9f1;border-radius:8px;padding:9px 6px;background:#f4f6f9;color:#59677c;font:inherit;font-weight:700;cursor:pointer;transition:background .15s ease,border-color .15s ease,color .15s ease}
.hot-stock-tabs button:hover{border-color:#f0a8ab;background:#fff7f7}
.hot-stock-tabs button[aria-selected="true"]{border-color:#f3b4b7;background:#fff0f1;color:#d93f45;box-shadow:inset 0 -2px 0 #e5484d}
.hot-stock-tabs button small{margin-left:4px;color:inherit;font-size:10px;font-weight:600}
.hot-stock-view[hidden]{display:none}
.watchlist-view{padding:0 12px 12px}
.watchlist-form{display:grid;grid-template-columns:104px minmax(0,1fr) 68px;gap:8px;align-items:end;padding:11px;border:1px solid #e6edf5;border-radius:10px;background:#f8fafc}
.watchlist-form label span{display:block;margin:0 0 5px;color:#68778d;font-size:11px}
.watchlist-code-field{position:relative}
.watchlist-form select,.watchlist-form input{box-sizing:border-box;width:100%;height:36px;border:1px solid #d9e2ed;border-radius:7px;background:#fff;color:#182b49;font:inherit;padding:0 9px;outline:none}
.watchlist-form select:focus,.watchlist-form input:focus{border-color:#e87277;box-shadow:0 0 0 3px rgba(229,72,77,.1)}
.watchlist-form>button{height:36px;border:0;border-radius:7px;background:#e5484d;color:#fff;font:inherit;font-weight:700;cursor:pointer}
.watchlist-form>button:hover{background:#d33d43}
.watchlist-suggestions{position:absolute;z-index:20;top:calc(100% + 5px);left:0;right:0;max-height:260px;overflow:auto;border:1px solid #dbe3ed;border-radius:9px;background:#fff;box-shadow:0 12px 28px rgba(28,43,66,.16)}
.watchlist-suggestions[hidden]{display:none}
.watchlist-suggestion{display:grid;width:100%;grid-template-columns:minmax(0,1fr) auto;gap:4px 12px;padding:9px 10px;border:0;border-bottom:1px solid #eef2f6;background:#fff;color:#182b49;text-align:left;cursor:pointer}
.watchlist-suggestion:last-child{border-bottom:0}
.watchlist-suggestion:hover,.watchlist-suggestion.is-active{background:#fff3f4}
.watchlist-suggestion strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
.watchlist-suggestion small{color:#7b8798;font-size:10px}
.watchlist-suggestion-source{grid-column:2;grid-row:1/3;align-self:center;padding:2px 6px;border-radius:9px;background:#f0f3f7;color:#7b8798;font-size:9px}
.watchlist-feedback{display:flex;align-items:center;justify-content:space-between;gap:8px;min-height:24px;padding:6px 2px 3px;color:#68778d;font-size:11px}
.watchlist-feedback [data-watchlist-feedback]{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.watchlist-feedback [data-watchlist-feedback].is-error{color:#b42318}
.watchlist-refresh{flex:0 0 auto;min-width:48px;height:26px;border:1px solid #d9e2ed;border-radius:6px;padding:0 9px;background:#fff;color:#536278;font:inherit;font-weight:700;cursor:pointer}
.watchlist-refresh:hover{border-color:#f0a8ab;background:#fff7f7;color:#d33d43}
.watchlist-refresh:focus-visible{outline:2px solid rgba(229,72,77,.28);outline-offset:1px}
.watchlist-refresh:disabled{cursor:wait;opacity:.55}
.watchlist-table-wrap{max-height:552px;border:1px solid #edf1f6;border-radius:9px}
.watchlist-table td:not(.watchlist-action){vertical-align:middle}
.watchlist-table th:first-child,.watchlist-table td:first-child{width:130px;min-width:124px}
.watchlist-table th:nth-child(2),.watchlist-table td:nth-child(2){width:88px}
.watchlist-table th:nth-child(3),.watchlist-table td:nth-child(3){width:70px;min-width:66px;text-align:right;white-space:nowrap}
.watchlist-table th:nth-child(4),.watchlist-table td:nth-child(4){width:86px;min-width:82px;text-align:right;white-space:nowrap}
.watchlist-table th:nth-child(5),.watchlist-table td:nth-child(5){width:78px;min-width:72px;text-align:right;white-space:nowrap}
.watchlist-table th:nth-child(6),.watchlist-table td:nth-child(6){width:54px;min-width:50px;text-align:right;white-space:nowrap}
.watchlist-change-sort-head{padding:0!important}
.watchlist-change-sort-head button{display:inline-flex;align-items:center;justify-content:flex-end;gap:5px;width:100%;padding:10px 7px;border:0;background:transparent;color:inherit;font:inherit;font-weight:inherit;white-space:nowrap;cursor:pointer}
.watchlist-change-sort-head button:hover{color:#d93f45}
.watchlist-sort-icon{position:relative;display:inline-block;width:8px;height:13px;flex:0 0 8px}
.watchlist-sort-icon:before,.watchlist-sort-icon:after{content:"";position:absolute;left:0;border-left:4px solid transparent;border-right:4px solid transparent}
.watchlist-sort-icon:before{top:1px;border-bottom:5px solid #c7cfda}
.watchlist-sort-icon:after{bottom:1px;border-top:5px solid #c7cfda}
.watchlist-change-sort-head button[data-sort-state="change-desc"] .watchlist-sort-icon:after{border-top-color:#e5484d}
.watchlist-change-sort-head button[data-sort-state="change-desc"] .watchlist-sort-icon:before{border-bottom-color:#e3e8ef}
.watchlist-change-sort-head button[data-sort-state="change-asc"] .watchlist-sort-icon:before{border-bottom-color:#e5484d}
.watchlist-change-sort-head button[data-sort-state="change-asc"] .watchlist-sort-icon:after{border-top-color:#e3e8ef}
.watchlist-identity strong{display:block}
.watchlist-identity small{display:block;margin-top:3px;color:#8a96a8;font-size:10px}
.watchlist-identity strong,.watchlist-identity small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.watchlist-spark{width:84px;height:40px;color:#8a96a8}
.watchlist-spark .watchlist-zero-line{stroke:#cbd3df;stroke-width:1;stroke-dasharray:3 3;vector-effect:non-scaling-stroke}
.watchlist-spark path{fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.watchlist-spark.spark-up{color:#e5484d}.watchlist-spark.spark-down{color:#19a665}
.watchlist-price{font-weight:750;font-variant-numeric:tabular-nums}
.watchlist-turnover{color:#536278;font-variant-numeric:tabular-nums}
.watchlist-volume-ratio{font-weight:700;color:#536278;font-variant-numeric:tabular-nums}
.watchlist-action{width:58px;text-align:right!important;vertical-align:bottom!important;padding-right:10px!important;padding-bottom:9px!important}
.watchlist-remove{appearance:none;border:1px solid #e1e7ef;border-radius:6px;padding:5px 8px;background:#fff;color:#7a8798;cursor:pointer}
.watchlist-remove:hover{border-color:#f0a8ab;color:#d33d43;background:#fff7f7}
.watchlist-row-error td:nth-child(2){color:#b42318;font-size:11px}
.watchlist-row[data-reorderable="true"]{cursor:grab;touch-action:none}
.watchlist-row[data-reorderable="true"] .watchlist-identity{position:relative;padding-left:22px}
.watchlist-row[data-reorderable="true"] .watchlist-identity:before{content:"⋮⋮";position:absolute;left:7px;top:50%;transform:translateY(-50%);color:#bcc5d1;font-size:10px;letter-spacing:-3px}
.watchlist-table tbody.is-reordering{user-select:none}
.watchlist-table tbody.is-reordering .watchlist-row{transition:transform .12s ease}
.watchlist-row.is-pressing td{background:#fff8f8}
.watchlist-row.is-dragging{cursor:grabbing;opacity:.72}
.watchlist-row.is-dragging td{background:#fff3f4!important;border-color:#f2c4c7}
@media(max-width:600px){
  .watchlist-table th,.watchlist-table td{padding-left:3px;padding-right:3px}
  .watchlist-table th:first-child,.watchlist-table td:first-child{width:88px;min-width:88px}
  .watchlist-table th:nth-child(2),.watchlist-table td:nth-child(2){width:44px}
  .watchlist-table th:nth-child(3),.watchlist-table td:nth-child(3){width:50px;min-width:50px}
  .watchlist-table th:nth-child(4),.watchlist-table td:nth-child(4){width:62px;min-width:62px}
  .watchlist-table th:nth-child(5),.watchlist-table td:nth-child(5){width:64px;min-width:64px}
  .watchlist-table th:nth-child(6),.watchlist-table td:nth-child(6){width:42px;min-width:42px}
  .watchlist-change-sort-head button{gap:2px;padding-left:2px;padding-right:2px}
  .watchlist-sort-icon{transform:scale(.82)}
  .watchlist-spark{width:40px}
  .watchlist-action{width:40px;padding-right:3px!important}
  .watchlist-remove{padding:5px 5px}
}
@media(max-width:450px){
  .watchlist-form{grid-template-columns:88px minmax(0,1fr)}
  .watchlist-form>button{grid-column:1/3}
  .watchlist-table thead{display:table-header-group}
  .watchlist-table,.watchlist-table tbody{display:table}
  .watchlist-table tr{display:table-row;padding:0}
  .watchlist-table td{display:table-cell;padding:8px 4px}
  .watchlist-table th:first-child,.watchlist-table td:first-child{width:80px;min-width:80px}
  .watchlist-table td:nth-child(7){display:table-cell;grid-column:auto}
  .watchlist-table th:nth-child(2),.watchlist-table td:nth-child(2){display:none}
  .watchlist-action{width:44px;padding-right:4px!important;padding-bottom:7px!important}
}
</style>"""


WATCHLIST_SCRIPT = r"""<script>
(function(){
  var storageKey="market-sentiment-watchlist-v1";
  var sortStorageKey="market-sentiment-watchlist-sort-v1";
  var quoteStorageKey="market-sentiment-watchlist-quotes-v1";
  var localDashboardOrigin="http://127.0.0.1:8765";
  var panel=document.querySelector(".hot-stock-panel");
  if(!panel)return;
  var body=panel.querySelector("[data-watchlist-body]");
  var form=panel.querySelector("[data-watchlist-form]");
  var market=panel.querySelector("[data-watchlist-market]");
  var code=panel.querySelector("[data-watchlist-code]");
  var feedback=panel.querySelector("[data-watchlist-feedback]");
  var suggestionsBox=panel.querySelector("[data-watchlist-suggestions]");
  var refreshButton=panel.querySelector("[data-watchlist-refresh]");
  var sortButton=panel.querySelector("[data-watchlist-change-sort]");
  var sortHeader=sortButton.closest("th");
  var countNodes=panel.querySelectorAll("[data-watchlist-count]");
  var placeholders={
    A:"600519 / 贵州茅台 / GZMT",
    HK:"00700 / 腾讯控股 / TXKG",
    US:"AAPL / Apple",
    KR:"000660.KS / SK海力士"
  };
  var chineseSecurityNames={
    "KR:000660":"SK海力士",
    "KR:005930":"三星电子"
  };
  var suggestions=[],activeSuggestion=-1,searchTimer=0,searchSerial=0,lastSearchQuery="";
  function preferredDisplayName(marketKey,securityCode,upstreamName){
    var baseCode=String(securityCode||"").toUpperCase().split(".")[0];
    return chineseSecurityNames[String(marketKey||"").toUpperCase()+":"+baseCode]||
      upstreamName||securityCode||""
  }
  function normalizeItems(value){
    var seen={};
    return(Array.isArray(value)?value:[]).filter(function(item){
      var itemMarket=String(item&&item.market||"").toUpperCase();
      var itemCode=String(item&&item.code||"").toUpperCase();
      var key=itemMarket+":"+itemCode;
      if(!itemMarket||!itemCode||seen[key])return false;
      item.market=itemMarket;item.code=itemCode;seen[key]=true;return true
    }).slice(0,30)
  }
  function loadItems(){
    try{
      var value=JSON.parse(localStorage.getItem(storageKey)||"[]");
      return normalizeItems(value);
    }catch(error){return []}
  }
  var importedShouldReplace=false;
  function takeImportedItems(){
    if(location.protocol==="file:")return [];
    try{
      var target=new URL(location.href);
      var importedRaw=target.searchParams.get("watchlist");
      if(!importedRaw)return [];
      importedShouldReplace=target.searchParams.get("watchlistMode")==="replace";
      target.searchParams.delete("watchlist");
      target.searchParams.delete("watchlistMode");
      history.replaceState(null,"",target.pathname+target.search+target.hash);
      return normalizeItems(JSON.parse(importedRaw))
    }catch(error){return []}
  }
  var importedItems=takeImportedItems();
  var items=loadItems();
  function loadQuoteCache(){
    try{
      var payload=JSON.parse(sessionStorage.getItem(quoteStorageKey)||"null");
      if(!payload||Date.now()-Number(payload.savedAt)>5*60*1000)return {};
      return payload.quotes&&typeof payload.quotes==="object"?payload.quotes:{}
    }catch(error){return {}}
  }
  var quoteCache=loadQuoteCache(),refreshInFlight=false,persistQueue=Promise.resolve();
  var initialSyncComplete=location.protocol==="file:";
  var sortMode=(function(){
    try{
      var value=localStorage.getItem(sortStorageKey)||"manual";
      return ["manual","change-desc","change-asc"].includes(value)?value:"manual"
    }catch(error){return "manual"}
  })();
  function itemKey(item){return String(item.market||"").toUpperCase()+":"+String(item.code||"").toUpperCase()}
  function saveItemsLocally(){
    try{localStorage.setItem(storageKey,JSON.stringify(items))}catch(error){}
    updateCount()
  }
  function persistQuoteCache(){
    var active={};
    items.forEach(function(item){
      var key=itemKey(item),result=quoteCache[key];
      if(result&&result.ok)active[key]=result
    });
    try{
      sessionStorage.setItem(quoteStorageKey,JSON.stringify({
        savedAt:Date.now(),quotes:active
      }))
    }catch(error){}
  }
  function persistItems(snapshot){
    if(location.protocol==="file:")return connectLocalDashboard("replace");
    persistQueue=persistQueue.catch(function(){}).then(async function(){
      var response=await fetch("/api/watchlist/items",{
        method:"POST",headers:{"Content-Type":"application/json","Accept":"application/json"},
        body:JSON.stringify({items:snapshot})
      });
      var payload=await response.json();
      if(!response.ok)throw new Error(payload.error||"保存自选股失败")
    });
    return persistQueue
  }
  function saveItems(options){
    saveItemsLocally();
    if(options&&options.localOnly)return Promise.resolve();
    return persistItems(items.map(function(item){return {market:item.market,code:item.code}}))
  }
  function mergeItems(primary,secondary){
    return normalizeItems((primary||[]).concat(secondary||[]))
  }
  async function initializePersistentItems(){
    if(location.protocol==="file:")return;
    try{
      var response=await fetch("/api/watchlist/items",{headers:{"Accept":"application/json"},cache:"no-store"});
      var payload=await response.json();
      if(!response.ok)throw new Error(payload.error||"读取自选股失败");
      if(importedShouldReplace){
        items=importedItems;
        await saveItems()
      }else if(payload.initialized){
        items=normalizeItems(payload.items)
      }else{
        items=mergeItems(importedItems,items);
        if(items.length)await saveItems()
      }
      saveItemsLocally()
    }catch(error){
      if(importedItems.length){items=mergeItems(importedItems,items);saveItemsLocally()}
      setFeedback("自选股持久化服务暂不可用："+error.message,true)
    }
  }
  function updateCount(){countNodes.forEach(function(node){node.textContent=String(items.length)})}
  function changeValue(result){
    if(!result||result.change_pct===null||result.change_pct==="")return null;
    var value=Number(result.change_pct);
    return Number.isFinite(value)?value:null
  }
  function orderedItems(){
    var rows=items.map(function(item,index){
      return {item:item,index:index,result:quoteCache[itemKey(item)]}
    });
    if(sortMode==="manual")return rows;
    var direction=sortMode==="change-desc"?-1:1;
    return rows.sort(function(left,right){
      var a=changeValue(left.result),b=changeValue(right.result);
      if(a===null&&b===null)return left.index-right.index;
      if(a===null)return 1;
      if(b===null)return -1;
      return a===b?left.index-right.index:(a-b)*direction
    })
  }
  function updateSortUI(){
    var state=sortMode==="change-desc"
      ?{sort:"descending",label:"涨跌幅排序：从高到低"}
      :sortMode==="change-asc"
        ?{sort:"ascending",label:"涨跌幅排序：从低到高"}
        :{sort:"none",label:"涨跌幅排序：默认顺序"};
    sortButton.setAttribute("data-sort-state",sortMode);
    sortButton.setAttribute("aria-label",state.label);
    sortButton.title=state.label+"；点击切换";
    sortHeader.setAttribute("aria-sort",state.sort)
  }
  function setFeedback(message,isError){
    feedback.textContent=message;
    feedback.classList.toggle("is-error",Boolean(isError));
  }
  async function connectLocalDashboard(importMode){
    setFeedback("正在连接本地行情服务…",false);
    try{
      await fetch(localDashboardOrigin+"/health",{mode:"no-cors",cache:"no-store"});
      var target=new URL(localDashboardOrigin+"/");
      target.searchParams.set("watchlist",JSON.stringify(items));
      if(importMode)target.searchParams.set("watchlistMode",importMode);
      target.hash="watchlist";
      location.replace(target.toString())
    }catch(error){
      setFeedback("未连接到本地行情服务；请先运行 run_dashboard.ps1 / run_dashboard.sh",true)
    }
  }
  function clearSuggestions(){
    searchSerial++;
    suggestions=[];activeSuggestion=-1;lastSearchQuery="";
    suggestionsBox.replaceChildren();suggestionsBox.hidden=true;
    code.setAttribute("aria-expanded","false")
  }
  function updateSuggestionState(){
    Array.prototype.forEach.call(suggestionsBox.children,function(node,index){
      node.classList.toggle("is-active",index===activeSuggestion);
      node.setAttribute("aria-selected",String(index===activeSuggestion))
    })
  }
  function renderSuggestions(results,queryKey){
    suggestions=Array.isArray(results)?results:[];
    activeSuggestion=suggestions.length?0:-1;
    lastSearchQuery=queryKey;
    suggestionsBox.replaceChildren();
    suggestions.forEach(function(result,index){
      var button=document.createElement("button");
      button.type="button";button.className="watchlist-suggestion";
      button.setAttribute("role","option");
      var name=document.createElement("strong");
      name.textContent=preferredDisplayName(result.market,result.code,result.name);
      var detail=document.createElement("small");
      detail.textContent=result.market_label+" · "+result.code+
        (result.search_alias?" · "+result.search_alias:"");
      var source=document.createElement("span");source.className="watchlist-suggestion-source";
      source.textContent=result.source||"";
      button.append(name,detail,source);
      button.addEventListener("mousedown",function(event){event.preventDefault()});
      button.addEventListener("click",function(){addItem(result)});
      suggestionsBox.appendChild(button)
    });
    suggestionsBox.hidden=!suggestions.length;
    code.setAttribute("aria-expanded",String(Boolean(suggestions.length)));
    updateSuggestionState()
  }
  async function searchStocks(raw,showEmpty){
    var query=String(raw||"").trim();
    var queryKey=market.value+":"+query.toLocaleLowerCase();
    if(!query){clearSuggestions();return []}
    if(location.protocol==="file:"){
      clearSuggestions();
      await connectLocalDashboard();
      return []
    }
    var serial=++searchSerial;
    try{
      var response=await fetch(
        "/api/watchlist/search?market="+encodeURIComponent(market.value)+
        "&q="+encodeURIComponent(query),
        {headers:{"Accept":"application/json"}}
      );
      var payload=await response.json();
      if(!response.ok)throw new Error(payload.error||"搜索接口请求失败");
      if(serial!==searchSerial)return [];
      renderSuggestions(payload.results||[],queryKey);
      if(showEmpty&&!(payload.results||[]).length)setFeedback("没有找到匹配股票，可检查市场或直接输入代码",true);
      return payload.results||[]
    }catch(error){
      if(serial!==searchSerial)return [];
      clearSuggestions();setFeedback("股票搜索失败："+error.message,true);
      return []
    }
  }
  function looksLikeCode(value,marketKey){
    var raw=String(value||"").trim().toUpperCase();
    if(marketKey==="A")return /^(SH|SZ|BJ)?\d{6}(\.(SH|SZ|BJ))?$/.test(raw);
    if(marketKey==="HK")return /^(HK)?\d{1,5}(\.HK)?$/.test(raw);
    if(marketKey==="US")return /^[A-Z][A-Z0-9.-]{0,14}(\.US)?$/.test(raw);
    return /^(KR)?\d{6}(\.(KS|KQ))?$/.test(raw)
  }
  function addItem(result){
    var next={market:String(result.market||market.value).toUpperCase(),code:String(result.code||"").toUpperCase()};
    if(!next.code){setFeedback("请选择有效的股票",true);return}
    if(items.some(function(item){return itemKey(item)===itemKey(next)})){
      setFeedback("这只股票已经在自选列表中",true);clearSuggestions();return
    }
    if(items.length>=30){setFeedback("自选股最多添加30只",true);return}
    items.push(next);
    saveItems().catch(function(error){setFeedback("添加成功，但持久化失败："+error.message,true)});
    code.value="";clearSuggestions();
    refresh()
  }
  function sparkGeometry(values,previousClose,progress){
    var clean=(values||[]).map(Number).filter(Number.isFinite);
    if(!clean.length)return {path:"",baselineY:20,endX:3,progress:0};
    var baseline=Number(previousClose);
    if(!Number.isFinite(baseline))baseline=clean[0];
    var scaleValues=clean.concat([baseline]);
    var low=Math.min.apply(null,scaleValues),high=Math.max.apply(null,scaleValues);
    var minimumSpan=Math.max(Math.abs(baseline),1)*0.001;
    if(high-low<minimumSpan){
      var middle=(high+low)/2;
      low=middle-minimumSpan/2;high=middle+minimumSpan/2
    }else{
      var padding=(high-low)*0.08;
      low-=padding;high+=padding
    }
    var span=high-low;
    var ratio=Number(progress);
    if(!Number.isFinite(ratio))ratio=1;
    ratio=Math.max(0,Math.min(1,ratio));
    var endX=3+114*ratio;
    var path=clean.map(function(value,index){
      var x=clean.length<2?3:3+index*(endX-3)/(clean.length-1);
      var y=3+(high-value)*34/span;
      return(index?"L":"M")+x.toFixed(1)+","+y.toFixed(1)
    }).join(" ");
    return {
      path:path,
      baselineY:3+(high-baseline)*34/span,
      endX:endX,
      progress:ratio
    }
  }
  function priceText(value){
    var number=Number(value);
    if(!Number.isFinite(number))return "待更新";
    var digits=Math.abs(number)>=1000?0:2;
    return number.toLocaleString("zh-CN",{minimumFractionDigits:digits,maximumFractionDigits:digits})
  }
  function volumeRatioText(value){
    var number=Number(value);
    return Number.isFinite(number)&&number>0?number.toFixed(2):"—"
  }
  function turnoverText(value){
    var number=Number(value);
    if(!Number.isFinite(number)||number<=0)return "—";
    var divisor=1,suffix="";
    if(number>=1e12){divisor=1e12;suffix="万亿"}
    else if(number>=1e8){divisor=1e8;suffix="亿"}
    else if(number>=1e4){divisor=1e4;suffix="万"}
    var scaled=number/divisor;
    var digits=scaled>=100?0:scaled>=10?1:2;
    return scaled.toFixed(digits)+suffix
  }
  function removeButton(key){
    var button=document.createElement("button");
    button.type="button";button.className="watchlist-remove";button.textContent="删除";
    button.addEventListener("click",async function(){
      items=items.filter(function(item){return itemKey(item)!==key});
      delete quoteCache[key];persistQuoteCache();render();
      try{
        await saveItems();
        if(items.length)await refresh();
        setFeedback("已永久删除；重新打开页面也不会恢复",false)
      }catch(error){
        setFeedback("删除已在当前页面生效，但持久化失败："+error.message,true)
      }
    });
    return button
  }
  function saveDraggedOrder(){
    var keys=Array.prototype.map.call(
      body.querySelectorAll("tr[data-watchlist-key]"),
      function(row){return row.getAttribute("data-watchlist-key")}
    );
    if(keys.length!==items.length)return;
    var byKey={};
    items.forEach(function(item){byKey[itemKey(item)]=item});
    if(keys.some(function(key){return !byKey[key]}))return;
    items=keys.map(function(key){return byKey[key]});
    saveItems().catch(function(error){setFeedback("排序已生效，但持久化失败："+error.message,true)});
    setFeedback("已保存自定义排序；行情仍会每分钟刷新",false)
  }
  var dragState=null,dragTimer=0;
  function activateDrag(){
    if(!dragState||dragState.active)return;
    dragState.active=true;
    dragState.row.classList.remove("is-pressing");
    dragState.row.classList.add("is-dragging");
    dragState.row.setAttribute("aria-grabbed","true");
    body.classList.add("is-reordering");
    setFeedback("拖动到目标位置后松开鼠标，即可保存顺序",false)
  }
  function finishDrag(event){
    if(!dragState||event&&event.pointerId!==dragState.pointerId)return;
    window.clearTimeout(dragTimer);
    var wasActive=dragState.active,row=dragState.row;
    row.classList.remove("is-pressing","is-dragging");
    row.setAttribute("aria-grabbed","false");
    body.classList.remove("is-reordering");
    dragState=null;
    if(wasActive){saveDraggedOrder();render()}
  }
  body.addEventListener("pointerdown",function(event){
    var row=event.target.closest("tr[data-watchlist-key]");
    if(
      sortMode!=="manual"||!row||event.button!==0||event.isPrimary===false||
      event.target.closest("button,input,select,a")
    )return;
    event.preventDefault();
    window.clearTimeout(dragTimer);
    dragState={
      row:row,pointerId:event.pointerId,
      startX:event.clientX,startY:event.clientY,active:false
    };
    row.classList.add("is-pressing");
    dragTimer=window.setTimeout(activateDrag,220)
  });
  document.addEventListener("pointermove",function(event){
    if(!dragState||event.pointerId!==dragState.pointerId)return;
    var distance=Math.hypot(
      event.clientX-dragState.startX,event.clientY-dragState.startY
    );
    if(!dragState.active&&distance>6)activateDrag();
    if(!dragState.active)return;
    event.preventDefault();
    var row=dragState.row;
    var target=document.elementFromPoint(event.clientX,event.clientY);
    target=target&&target.closest("tr[data-watchlist-key]");
    if(target&&target!==row&&target.parentNode===body){
      var rect=target.getBoundingClientRect();
      body.insertBefore(row,event.clientY<rect.top+rect.height/2?target:target.nextSibling)
    }
    var wrap=body.closest(".watchlist-table-wrap");
    if(wrap){
      var wrapRect=wrap.getBoundingClientRect();
      if(event.clientY<wrapRect.top+32)wrap.scrollTop-=10;
      else if(event.clientY>wrapRect.bottom-32)wrap.scrollTop+=10
    }
  },{passive:false});
  window.addEventListener("pointerup",finishDrag);
  window.addEventListener("pointercancel",finishDrag);
  function render(){
    body.replaceChildren();
    if(!items.length){
      var empty=document.createElement("tr"),cell=document.createElement("td");
      cell.colSpan=7;cell.className="hot-empty";cell.textContent="还没有自选股";
      empty.appendChild(cell);body.appendChild(empty);return
    }
    orderedItems().forEach(function(entry){
      var item=entry.item,result=entry.result,key=itemKey(item);
      var row=document.createElement("tr");
      row.classList.add("watchlist-row");
      row.setAttribute("data-watchlist-key",key);
      row.setAttribute("data-reorderable",String(sortMode==="manual"));
      if(!result||!result.ok){
        row.classList.add("watchlist-row-error");
        var identity=document.createElement("td");
        identity.textContent=(item.market||"")+" · "+(item.code||"");
        var errorCell=document.createElement("td");
        errorCell.colSpan=5;errorCell.textContent=(result&&result.error)||"行情待刷新";
        var action=document.createElement("td");action.className="watchlist-action";
        action.appendChild(removeButton(key));
        row.append(identity,errorCell,action);body.appendChild(row);return
      }
      var identityCell=document.createElement("td");identityCell.className="watchlist-identity";
      var displayName=preferredDisplayName(result.market,result.code,result.name);
      var name=document.createElement("strong");name.textContent=displayName;
      var meta=document.createElement("small");
      meta.textContent=result.market_label+" · "+result.code;
      identityCell.append(name,meta);
      var sparkCell=document.createElement("td");
      var svg=document.createElementNS("http://www.w3.org/2000/svg","svg");
      svg.setAttribute("viewBox","0 0 120 40");
      svg.setAttribute("class","watchlist-spark "+(Number(result.change_pct)>=0?"spark-up":"spark-down"));
      svg.setAttribute("role","img");svg.setAttribute("aria-label",displayName+"分时走势，虚线为0%涨幅");
      var geometry=sparkGeometry(result.sparkline,result.previous_close,result.sparkline_progress);
      svg.setAttribute("data-progress",geometry.progress.toFixed(3));
      var zeroLine=document.createElementNS("http://www.w3.org/2000/svg","line");
      zeroLine.setAttribute("class","watchlist-zero-line");
      zeroLine.setAttribute("x1","3");zeroLine.setAttribute("x2","117");
      zeroLine.setAttribute("y1",geometry.baselineY.toFixed(1));
      zeroLine.setAttribute("y2",geometry.baselineY.toFixed(1));
      var path=document.createElementNS("http://www.w3.org/2000/svg","path");
      path.setAttribute("d",geometry.path);svg.append(zeroLine,path);sparkCell.appendChild(svg);
      var priceCell=document.createElement("td");priceCell.className="watchlist-price";
      priceCell.textContent=priceText(result.price);
      var pctCell=document.createElement("td");
      var pct=Number(result.change_pct);
      pctCell.className=(pct>0?"rise":pct<0?"fall":"tone-neutral")+" hot-number";
      pctCell.textContent=Number.isFinite(pct)?(pct>=0?"+":"")+pct.toFixed(2)+"%":"待更新";
      var turnoverCell=document.createElement("td");
      turnoverCell.className="watchlist-turnover";
      turnoverCell.textContent=turnoverText(result.turnover);
      var volumeRatioCell=document.createElement("td");
      volumeRatioCell.className="watchlist-volume-ratio";
      volumeRatioCell.textContent=volumeRatioText(result.volume_ratio);
      volumeRatioCell.title="量比：当前每分钟均量 / 近5日平均每分钟量";
      var actionCell=document.createElement("td");actionCell.className="watchlist-action";
      actionCell.appendChild(removeButton(key));
      row.title="行情时间 "+(result.quote_time||"—");
      row.append(
        identityCell,sparkCell,priceCell,pctCell,
        turnoverCell,volumeRatioCell,actionCell
      );
      body.appendChild(row)
    })
  }
  async function refresh(){
    if(dragState||refreshInFlight)return;
    refreshInFlight=true;
    refreshButton.disabled=true;
    refreshButton.setAttribute("aria-busy","true");
    try{
    updateCount();
    if(!items.length){render();setFeedback("自选保存在本机浏览器；行情每分钟刷新",false);return}
    render();setFeedback("正在刷新自选股行情…",false);
    if(location.protocol==="file:"){
      await connectLocalDashboard();
      return
    }
    var requestItems=items.slice();
    try{
      var response=await fetch("/api/watchlist/quotes",{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({items:requestItems})
      });
      var payload=await response.json();
      if(!response.ok)throw new Error(payload.error||"行情接口请求失败");
      var results=payload.results||[];
      results.forEach(function(result,index){
        var requested=requestItems[index];
        if(!requested)return;
        result.name=preferredDisplayName(result.market,result.code,result.name);
        var requestedKey=itemKey(requested);
        if(result.ok){
          quoteCache[requestedKey]=result;
          var normalized={market:result.market,code:result.code};
          var currentIndex=items.findIndex(function(item){return itemKey(item)===requestedKey});
          if(currentIndex>=0){
            items[currentIndex]=normalized;
            var normalizedKey=itemKey(normalized);
            quoteCache[normalizedKey]=result;
            if(normalizedKey!==requestedKey)delete quoteCache[requestedKey]
          }
        }else if(!quoteCache[requestedKey]||!quoteCache[requestedKey].ok){
          quoteCache[requestedKey]=result
        }
      });
      var seen={};
      items=items.filter(function(item){var key=itemKey(item);if(seen[key])return false;seen[key]=true;return true});
      saveItems({localOnly:true});persistQuoteCache();render();
      var fallback=results.filter(function(result){return result.ok&&result.is_fallback}).length;
      var failed=results.filter(function(result){return !result.ok}).length;
      setFeedback(
        "更新于 "+(payload.updated_at||"刚刚")+
        (fallback?" · "+fallback+" 只使用备用行情源":"")+
        (failed?" · "+failed+" 只暂用上次行情":""),
        false
      )
    }catch(error){
      render();setFeedback("行情刷新失败："+error.message,true)
    }
    }finally{
      refreshInFlight=false;
      refreshButton.disabled=false;
      refreshButton.removeAttribute("aria-busy")
    }
  }
  refreshButton.addEventListener("click",refresh);
  panel.querySelectorAll("[data-hot-stock-tab]").forEach(function(button){
    button.addEventListener("click",function(){
      var key=button.getAttribute("data-hot-stock-tab");
      panel.querySelectorAll("[data-hot-stock-tab]").forEach(function(tab){tab.setAttribute("aria-selected",String(tab===button))});
      panel.querySelectorAll("[data-hot-stock-view]").forEach(function(view){view.hidden=view.getAttribute("data-hot-stock-view")!==key});
      if(key==="watchlist"&&initialSyncComplete)refresh()
    })
  });
  market.addEventListener("change",function(){
    code.placeholder=placeholders[market.value]||"";
    clearSuggestions();
    if(code.value.trim())searchStocks(code.value,false)
  });
  code.addEventListener("input",function(){
    window.clearTimeout(searchTimer);
    var query=code.value.trim();
    if(!query){clearSuggestions();return}
    searchTimer=window.setTimeout(function(){searchStocks(query,false)},220)
  });
  code.addEventListener("keydown",function(event){
    if(suggestionsBox.hidden||!suggestions.length)return;
    if(event.key==="ArrowDown"){
      event.preventDefault();activeSuggestion=(activeSuggestion+1)%suggestions.length;updateSuggestionState()
    }else if(event.key==="ArrowUp"){
      event.preventDefault();activeSuggestion=(activeSuggestion-1+suggestions.length)%suggestions.length;updateSuggestionState()
    }else if(event.key==="Enter"){
      event.preventDefault();addItem(suggestions[activeSuggestion>=0?activeSuggestion:0])
    }else if(event.key==="Escape"){
      event.preventDefault();clearSuggestions()
    }
  });
  code.addEventListener("blur",function(){
    window.setTimeout(function(){clearSuggestions()},120)
  });
  form.addEventListener("submit",async function(event){
    event.preventDefault();
    var raw=code.value.trim();
    if(!raw){setFeedback("请输入代码、名称、拼音首字母或英文名",true);code.focus();return}
    var queryKey=market.value+":"+raw.toLocaleLowerCase();
    if(suggestions.length&&lastSearchQuery===queryKey){
      addItem(suggestions[activeSuggestion>=0?activeSuggestion:0]);return
    }
    if(looksLikeCode(raw,market.value)){
      addItem({market:market.value,code:raw});return
    }
    setFeedback("正在搜索并添加…",false);
    var results=await searchStocks(raw,true);
    if(results.length)addItem(results[0])
  });
  sortButton.addEventListener("click",function(){
    sortMode=sortMode==="manual"
      ?"change-desc"
      :sortMode==="change-desc"?"change-asc":"manual";
    try{localStorage.setItem(sortStorageKey,sortMode)}catch(error){}
    updateSortUI();render();
    setFeedback(
      sortMode==="manual"?"已恢复默认顺序；可按住任意一行拖动"
        :sortMode==="change-desc"?"已按涨跌幅从高到低排序"
          :"已按涨跌幅从低到高排序",
      false
    )
  });
  updateSortUI();
  if(location.protocol==="file:"){
    updateCount();render();
    if(location.hash==="#watchlist"){
      var fileWatchlistTab=panel.querySelector('[data-hot-stock-tab="watchlist"]');
      if(fileWatchlistTab)fileWatchlistTab.click()
    }
  }else{
    countNodes.forEach(function(node){node.textContent="0"});
    Array.prototype.forEach.call(form.elements,function(element){element.disabled=true});
    body.innerHTML='<tr><td colspan="7" class="hot-empty">正在加载自选股…</td></tr>';
    initializePersistentItems().then(function(){
      initialSyncComplete=true;
      Array.prototype.forEach.call(form.elements,function(element){element.disabled=false});
      updateCount();render();
      if(location.hash==="#watchlist"){
        var watchlistTab=panel.querySelector('[data-hot-stock-tab="watchlist"]');
        if(watchlistTab)watchlistTab.click()
      }else if(items.length){refresh()}
    })
  }
  // 整张仪表盘会在下方统一每 60 秒重载；新页面初始化时已会刷新行情。
  // 不再额外启动同频定时器，避免旧页面临退出和新页面各发一批重复请求。
})();
</script>"""

DASHBOARD_REFRESH_SCRIPT = r"""<script>
(function(){
  var refreshIntervalMs=60000;
  var scrollStorageKey="market-sentiment-dashboard-scroll-v1";
  var scrollSelectors=[".hot-table-wrap",".watchlist-table-wrap"];
  if("scrollRestoration" in history)history.scrollRestoration="manual";

  function saveScrollState(){
    var elements=[];
    scrollSelectors.forEach(function(selector){
      document.querySelectorAll(selector).forEach(function(element,index){
        elements.push({
          selector:selector,index:index,
          top:element.scrollTop,left:element.scrollLeft
        })
      })
    });
    try{
      sessionStorage.setItem(scrollStorageKey,JSON.stringify({
        x:window.scrollX,y:window.scrollY,elements:elements
      }))
    }catch(error){}
  }

  function restoreScrollState(){
    var state=null;
    try{
      state=JSON.parse(sessionStorage.getItem(scrollStorageKey)||"null")
    }catch(error){}
    if(!state)return;
    var restore=function(){
      (state.elements||[]).forEach(function(item){
        var matches=document.querySelectorAll(item.selector);
        var element=matches[item.index];
        if(element){
          element.scrollLeft=Number(item.left)||0;
          element.scrollTop=Number(item.top)||0
        }
      });
      window.scrollTo(Number(state.x)||0,Number(state.y)||0)
    };
    window.requestAnimationFrame(function(){
      restore();
      window.requestAnimationFrame(restore)
    })
  }

  restoreScrollState();
  window.addEventListener("pagehide",saveScrollState);
  var refreshAt=Date.now()+refreshIntervalMs;
  document.documentElement.setAttribute(
    "data-dashboard-refresh-at",String(refreshAt)
  );
  window.setTimeout(function(){
    saveScrollState();
    var target=new URL(window.location.href);
    target.searchParams.set("_dashboardRefresh",String(Date.now()));
    window.location.replace(target.toString())
  },refreshIntervalMs)
})();
</script>"""


def score_meaning(value, metric):
    """把最新分数转换为面向读者的市场含义。"""
    if pd.isna(value):
        return "暂无有效数据，暂不能判断。"
    if metric == "breadth":
        meanings = [
            (25, "广泛走弱，多数股票承压，指数表现可能掩盖个股亏钱效应。"),
            (45, "多数股票偏弱，市场上涨参与度不足，行情扩散性有限。"),
            (60, "涨跌参与度相对均衡，市场尚未形成明确的全面扩散方向。"),
            (75, "多数股票参与上涨，行情扩散较好，市场内部结构偏强。"),
            (100, "市场呈全面走强状态，但广度处于高位时也需留意短期过热。"),
        ]
    else:
        meanings = [
            (25, "亏钱效应显著，追涨和接力交易的成功率处于低位。"),
            (45, "短线机会偏少，涨停接力、封板和次日溢价整体较弱。"),
            (60, "短线机会与风险相对均衡，赚钱效应缺乏明确方向。"),
            (75, "短线资金活跃，封板与接力表现较好，赚钱效应偏强。"),
            (100, "涨停溢价、晋级和封板表现处于近期高位，赚钱效应强，但追高风险同步上升。"),
        ]
    return next(text for upper, text in meanings if value <= upper)


def score_tone(value):
    """返回五档情绪颜色类：冷绿、中性金黄、热红，并区分深浅。"""
    if pd.isna(value):
        return "tone-missing"
    if value <= 25:
        return "tone-cold-deep"
    if value <= 45:
        return "tone-cold"
    if value <= 60:
        return "tone-neutral"
    if value <= 75:
        return "tone-hot"
    return "tone-hot-deep"


def overall_summary(last):
    """生成综合情绪的最新状态与驱动说明。"""
    available = [
        (name, float(last[key]))
        for key, (name, _) in COMPONENTS.items()
        if pd.notna(last[key])
    ]
    if not available or pd.isna(last.sentiment_score):
        return "当前可用分项不足，暂不能形成综合情绪判断。"
    strongest = max(available, key=lambda item: item[1])
    weakest = min(available, key=lambda item: item[1])
    summary = (
        f"综合情绪按量能30%、市场广度20%、短线赚钱效应20%、"
        f"资金风险偏好10%、市场动量20%加权。"
        f'最新综合分 <span class="tone {score_tone(last.sentiment_score)}">'
        f'{last.sentiment_score:.2f}</span>，状态为'
        f'<span class="tone {score_tone(last.sentiment_score)}">“{last.sentiment_state}”</span>；'
        f'市场阶段为 <span class="tone {score_tone(last.momentum_score)}">'
        f'“{last.market_phase}”</span>，结构情绪 {last.structural_score:.2f} 分，'
        f'当日动量 {last.momentum_score:.2f} 分；'
        f'最强分项是{strongest[0]}（<span class="tone {score_tone(strongest[1])}">'
        f'{strongest[1]:.2f}</span>），最弱分项是{weakest[0]}（'
        f'<span class="tone {score_tone(weakest[1])}">{weakest[1]:.2f}</span>）。'
        "分项差异越大，越说明市场热度集中在局部，而非全面一致。"
    )
    if last.get("data_status") == "盘中暂估":
        summary += " 当前为盘中暂估，资金风险偏好使用沪深指数分钟主力净流入代理，收盘后将由正式数据替换。"
    return summary


def render_dashboard(
    df,
    output="market_sentiment_dashboard.html",
    margin_file="market_margin_balance.csv",
    valuation_file=DEFAULT_VALUATION_CACHE,
):
    last, trend = df.iloc[-1], df.tail(120).reset_index(drop=True)
    is_intraday = last.get("data_status") == "盘中暂估"
    amount_max = float(trend.amount_yi.max()) * 1.08
    score_path = path_for(
        trend.sentiment_score, 100, height=500, top=26, bottom=62
    )
    score_extrema = extrema_annotations(
        trend.sentiment_score, 100, height=500, top=26, bottom=62,
        formatter=lambda value: f"{value:.1f}分",
    )
    score_clicks = line_click_points(
        trend.sentiment_score,
        trend.date,
        100,
        series_name="综合情绪",
        formatter=lambda value: f"{value:.2f}分",
        height=500,
        top=26,
        bottom=62,
    )
    score_dates = date_axis_labels(trend.date, y=475)
    ma_path = path_for(trend.ma20_yi, amount_max, height=290)
    previous_amount = pd.to_numeric(df.amount_yi, errors="coerce").shift(1).tail(120)
    amount_bars = turnover_bar_chart(
        trend.amount_yi,
        previous_amount.reset_index(drop=True),
        trend.date,
        amount_max,
    )
    amount_ma_clicks = line_click_points(
        trend.ma20_yi,
        trend.date,
        amount_max,
        series_name="20日均额",
        formatter=lambda value: f"{value / 10000:.2f}万亿元",
        css_class="chart-hit-mean",
        height=290,
    )
    amount_extrema = extrema_annotations(
        trend.amount_yi, amount_max, height=290,
        formatter=lambda value: f"{value / 10000:.2f}万亿",
        css_class="extrema-amount", prefix="成交额",
    )
    amount_dates = date_axis_labels(trend.date)
    margin_path = Path(margin_file)
    if margin_path.exists():
        capital = pd.read_csv(margin_path, parse_dates=["date"])
    else:
        capital = df[["date", "hs_margin_balance_yi"]].copy()
    capital = capital.dropna(subset=["hs_margin_balance_yi"]).sort_values("date")
    capital["hs_margin_balance_ma20_yi"] = (
        capital.hs_margin_balance_yi.rolling(20, min_periods=10).mean()
    )
    if not capital.empty:
        capital_cutoff = capital.date.max() - pd.DateOffset(years=2)
        capital = capital[capital.date >= capital_cutoff].reset_index(drop=True)
    if capital.empty:
        capital_min, capital_mid, capital_max = 0.0, 0.5, 1.0
        capital_path, capital_ma20_path, capital_dates = "", "", ""
        capital_extrema = ""
        capital_clicks, capital_ma20_clicks = "", ""
        capital_note = "暂无沪深两市融资余额数据。"
    else:
        capital_low = float(
            capital[["hs_margin_balance_yi", "hs_margin_balance_ma20_yi"]].min().min()
        )
        capital_high = float(
            capital[["hs_margin_balance_yi", "hs_margin_balance_ma20_yi"]].max().max()
        )
        capital_pad = max((capital_high - capital_low) * 0.08, capital_high * 0.005)
        capital_min = max(0, capital_low - capital_pad)
        capital_max = capital_high + capital_pad
        capital_mid = (capital_min + capital_max) / 2
        capital_path = path_for(
            capital.hs_margin_balance_yi, capital_max, height=290, minimum=capital_min
        )
        capital_ma20_path = path_for(
            capital.hs_margin_balance_ma20_yi, capital_max, height=290, minimum=capital_min
        )
        capital_extrema = extrema_annotations(
            capital.hs_margin_balance_yi,
            capital_max,
            height=290,
            minimum=capital_min,
            formatter=lambda value: f"{value:,.0f}亿",
            css_class="extrema-amount",
            prefix="余额",
        )
        capital_clicks = line_click_points(
            capital.hs_margin_balance_yi,
            capital.date,
            capital_max,
            series_name="沪深两市融资余额",
            formatter=lambda value: f"{value:,.2f}亿元",
            css_class="chart-hit-amount",
            height=290,
            minimum=capital_min,
        )
        capital_ma20_clicks = line_click_points(
            capital.hs_margin_balance_ma20_yi,
            capital.date,
            capital_max,
            series_name="融资余额20日均线",
            formatter=lambda value: f"{value:,.2f}亿元",
            css_class="chart-hit-mean",
            height=290,
            minimum=capital_min,
        )
        capital_dates = date_axis_labels(capital.date)
        capital_note = (
            "纵轴：沪深两市融资余额合计（亿元）· 最近两年 · "
            f"为突出趋势，纵轴采用局部缩放；最新余额 {capital.hs_margin_balance_yi.iloc[-1]:,.2f} 亿元，"
            f"20日均值 {capital.hs_margin_balance_ma20_yi.iloc[-1]:,.2f} 亿元。"
            "点击折线数据点可查看对应交易日数值。"
        )
    breadth_trend = df.dropna(subset=["breadth_score"]).tail(120).reset_index(drop=True)
    short_trend = df.dropna(subset=["short_term_score"]).tail(120).reset_index(drop=True)
    breadth_path = path_for(breadth_trend.breadth_score, 100, height=290)
    short_path = path_for(short_trend.short_term_score, 100, height=290)
    breadth_extrema = extrema_annotations(
        breadth_trend.breadth_score, 100, height=290,
        formatter=lambda value: f"{value:.1f}分",
    )
    short_extrema = extrema_annotations(
        short_trend.short_term_score, 100, height=290,
        formatter=lambda value: f"{value:.1f}分",
        css_class="extrema-mean",
    )
    breadth_clicks = line_click_points(
        breadth_trend.breadth_score,
        breadth_trend.date,
        100,
        series_name="市场广度",
        formatter=lambda value: f"{value:.2f}分",
        height=290,
    )
    short_clicks = line_click_points(
        short_trend.short_term_score,
        short_trend.date,
        100,
        series_name="短线赚钱效应",
        formatter=lambda value: f"{value:.2f}分",
        css_class="chart-hit-mean",
        height=290,
    )
    breadth_dates = date_axis_labels(breadth_trend.date)
    short_dates = date_axis_labels(short_trend.date)
    breadth_value = pd.NA if breadth_trend.empty else breadth_trend.breadth_score.iloc[-1]
    short_value = pd.NA if short_trend.empty else short_trend.short_term_score.iloc[-1]
    breadth_latest = (
        "暂无数据" if pd.isna(breadth_value)
        else f'最新 <span class="tone {score_tone(breadth_value)}">{breadth_value:.2f} 分</span>：'
             f"{score_meaning(breadth_value, 'breadth')}"
    )
    short_latest = (
        "暂无数据" if pd.isna(short_value)
        else f'最新 <span class="tone {score_tone(short_value)}">{short_value:.2f} 分</span>：'
             f"{score_meaning(short_value, 'short_term')}"
    )
    component_html = []
    for key, (name, weight) in COMPONENTS.items():
        value = last[key]
        score = "待接入" if pd.isna(value) else f"{value:.0f}"
        bar = 0 if pd.isna(value) else float(value)
        definition = COMPONENT_DEFINITIONS[key]
        if is_intraday and key == "capital_score":
            definition = (
                "盘中代理数据：沪指与深成指分钟主力净流入合计。量化：净流入占实时成交额"
                "比例在-5%至+5%映射为0–100分；盘后改用融资余额日变化。"
            )
        if key == "liquidity_score":
            liquidity_parts = [
                ("绝对成交额分位", last.get("liquidity_level_score")),
                ("20日相对强度", last.get("liquidity_relative_score")),
                ("5日成交加速度", last.get("liquidity_acceleration_score")),
            ]
            valid_parts = [
                f"{name}{float(part):.1f}"
                for name, part in liquidity_parts
                if pd.notna(pd.to_numeric(part, errors="coerce"))
            ]
            if valid_parts:
                definition += " 最新分解：" + "、".join(valid_parts) + "。"
        if key == "momentum_score":
            momentum_parts = [
                ("平均涨幅", last.get("mean_return_pct_score")),
                ("中位涨幅", last.get("median_return_pct_score")),
                ("强弱占比差", last.get("strong_net_share_pct_score")),
                ("五大指数", last.get("index_equal_weight_return_pct_score")),
            ]
            valid_parts = [
                f"{name}{float(part):.1f}"
                for name, part in momentum_parts
                if pd.notna(pd.to_numeric(part, errors="coerce"))
            ]
            if valid_parts:
                definition += " 最新分解：" + "、".join(valid_parts) + "。"
        component_html.append(
            f'<div class="component"><b>{name}</b>'
            f'<strong class="tone {score_tone(value)}">{score}</strong>'
            f'<div class="bar"><i style="width:{bar:.1f}%"></i></div>'
            f'<small>综合权重 {weight}%</small>'
            f'<small class="definition">{definition}</small></div>'
        )
    rows = "".join(
        f"<tr><td>{r.date:%Y-%m-%d}{'（盘中）' if r.get('data_status') == '盘中暂估' else ''}</td>"
        f"<td>{r.amount_yi/10000:.2f}万亿</td>"
        f"<td>{r.vs_ma20:.3f}×</td><td>"
        f'<span class="tone {score_tone(r.sentiment_score)}">'
        f"{'—' if pd.isna(r.sentiment_score) else f'{r.sentiment_score:.2f}'}</span></td>"
        f'<td><span class="tone {score_tone(r.sentiment_score)}">'
        f"{html.escape(str(r.sentiment_state))}</span></td>"
        f'<td><span class="tone {score_tone(r.momentum_score)}">'
        f"{html.escape(str(r.market_phase))}</span></td></tr>"
        for _, r in df.tail(30).iloc[::-1].iterrows())
    score = "—" if pd.isna(last.sentiment_score) else f"{last.sentiment_score:.0f}"
    index_market_html = ""
    market_distribution_html = ""
    industry_heatmap_html = ""
    hot_rankings_html = ""
    if is_intraday:
        subtitle = (
            f"盘中暂估：{last.date:%Y-%m-%d} · 更新时间 {last.snapshot_time} · "
            "涨跌分布覆盖沪深北A股；成交额与综合情绪沿用沪深A股口径"
        )
        amount_value = f"{last.actual_amount_yi/10000:.2f}万亿"
        actual_amount_time = str(last.get("actual_amount_time") or "")
        actual_time_prefix = (
            f"实时报价截至 {html.escape(actual_amount_time)} · "
            if actual_amount_time and actual_amount_time.lower() != "nan" else ""
        )
        projection_method = str(
            last.get("turnover_projection_method") or "昨日同一时点成交进度"
        )
        comparison_warning = last.get("turnover_comparison_warning")
        comparison_degraded = (
            pd.notna(comparison_warning)
            and str(comparison_warning).strip().lower() not in ("", "nan")
        )
        degraded_note = " · 分钟数据网络降级" if comparison_degraded else ""
        amount_detail = (
            f"{actual_time_prefix}按{html.escape(projection_method)}预估全天 "
            f"{last.projected_amount_yi/10000:.2f}万亿{degraded_note}"
        )
        delta_yi = pd.to_numeric(last.get("same_time_amount_delta_yi"), errors="coerce")
        delta_pct = pd.to_numeric(last.get("same_time_amount_delta_pct"), errors="coerce")
        previous_yi = pd.to_numeric(
            last.get("previous_same_time_amount_yi"), errors="coerce"
        )
        if pd.notna(delta_yi) and pd.notna(delta_pct) and pd.notna(previous_yi):
            delta_class = "volume-up" if delta_yi >= 0 else "volume-down"
            delta_text = (
                f"{delta_yi / 10000:+.2f}万亿"
                if abs(delta_yi) >= 1000 else f"{delta_yi:+.0f}亿元"
            )
            amount_comparison = (
                f'<small class="amount-compare {delta_class}">'
                f"较{'估算的' if comparison_degraded else ''}上一交易日 "
                f"{last.comparison_previous_date} "
                f"{last.comparison_time}（{previous_yi / 10000:.2f}万亿）"
                f"{last.same_time_volume_state} {delta_text}（{delta_pct:+.1f}%）</small>"
            )
        else:
            amount_comparison = (
                '<small class="amount-compare">上一交易日同时间成交额暂不可用</small>'
            )
        proxy_class = score_tone(last.capital_score)
        proxy_card = (
            '<div class="card"><small>主力资金实时净流入</small>'
            f'<div class="value tone {proxy_class}">{last.main_net_yi:+.2f}亿</div>'
            f"<small>占实时成交额 {last.main_net_ratio_pct:+.2f}% · 沪深指数分钟汇总</small></div>"
        )
        try:
            index_snapshot = json.loads(last.get("index_snapshot_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            index_snapshot = []
        index_cards = []
        index_times = []
        for item in index_snapshot:
            change_pct = float(item["change_pct"])
            direction = "up" if change_pct > 0 else "down" if change_pct < 0 else "flat"
            direction_text = "上涨" if change_pct > 0 else "下跌" if change_pct < 0 else "平盘"
            spark_path = sparkline_path(item.get("sparkline") or [])
            quote_time = str(item.get("quote_time") or "")
            if len(quote_time) >= 14:
                index_times.append(
                    f"{quote_time[8:10]}:{quote_time[10:12]}:{quote_time[12:14]}"
                )
            index_cards.append(
                f'<article class="index-card market-{direction}" '
                f'aria-label="{html.escape(item["name"])}{direction_text}{abs(change_pct):.2f}%">'
                f'<div class="index-value">{float(item["value"]):.2f}</div>'
                f'<div class="index-meta"><span>{html.escape(item["name"])}</span>'
                f'<b>{change_pct:+.2f}%</b></div>'
                f'<svg class="index-spark" viewBox="0 0 180 58" role="img" '
                f'aria-label="{html.escape(item["name"])}盘中走势">'
                f'<path d="{spark_path}"/></svg></article>'
            )
        index_time = min(index_times) if index_times else "—"
        index_market_html = (
            '<section class="index-market"><div class="section-head">'
            '<h2>盘中大盘行情</h2>'
            f"<small>腾讯实时指数 · 行情时间 {index_time} · 页面每60秒自动刷新</small>"
            f'</div><div class="index-grid">{"".join(index_cards)}</div></section>'
        )
        try:
            distribution = json.loads(last.get("change_distribution_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            distribution = []
        maximum_count = max((int(item["count"]) for item in distribution), default=1)
        distribution_bars = []
        for item in distribution:
            count = int(item["count"])
            relative_count = count / max(maximum_count, 1)
            # 0.72次幂兼顾中段可读性和低数量压缩；小于50家的柱保持接近底部。
            height = 3 + 129 * relative_count ** 0.72
            # 数量越大颜色越实，数量越少越透明；文字标签始终保持清晰。
            opacity = 0.30 + 0.70 * relative_count ** 0.55
            distribution_bars.append(
                f'<div class="distribution-bin bin-{item["side"]}">'
                f"<b>{count:,}</b><i style=\"height:{height:.1f}px;"
                f"opacity:{opacity:.3f}\"></i>"
                f'<span>{html.escape(item["label"])}</span></div>'
            )
        main_flow_class = "volume-up" if last.main_net_yi >= 0 else "volume-down"
        distribution_limit_up = int(
            pd.to_numeric(last.get("distribution_limit_up_count"), errors="coerce")
            if pd.notna(last.get("distribution_limit_up_count"))
            else last.limit_up_count
        )
        distribution_limit_down = int(
            pd.to_numeric(last.get("distribution_limit_down_count"), errors="coerce")
            if pd.notna(last.get("distribution_limit_down_count"))
            else last.limit_down_count
        )
        distribution_ratio = pd.to_numeric(
            last.get("distribution_limit_up_down_ratio"), errors="coerce"
        )
        if pd.isna(distribution_ratio):
            distribution_ratio = pd.to_numeric(
                last.get("limit_up_down_ratio"), errors="coerce"
            )
        limit_ratio = (
            f"{distribution_ratio:.1f}倍"
            if pd.notna(distribution_ratio)
            else ("无跌停" if distribution_limit_up else "—")
        )
        distribution_total = max(
            int(pd.to_numeric(
                last.get("distribution_stock_count"), errors="coerce"
            ))
            if pd.notna(last.get("distribution_stock_count"))
            else int(last.valid_stock_count),
            1,
        )
        distribution_up = (
            int(last.distribution_up_count)
            if pd.notna(last.get("distribution_up_count")) else int(last.up_count)
        )
        distribution_down = (
            int(last.distribution_down_count)
            if pd.notna(last.get("distribution_down_count")) else int(last.down_count)
        )
        distribution_no_trade = (
            int(last.distribution_no_trade_count)
            if pd.notna(last.get("distribution_no_trade_count")) else 0
        )
        no_trade_note = (
            f" · 另有 {distribution_no_trade:,} 只停牌/零成交未计入"
            if distribution_no_trade else ""
        )
        rise_pct = distribution_up / distribution_total * 100
        flat_pct = int(last.flat_count) / distribution_total * 100
        fall_pct = distribution_down / distribution_total * 100
        try:
            dragon_tiger = json.loads(
                last.get("dragon_tiger_top5_json") or "{}"
            )
        except (TypeError, json.JSONDecodeError):
            dragon_tiger = {}
        dragon_tiger_stocks = (dragon_tiger.get("stocks") or [])[:5]
        dragon_tiger_rows = []
        for rank, item in enumerate(dragon_tiger_stocks, 1):
            change_pct = float(item.get("change_pct") or 0)
            net_amount_yi = float(item.get("net_amount_yi") or 0)
            change_class = "rise" if change_pct >= 0 else "fall"
            net_class = "volume-up" if net_amount_yi >= 0 else "volume-down"
            net_label = "流入" if net_amount_yi >= 0 else "流出"
            dragon_tiger_rows.append(
                f'<li><span class="dragon-tiger-name"><i>{rank}</i>'
                f'{html.escape(str(item.get("name") or item.get("code") or "—"))}</span>'
                f'<span class="{change_class}">{change_pct:+.2f}%</span>'
                f'<span class="{net_class}">{net_label} {abs(net_amount_yi):.2f}亿</span></li>'
            )
        dragon_tiger_date = html.escape(str(dragon_tiger.get("date") or "日期待更新"))
        dragon_tiger_status = html.escape(
            str(dragon_tiger.get("status") or "最近已发布")
        )
        dragon_tiger_checked_at = str(dragon_tiger.get("checked_at") or "")
        dragon_tiger_checked_text = (
            f" · {html.escape(dragon_tiger_checked_at[-8:-3])} 已检查"
            if len(dragon_tiger_checked_at) >= 16 else ""
        )
        dragon_tiger_body = (
            '<div class="dragon-tiger-columns"><span>股票</span>'
            '<span>涨跌幅</span><span>资金净额</span></div>'
            f'<ol class="dragon-tiger-list">{"".join(dragon_tiger_rows)}</ol>'
            if dragon_tiger_rows else
            '<div class="dragon-tiger-empty">最近榜单暂不可用</div>'
        )
        dragon_tiger_card = (
            '<article class="dragon-tiger-card"><small>龙虎榜净买额 TOP5</small>'
            f'<small class="dragon-tiger-date">{dragon_tiger_date} · '
            f'{dragon_tiger_status}{dragon_tiger_checked_text}</small>'
            f'{dragon_tiger_body}</article>'
        )
        market_distribution_html = (
            '<section class="distribution-market"><div class="section-head">'
            '<h2>盘中涨跌停分布</h2>'
            f"<small>沪深北A股当日有成交 {distribution_total:,} 只"
            f"{no_trade_note} · "
            f"行情时间 {last.comparison_time} · 柱高与透明度按数量映射</small></div>"
            f'<div class="distribution-bars">{"".join(distribution_bars)}</div>'
            '<div class="distribution-progress">'
            '<div class="distribution-progress-labels">'
            f'<b class="rise">涨 {distribution_up:,} · {rise_pct:.1f}%</b>'
            f'<span>平 {int(last.flat_count):,} · {flat_pct:.1f}%</span>'
            f'<b class="fall">跌 {distribution_down:,} · {fall_pct:.1f}%</b></div>'
            f'<div class="distribution-track" role="img" aria-label="上涨{rise_pct:.1f}%，'
            f'平盘{flat_pct:.1f}%，下跌{fall_pct:.1f}%">'
            f'<i class="progress-rise" style="width:{rise_pct:.4f}%"></i>'
            f'<i class="progress-flat" style="width:{flat_pct:.4f}%"></i>'
            f'<i class="progress-fall" style="width:{fall_pct:.4f}%"></i>'
            "</div></div>"
            '<div class="market-kpis">'
            '<article><small>今日累计成交额</small>'
            f'<div>{last.actual_amount_yi / 10000:.2f}万亿</div>'
            f"<small>截至 {last.comparison_time}</small>"
            f"{amount_comparison}</article>"
            '<article><small>主力实时净流入</small>'
            f'<div class="{main_flow_class}">{last.main_net_yi:+.2f}亿</div>'
            f"<small>占成交额 {last.main_net_ratio_pct:+.2f}%</small></article>"
            '<article><small>涨停 / 跌停比</small>'
            f'<div><span class="rise">{distribution_limit_up}</span> : '
            f'<span class="fall">{distribution_limit_down}</span></div>'
            f"<small>涨停 : 跌停 · {limit_ratio}</small></article>"
            f"{dragon_tiger_card}"
            "</div></section>"
        )
        try:
            industry_heatmap = json.loads(last.get("industry_heatmap_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            industry_heatmap = {}
        top_industries = industry_heatmap.get("top") or []
        bottom_industries = industry_heatmap.get("bottom") or []
        if top_industries and bottom_industries:
            top_rectangles = industry_treemap(top_industries)
            bottom_rectangles = industry_treemap(bottom_industries)
            industry_source = str(industry_heatmap.get("source") or "东方财富")
            fallback_text = (
                " · 同花顺异常，已自动降级"
                if industry_heatmap.get("fallback") else ""
            )
            industry_heatmap_html = (
                '<section class="industry-market"><div class="section-head">'
                '<h2>今日热门行业涨跌热力图</h2>'
                f'<small>{html.escape(industry_source)}行业板块 '
                f'{int(industry_heatmap.get("total") or 0):,} 个{fallback_text} · '
                "有序Binary布局 · 高涨幅组优先位于左侧/上侧 · "
                "面积=成交额Min-Max映射（最小面积保护） · "
                "0–5%每0.5%线性色阶，|涨跌幅|≥5%完全不透明 · "
                "窄块悬停查看</small></div>"
                '<div class="industry-block"><h3><span class="heat-up-dot"></span>'
                '涨幅排行</h3><div class="industry-heatmap">'
                f'{"".join(industry_tile_html(item, rect[1:]) for rect in top_rectangles for item in [rect[0]])}'
                "</div></div>"
                '<div class="industry-block"><h3><span class="heat-down-dot"></span>'
                '跌幅排行</h3><div class="industry-heatmap">'
                f'{"".join(industry_tile_html(item, rect[1:]) for rect in bottom_rectangles for item in [rect[0]])}'
                "</div></div></section>"
            )
        try:
            hot_rankings = json.loads(last.get("ths_hot_rankings_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            hot_rankings = {}
        stocks = (hot_rankings.get("stocks") or [])[:30]
        concepts = (hot_rankings.get("concepts") or [])[:30]
        industries = (hot_rankings.get("industries") or [])[:30]
        indices = (hot_rankings.get("indices") or [])[:30]
        errors = hot_rankings.get("errors") or []
        if stocks or concepts or industries or indices or errors:
            error_text = (
                " · " + html.escape("；".join(errors))
                if errors else ""
            )
            hot_rankings_html = (
                '<section class="hot-rankings"><div class="section-head">'
                '<h2>同花顺热榜</h2>'
                f'<small>1小时热度 · 最多30名 · 更新 '
                f'{html.escape(str(hot_rankings.get("updated_at") or last.snapshot_time))}'
                f"{error_text}</small></div>"
                '<div class="hot-rank-grid">'
                f"{hot_stock_panel(stocks)}"
                f"{hot_plate_panel(concepts, industries, indices)}"
                "</div><p class=\"note\">热度为同花顺用户关注度指标；涨跌幅为榜单接口实时值。"
                "“上榜原因”优先展示同花顺分析标题，其次展示人气/概念标签；"
                "股票榜新增自选股页签，可手动添加A股、港股、美股和韩股；"
                "自选行情优先取同花顺，公开端点未覆盖或暂不可用时自动切换备用源。"
                "概念、行业、指数三个板块榜可点击页签切换；行业榜展示连续上榜、涨停家数及关联ETF。"
                "各板块榜按接口实际返回数量展示，不补造缺失名次。</p></section>"
            )
    else:
        subtitle = f"最新完整交易日：{last.date:%Y-%m-%d} · 沪深A股（不含北交所）"
        amount_value = f"{last.amount_yi/10000:.2f}万亿"
        amount_detail = f"20日均额 {last.ma20_yi/10000:.2f}万亿"
        amount_comparison = ""
        proxy_card = ""
    source_note = (
        "盘中数据源：腾讯财经沪深北A股实时行情、东财沪深指数分钟资金流、"
        "东财最近已发布交易日龙虎榜、"
        "同花顺行业板块排名（异常时自动降级为东财）、"
        "同花顺1小时热股及概念/行业/指数板块热榜；"
        "成交额按上一交易日同一时点的成交进度投影，所有盘中分数均为暂估。"
        if is_intraday else
        "数据源：上交所每日股票成交概况与融资融券汇总、深交所市场总貌、"
        "腾讯财经沪深A股前复权日K。"
    )
    summary = overall_summary(last)
    valuation_html = market_valuation_section(valuation_file)
    doc = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>A股市场情绪仪表盘</title><style>
body{{margin:0;background:#f6f8fb;color:#182b49;font:14px -apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif}}main{{max-width:1140px;margin:auto;padding:28px 20px 48px}}h1{{margin:0;font-size:27px}}.sub,small,.note{{color:#68778d}}.cards,.grid{{display:grid;gap:14px}}.cards{{grid-template-columns:repeat(auto-fit,minmax(190px,1fr));margin:14px 0}}.grid{{grid-template-columns:2fr 1fr}}.trend-grid{{grid-template-columns:1fr;margin:14px 0}}.trend-grid>section:first-child{{grid-column:auto}}.card,section{{box-sizing:border-box;background:#fff;border:1px solid #e6edf5;border-radius:12px;padding:18px}}.value{{font-size:30px;font-weight:700;margin:7px 0}}h2{{font-size:16px;margin:0 0 12px}}svg{{width:100%;display:block}}.axis{{stroke:#dfe7f0}}.score{{fill:none;stroke:#5a4fcf;stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}}.score-halo{{fill:none;stroke:#fff;stroke-width:6;stroke-linecap:round;stroke-linejoin:round;opacity:.78}}.sentiment-band-cold-deep{{fill:#087f5b;opacity:.16}}.sentiment-band-cold{{fill:#3b9b63;opacity:.12}}.sentiment-band-neutral{{fill:#f4c95d;opacity:.20}}.sentiment-band-hot{{fill:#df655f;opacity:.12}}.sentiment-band-hot-deep{{fill:#b42318;opacity:.16}}.sentiment-band-label{{font-size:12px;font-weight:700;text-anchor:end;opacity:.78}}.amount{{fill:none;stroke:#2869b2;stroke-width:2}}.mean{{fill:none;stroke:#ec8b00;stroke-width:2}}.tone{{font-weight:700}}.tone-cold-deep{{color:#087f5b}}.tone-cold{{color:#3b9b63}}.tone-neutral{{color:#b47b00}}.tone-hot{{color:#df655f}}.tone-hot-deep{{color:#b42318}}.tone-missing{{color:#8a96a8}}.amount-compare{{display:block;margin-top:7px;font-weight:700;line-height:1.45}}.volume-up{{color:#b42318}}.volume-down{{color:#087f5b}}.rise{{color:#e5484d}}.fall{{color:#19a665}}.index-market,.distribution-market,.industry-market,.hot-rankings{{margin-top:20px}}.section-head{{display:flex;align-items:baseline;justify-content:space-between;gap:12px}}.section-head h2{{margin-bottom:12px}}.index-grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}}.index-card{{box-sizing:border-box;border:1px solid transparent;border-radius:10px;padding:15px 14px 10px;min-width:0}}.index-value{{font-size:24px;font-weight:750;line-height:1.1;font-variant-numeric:tabular-nums}}.index-meta{{display:flex;align-items:baseline;justify-content:space-between;gap:6px;margin-top:7px;white-space:nowrap}}.index-meta span{{color:#223047}}.index-meta b{{font-size:13px}}.index-spark{{height:58px;margin-top:10px}}.index-spark path{{fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}}.market-up{{color:#e5484d;background:#fff5f5;border-color:#ffe3e3}}.market-down{{color:#19a665;background:#f2fbf7;border-color:#dff5e8}}.market-flat{{color:#b47b00;background:#fff9e8;border-color:#f9edbd}}.distribution-bars{{height:180px;display:grid;grid-template-columns:repeat(11,minmax(0,1fr));gap:7px;align-items:end;border-bottom:1px solid #dfe7f0;padding:0 5px}}.distribution-bin{{height:175px;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;min-width:0}}.distribution-bin b{{font-size:12px;font-variant-numeric:tabular-nums;margin-bottom:5px}}.distribution-bin i{{display:block;width:min(34px,75%);min-height:4px;border-radius:6px 6px 2px 2px}}.distribution-bin span{{height:27px;margin-top:6px;font-size:11px;color:#68778d;white-space:nowrap}}.bin-up b{{color:#e5484d}}.bin-up i{{background:#e5484d}}.bin-down b{{color:#19a665}}.bin-down i{{background:#19a665}}.bin-flat b{{color:#8a96a8}}.bin-flat i{{background:#aeb8c5}}.distribution-progress{{margin-top:13px}}.distribution-progress-labels{{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:14px;font-variant-numeric:tabular-nums}}.distribution-progress-labels span{{color:#68778d}}.distribution-track{{display:flex;width:100%;height:9px;margin-top:8px;background:#edf1f6;border-radius:7px;overflow:hidden}}.distribution-track i{{display:block;height:100%;min-width:1px}}.progress-rise{{background:#e5484d}}.progress-flat{{background:#aeb8c5}}.progress-fall{{background:#19a665}}.market-kpis{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:16px}}.market-kpis article{{border:1px solid #e6edf5;border-radius:10px;padding:14px;min-width:0;background:#fbfcfe}}.market-kpis article>div{{font-size:22px;font-weight:750;margin:8px 0;font-variant-numeric:tabular-nums}}.market-kpis article>small:last-child{{display:block;line-height:1.45}}.dragon-tiger-date{{display:block;margin-top:3px;font-size:11px}}.market-kpis .dragon-tiger-columns{{display:grid;grid-template-columns:minmax(0,1fr) 52px 78px;gap:5px;margin:10px 0 2px;font-size:10px;font-weight:500;color:#8a96a8}}.dragon-tiger-columns span:nth-child(n+2){{text-align:right}}.dragon-tiger-list{{list-style:none;margin:0;padding:0}}.dragon-tiger-list li{{display:grid;grid-template-columns:minmax(0,1fr) 52px 78px;align-items:center;gap:5px;padding:6px 0;border-top:1px solid #edf1f6;font-size:11px;font-variant-numeric:tabular-nums}}.dragon-tiger-list li>span:nth-child(n+2){{text-align:right;white-space:nowrap;font-weight:700}}.dragon-tiger-name{{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:700;color:#223047}}.dragon-tiger-name i{{display:inline-grid;place-items:center;width:16px;height:16px;margin-right:4px;border-radius:4px;background:#edf1f6;color:#68778d;font-size:9px;font-style:normal}}.market-kpis .dragon-tiger-empty{{margin:18px 0 0;font-size:13px;font-weight:500;color:#8a96a8}}.industry-block+ .industry-block{{margin-top:18px}}.industry-block h3{{display:flex;align-items:center;gap:7px;margin:4px 0 10px;font-size:14px}}.industry-block h3 span{{width:9px;height:9px;border-radius:50%}}.heat-up-dot{{background:#e5484d}}.heat-down-dot{{background:#19a665}}.industry-heatmap{{position:relative;height:300px;border:1px solid #edf1f6;border-radius:9px;overflow:hidden;background:#f3f6fa}}.industry-tile{{position:absolute;box-sizing:border-box;display:flex;flex-direction:column;justify-content:center;border:2px solid #fff;border-radius:7px;padding:10px;text-align:center;overflow:hidden;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.55);container-type:size;transition:filter .15s ease,transform .15s ease}}.industry-tile:hover{{z-index:2;filter:saturate(1.1) brightness(.96);box-shadow:0 4px 14px rgba(24,43,73,.18)}}.industry-tile-label{{display:flex;min-width:0;flex-direction:column;align-items:center;justify-content:center}}.industry-tile b{{max-width:100%;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.industry-tile strong{{font-size:19px;margin-top:7px;font-variant-numeric:tabular-nums}}@container (max-width:70px){{.industry-tile b{{font-size:11px}}.industry-tile strong{{font-size:14px;margin-top:4px}}}}@container (max-width:45px){{.industry-tile-label{{display:none}}}}@container (max-height:32px){{.industry-tile-label{{display:none}}}}.hot-rank-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}.hot-panel{{min-width:0;border:1px solid #e6edf5;border-radius:10px;background:#fbfcfe}}.hot-panel-head{{display:flex;align-items:baseline;justify-content:space-between;padding:13px 14px 8px}}.hot-panel-head h3{{margin:0;font-size:15px}}.hot-table-wrap{{max-height:650px;overflow:auto}}.hot-table{{font-size:12px;table-layout:fixed}}.hot-table th{{position:sticky;top:0;z-index:1;background:#f5f7fa}}.hot-table th:nth-child(1){{width:36px}}.hot-table th:nth-child(2){{width:80px}}.hot-table th:nth-child(3){{width:58px}}.hot-table th:nth-child(4){{width:50px}}.hot-table td{{vertical-align:top}}.rank-badge{{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:6px;color:#8a96a8;background:#edf1f6}}.rank-top{{color:#fff;background:#ef4444}}.stock-code{{display:block;margin-top:3px;font-size:10px}}.hot-number{{font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}}.hot-reason{{line-height:1.45;overflow:hidden}}.hot-reason span{{display:inline-block;max-width:100%;margin:0 4px 4px 0;padding:2px 5px;border:1px solid #dde5ef;border-radius:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#536278;background:#fff}}.hot-reason span:first-child{{color:#d33d43;border-color:#f3c6c8;background:#fff7f7}}.hot-reason .reason-missing{{color:#8a96a8;border-color:#dde5ef;background:#fff}}.hot-empty{{padding:28px;text-align:center;color:#8a96a8}}.component{{margin:17px 0}}.component strong{{float:right}}.definition{{display:block;margin-top:7px;line-height:1.55}}.bar{{clear:both;height:9px;background:#edf1f6;border-radius:8px;margin:8px 0}}.bar i{{display:block;height:100%;background:#2869b2;border-radius:8px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:9px 6px;text-align:left;border-bottom:1px solid #edf1f6}}th{{color:#68778d;font-weight:500}}.note{{font-size:12px;line-height:1.6}}@media(max-width:900px){{.index-grid{{grid-template-columns:repeat(3,1fr)}}.market-kpis{{grid-template-columns:repeat(2,1fr)}}.hot-rank-grid{{grid-template-columns:1fr}}}}@media(max-width:720px){{.cards,.grid{{grid-template-columns:1fr 1fr}}.grid>section:first-child{{grid-column:span 2}}.trend-grid{{grid-template-columns:1fr}}.trend-grid>section:first-child{{grid-column:auto}}.index-grid{{grid-template-columns:repeat(2,1fr)}}.section-head{{display:block}}.distribution-bars{{gap:3px}}.distribution-bin span{{font-size:10px;transform:rotate(-35deg);transform-origin:top center;margin-top:9px}}.distribution-progress-labels{{font-size:12px;gap:5px}}.industry-heatmap{{height:330px}}.industry-tile{{padding:7px}}.industry-tile b{{font-size:12px}}.industry-tile strong{{font-size:15px;margin-top:4px}}.hot-table-wrap{{max-height:520px}}}}@media(max-width:450px){{.cards,.grid,.index-grid,.market-kpis{{grid-template-columns:1fr}}.grid>section:first-child{{grid-column:auto}}.hot-table thead{{display:none}}.hot-table,.hot-table tbody{{display:block}}.hot-table tr{{display:grid;grid-template-columns:34px minmax(0,1fr) 64px 58px;padding:8px 5px;border-bottom:1px solid #edf1f6}}.hot-table td{{display:block;padding:4px;border:0;min-width:0}}.hot-table td:nth-child(5){{grid-column:2/5;padding-top:0}}}}</style><main>
<style>.extrema-dot{{stroke:#fff;stroke-width:2;pointer-events:none}}.extrema-text{{font-size:11px;font-weight:750;paint-order:stroke;stroke:#fff;stroke-width:3px;stroke-linejoin:round;pointer-events:none}}.extrema-score{{fill:#5a4fcf}}.extrema-amount{{fill:#2869b2}}.extrema-mean{{fill:#d97706}}.chart-hit{{fill:transparent;stroke:transparent;stroke-width:2;cursor:pointer;outline:none}}.chart-hit-score:hover,.chart-hit-score:focus{{fill:#5a4fcf;stroke:#fff}}.chart-hit-amount:hover,.chart-hit-amount:focus{{fill:#2869b2;stroke:#fff}}.chart-hit-mean:hover,.chart-hit-mean:focus{{fill:#d97706;stroke:#fff}}.turnover-bar{{shape-rendering:geometricPrecision;cursor:pointer;outline:none}}.turnover-bar:hover,.turnover-bar:focus{{opacity:1;stroke:#fff;stroke-width:1.5}}.turnover-bar-up{{fill:#e5484d;opacity:.82}}.turnover-bar-down{{fill:#19a665;opacity:.82}}.turnover-bar-flat{{fill:#aeb8c5;opacity:.75}}.chart-data-popover{{position:fixed;z-index:200;width:max-content;max-width:min(300px,calc(100vw - 24px));padding:12px 34px 12px 14px;border:1px solid #d9e2ed;border-radius:10px;background:rgba(255,255,255,.98);box-shadow:0 8px 28px rgba(24,43,73,.22);color:#182b49;pointer-events:auto}}.chart-data-popover[hidden]{{display:none}}.chart-data-popover b,.chart-data-popover strong,.chart-data-popover span{{display:block}}.chart-data-popover b{{font-size:13px}}.chart-data-popover small{{display:block;margin-top:3px}}.chart-data-popover strong{{margin-top:7px;font-size:20px;font-variant-numeric:tabular-nums}}.chart-data-popover span{{margin-top:5px;color:#68778d;font-size:12px}}.chart-data-popover button{{position:absolute;top:5px;right:7px;border:0;background:transparent;color:#7c899a;font-size:20px;line-height:1;cursor:pointer}}.hot-plate-tabs{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;padding:0 12px 12px}}.hot-plate-tabs button{{appearance:none;border:1px solid #e3e9f1;border-radius:8px;padding:9px 6px;background:#f4f6f9;color:#59677c;font:inherit;font-weight:700;cursor:pointer;transition:background .15s ease,border-color .15s ease,color .15s ease}}.hot-plate-tabs button:hover{{border-color:#f0a8ab;background:#fff7f7}}.hot-plate-tabs button[aria-selected="true"]{{border-color:#f3b4b7;background:#fff0f1;color:#d93f45;box-shadow:inset 0 -2px 0 #e5484d}}.hot-plate-tabs button small{{margin-left:4px;color:inherit;font-size:10px;font-weight:600}}.hot-plate-view[hidden]{{display:none}}@media(max-width:450px){{.hot-plate-tabs{{gap:4px;padding:0 7px 9px}}.hot-plate-tabs button{{padding:8px 3px;font-size:12px}}}}</style>
<style>
.industry-tile{{padding:4px}}
.industry-tile-label{{display:flex;width:100%;min-width:0}}
.industry-tile b{{max-width:100%;font-size:clamp(8px,min(10cqi,30cqb),15px);line-height:1.15;white-space:normal;overflow-wrap:anywhere;overflow:visible;text-overflow:clip}}
.industry-tile strong{{max-width:100%;font-size:clamp(9px,min(13cqi,35cqb),19px);line-height:1.1;margin-top:clamp(1px,2cqb,7px);white-space:nowrap}}
@container (max-width:28px){{.industry-tile-label{{display:none}}}}
@container (max-height:24px){{.industry-tile-label{{display:none}}}}
</style>
<style>
.valuation-market{{margin-top:20px}}.valuation-report-link{{display:flex;align-items:center;justify-content:space-between;gap:18px;margin:4px 0 14px;padding:15px 18px;border:1px solid #cdd9f0;border-radius:10px;background:linear-gradient(135deg,#f8faff,#eef3ff);color:#182b49;text-decoration:none;transition:transform .15s ease,border-color .15s ease,box-shadow .15s ease}}.valuation-report-link:hover{{transform:translateY(-1px);border-color:#9fb5e8;box-shadow:0 6px 18px rgba(43,92,217,.10)}}.valuation-report-link span{{display:flex;min-width:0;flex-direction:column;gap:4px}}.valuation-report-link b{{font-size:15px}}.valuation-report-link small{{line-height:1.5}}.valuation-report-link>strong{{flex:none;color:#2b5cd9;white-space:nowrap}}.valuation-chart text{{font-size:11px;fill:#68778d}}.valuation-zone-high{{fill:#e5484d;opacity:.08}}.valuation-zone-low{{fill:#19a665;opacity:.08}}.valuation-p80{{stroke:#d85b61;stroke-width:1.4;stroke-dasharray:7 5}}.valuation-p20{{stroke:#2f9e70;stroke-width:1.4;stroke-dasharray:7 5}}.valuation-line-label{{font-weight:750;paint-order:stroke;stroke:#fff;stroke-width:3px}}.valuation-high{{fill:#c13e44!important}}.valuation-low{{fill:#21845c!important}}.valuation-halo{{fill:none;stroke:#fff;stroke-width:6;stroke-linecap:round;stroke-linejoin:round;opacity:.82}}.valuation-pe{{fill:none;stroke:#2869b2;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}}.valuation-latest{{fill:#2869b2;stroke:#fff;stroke-width:2}}.valuation-current{{fill:#2869b2!important;font-weight:750;paint-order:stroke;stroke:#fff;stroke-width:3px}}.extrema-valuation{{fill:#2869b2}}.valuation-warning{{color:#b47b00}}@media(max-width:720px){{.valuation-market{{padding:13px}}.valuation-report-link{{align-items:flex-start;flex-direction:column;gap:9px;padding:13px}}}}
</style>
{WATCHLIST_CSS}
<h1>A股市场情绪仪表盘</h1><p class="sub">{subtitle}</p>
{index_market_html}
{market_distribution_html}
{industry_heatmap_html}
{hot_rankings_html}
<div class="cards"><div class="card"><small>综合情绪</small><div class="value tone {score_tone(last.sentiment_score)}">{score}</div><small><span class="tone {score_tone(last.sentiment_score)}">{last.sentiment_state}</span> · 可用分项 {int(last.available_components)}/{len(COMPONENTS)}</small></div><div class="card"><small>市场阶段</small><div class="value tone {score_tone(last.momentum_score)}" style="font-size:21px;line-height:1.35">{last.market_phase}</div><small>结构 {last.structural_score:.0f} · {last.structural_state} / 动量 {last.momentum_score:.0f} · {last.momentum_state}</small></div><div class="card"><small>{'盘中累计成交额' if is_intraday else '日成交额'}</small><div class="value">{amount_value}</div><small>{amount_detail}</small>{amount_comparison}</div><div class="card"><small>量比（相对20日）</small><div class="value">{last.vs_ma20:.2f}×</div><small>{last.liquidity_state}{' · 盘中投影' if is_intraday else ''}</small></div><div class="card"><small>数据覆盖</small><div class="value">{last.coverage_weight_pct:.0f}%</div><small>{'五项均为盘中可用，其中资金为代理' if is_intraday else '综合分只使用已接入分项'}</small></div>{proxy_card}</div>
<div class="grid"><section><h2>近120日综合情绪</h2><svg class="sentiment-chart" viewBox="0 0 1080 500" role="img" aria-label="近120日综合情绪，背景按极冷、偏冷、中性、偏热和过热分级"><rect class="sentiment-band-hot-deep" x="56" y="26" width="1002" height="103"/><rect class="sentiment-band-hot" x="56" y="129" width="1002" height="61.8"/><rect class="sentiment-band-neutral" x="56" y="190.8" width="1002" height="61.8"/><rect class="sentiment-band-cold" x="56" y="252.6" width="1002" height="82.4"/><rect class="sentiment-band-cold-deep" x="56" y="335" width="1002" height="103"/><line class="axis" x1="56" y1="438" x2="1058" y2="438"/><line class="axis" x1="56" y1="335" x2="1058" y2="335"/><line class="axis" x1="56" y1="252.6" x2="1058" y2="252.6"/><line class="axis" x1="56" y1="190.8" x2="1058" y2="190.8"/><line class="axis" x1="56" y1="129" x2="1058" y2="129"/><line class="axis" x1="56" y1="26" x2="1058" y2="26"/><text x="20" y="442">0</text><text x="12" y="339">25</text><text x="12" y="257">45</text><text x="12" y="195">60</text><text x="12" y="133">75</text><text x="4" y="30">100</text><text class="sentiment-band-label" x="1046" y="82" fill="#8f1919">过热 76–100</text><text class="sentiment-band-label" x="1046" y="164" fill="#b6403c">偏热 61–75</text><text class="sentiment-band-label" x="1046" y="225" fill="#8a6500">中性 46–60</text><text class="sentiment-band-label" x="1046" y="298" fill="#26784e">偏冷 26–45</text><text class="sentiment-band-label" x="1046" y="390" fill="#075f45">极冷 0–25</text><path class="score-halo" d="{score_path}"/><path class="score" d="{score_path}"/>{score_clicks}{score_extrema}{score_dates}<text x="1058" y="495" text-anchor="end">交易日期</text></svg><p class="note"><span class="tone tone-cold-deep">0–25 极冷</span> · <span class="tone tone-cold">26–45 偏冷</span> · <span class="tone tone-neutral">46–60 中性</span> · <span class="tone tone-hot">61–75 偏热</span> · <span class="tone tone-hot-deep">76–100 过热</span> · 点击折线数据点查看当日数值</p><p class="note">{summary}</p></section><section><h2>强弱分项</h2>{''.join(component_html)}<p class="note">缺失分项不按零分处理，综合分会按实际可用权重重新归一化。</p></section></div>
<div class="grid trend-grid"><section><h2>市场广度趋势</h2><svg viewBox="0 0 1080 290"><line class="axis" x1="56" y1="24" x2="1058" y2="24"/><line class="axis" x1="56" y1="138" x2="1058" y2="138"/><line class="axis" x1="56" y1="252" x2="1058" y2="252"/><text x="4" y="28">100</text><text x="12" y="142">50</text><text x="20" y="256">0</text><path class="score" d="{breadth_path}"/>{breadth_clicks}{breadth_extrema}{breadth_dates}</svg><p class="note">0–100分 · 涨跌家数、MA20/MA60、新高新低与10日腾落趋势 · 点击折线数据点查看当日数值 · {breadth_latest}</p></section><section><h2>短线赚钱效应趋势</h2><svg viewBox="0 0 1080 290"><line class="axis" x1="56" y1="24" x2="1058" y2="24"/><line class="axis" x1="56" y1="138" x2="1058" y2="138"/><line class="axis" x1="56" y1="252" x2="1058" y2="252"/><text x="4" y="28">100</text><text x="12" y="142">50</text><text x="20" y="256">0</text><path class="mean" d="{short_path}"/>{short_clicks}{short_extrema}{short_dates}</svg><p class="note">0–100分 · 昨涨停收益、晋级率、封板率、涨跌停强弱与连板高度 · 点击折线数据点查看当日数值 · {short_latest}</p></section></div>
<section><h2>资金风险偏好：沪深两市融资余额走势</h2><svg viewBox="0 0 1080 290"><line class="axis" x1="56" y1="24" x2="1058" y2="24"/><line class="axis" x1="56" y1="138" x2="1058" y2="138"/><line class="axis" x1="56" y1="252" x2="1058" y2="252"/><text x="4" y="28">{capital_max:.0f}</text><text x="4" y="142">{capital_mid:.0f}</text><text x="4" y="256">{capital_min:.0f}</text><path class="amount" d="{capital_path}"/><path class="mean" d="{capital_ma20_path}"/>{capital_clicks}{capital_ma20_clicks}{capital_extrema}{capital_dates}<text x="65" y="14" fill="#2869b2">沪深融资余额</text><text x="175" y="14" fill="#ec8b00">20日均线</text></svg><p class="note">{capital_note}</p></section>
<section><h2>近120日日成交额与20日均额</h2><svg viewBox="0 0 1080 315"><line class="axis" x1="56" y1="24" x2="1058" y2="24"/><line class="axis" x1="56" y1="138" x2="1058" y2="138"/><line class="axis" x1="56" y1="252" x2="1058" y2="252"/><text x="4" y="14">成交额（万亿元）</text><text x="4" y="28">{amount_max/10000:.2f}</text><text x="4" y="142">{amount_max/20000:.2f}</text><text x="20" y="256">0</text>{amount_bars}<path class="mean" d="{ma_path}"/>{amount_ma_clicks}{amount_extrema}{amount_dates}<text x="1058" y="307" text-anchor="end">交易日期</text><rect class="turnover-bar-up" x="162" y="5" width="10" height="9"/><text x="177" y="14">较昨日放量</text><rect class="turnover-bar-down" x="260" y="5" width="10" height="9"/><text x="275" y="14">较昨日缩量</text><line x1="365" y1="10" x2="385" y2="10" stroke="#ec8b00" stroke-width="2"/><text x="390" y="14" fill="#b36b00">20日均额</text></svg><p class="note">横轴：交易日期 · 纵轴：沪深A股成交额（万亿元）· 红柱=较上一交易日放量 · 绿柱=较上一交易日缩量 · 灰柱=持平或无可比数据。盘中当日柱按上一交易日同一时点的成交进度投影，已纳入集合竞价。点击柱子或均线数据点查看当日数值。</p></section>
<section><h2>最近30个交易日综合分、量比与状态</h2><table><thead><tr><th>日期</th><th>成交额</th><th>量比</th><th>综合分</th><th>状态</th><th>市场阶段</th></tr></thead><tbody>{rows}</tbody></table></section><p class="note">量比＝当日成交额÷20日平均成交额；表格按日期倒序排列。{source_note} 成交额反映参与度，不单独代表涨跌方向。</p>
{valuation_html}
<div id="chart-data-popover" class="chart-data-popover" role="status" aria-live="polite" hidden><button type="button" data-chart-popup-close aria-label="关闭">×</button><b data-chart-popup-series></b><small data-chart-popup-date></small><strong data-chart-popup-value></strong><span data-chart-popup-extra hidden></span></div>
<script>document.querySelectorAll("[data-hot-plate-tab]").forEach(function(button){{button.addEventListener("click",function(){{var panel=button.closest(".hot-plate-panel");var key=button.getAttribute("data-hot-plate-tab");panel.querySelectorAll("[data-hot-plate-tab]").forEach(function(tab){{tab.setAttribute("aria-selected",String(tab===button))}});panel.querySelectorAll("[data-hot-plate-view]").forEach(function(view){{view.hidden=view.getAttribute("data-hot-plate-view")!==key}})}})}});</script>
{WATCHLIST_SCRIPT}
{DASHBOARD_REFRESH_SCRIPT}
<script>(function(){{var tip=document.getElementById("chart-data-popover");if(!tip)return;var series=tip.querySelector("[data-chart-popup-series]"),date=tip.querySelector("[data-chart-popup-date]"),value=tip.querySelector("[data-chart-popup-value]"),extra=tip.querySelector("[data-chart-popup-extra]");function hideTip(){{tip.hidden=true}}function showTip(target){{series.textContent=target.getAttribute("data-chart-series")||"";date.textContent=target.getAttribute("data-chart-date")||"";value.textContent=target.getAttribute("data-chart-value")||"";var extraText=target.getAttribute("data-chart-extra")||"";extra.textContent=extraText;extra.hidden=!extraText;tip.hidden=false;var targetRect=target.getBoundingClientRect(),tipRect=tip.getBoundingClientRect();var left=Math.min(window.innerWidth-tipRect.width-8,Math.max(8,targetRect.left+targetRect.width/2-tipRect.width/2));var top=targetRect.top-tipRect.height-10;if(top<8)top=Math.min(window.innerHeight-tipRect.height-8,targetRect.bottom+10);tip.style.left=left+"px";tip.style.top=Math.max(8,top)+"px"}}document.addEventListener("click",function(event){{var target=event.target.closest?event.target.closest("[data-chart-date]"):null;if(target){{showTip(target);return}}if(event.target.closest&&event.target.closest("#chart-data-popover")){{if(event.target.closest("[data-chart-popup-close]"))hideTip();return}}hideTip()}});document.addEventListener("keydown",function(event){{if(event.key==="Escape"){{hideTip();return}}var target=event.target.closest?event.target.closest("[data-chart-date]"):null;if(target&&(event.key==="Enter"||event.key===" ")){{event.preventDefault();showTip(target)}}}})}})();</script></main></html>'''
    output_path = Path(output)
    output_tmp = output_path.with_name(f".{output_path.name}.tmp")
    output_tmp.write_text(doc, encoding="utf-8")
    output_tmp.replace(output_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--turnover", default="hs_a_share_turnover.csv")
    parser.add_argument("--extra", default="market_sentiment_daily.csv")
    parser.add_argument("--intraday", default="market_sentiment_intraday.csv")
    parser.add_argument("--margin", default="market_margin_balance.csv")
    parser.add_argument("--valuation", default=str(DEFAULT_VALUATION_CACHE))
    parser.add_argument("--output", default="market_sentiment_dashboard.html")
    args = parser.parse_args()
    render_dashboard(
        build_daily_sentiment(args.turnover, args.extra, args.intraday),
        args.output,
        args.margin,
        args.valuation,
    )
    print(f"dashboard written: {args.output}")
