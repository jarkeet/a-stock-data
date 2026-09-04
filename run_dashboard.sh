#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

pause_on_error() {
    local message="$1"
    printf '\n错误：%s\n' "$message" >&2
    exit 1
}

if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    pause_on_error "未找到 Python 3。请先安装 python3。"
fi

if ! "$PYTHON_BIN" -c "import pandas, requests" >/dev/null 2>&1; then
    pause_on_error "缺少依赖，请执行：$PYTHON_BIN -m pip install pandas requests"
fi

DASHBOARD_PATH="$SCRIPT_DIR/market_sentiment_dashboard.html"
DAILY_PATH="$SCRIPT_DIR/market_sentiment_daily.csv"
MARGIN_PATH="$SCRIPT_DIR/market_margin_balance.csv"
DASHBOARD_PORT=8765
DASHBOARD_BASE_URL="http://127.0.0.1:${DASHBOARD_PORT}/"
DASHBOARD_URL="${DASHBOARD_BASE_URL}#watchlist"
SERVER_INSTANCE_TOKEN="dashboard-$$-$(date +%s)"
SERVER_PID=""

cleanup() {
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

open_dashboard() {
    local target="$1"
    local chrome_bin=""
    for candidate in google-chrome google-chrome-stable chromium chromium-browser chrome.exe; do
        if command -v "$candidate" >/dev/null 2>&1; then
            chrome_bin="$candidate"
            break
        fi
    done
    if [[ -n "$chrome_bin" ]]; then
        # Chromium opens a command-line URL in a new tab unless --same-tab is used.
        nohup "$chrome_bin" "$target" >/dev/null 2>&1 &
    elif command -v open >/dev/null 2>&1; then
        nohup open -a "Google Chrome" "$target" >/dev/null 2>&1 &
    elif command -v xdg-open >/dev/null 2>&1; then
        printf '未检测到 Chrome，改用系统默认浏览器。\n'
        nohup xdg-open "$target" >/dev/null 2>&1 &
    elif command -v gio >/dev/null 2>&1; then
        printf '未检测到 Chrome，改用系统默认浏览器。\n'
        nohup gio open "$target" >/dev/null 2>&1 &
    elif command -v wslview >/dev/null 2>&1; then
        printf '未检测到 Chrome，改用系统默认浏览器。\n'
        nohup wslview "$target" >/dev/null 2>&1 &
    else
        printf '未检测到桌面浏览器启动工具。\n'
        printf '请手动打开：%s\n' "$target"
        return 1
    fi
}

printf '正在刷新近两年沪深两市融资余额...\n'
"$PYTHON_BIN" market_capital_risk_appetite.py \
    --turnover hs_a_share_turnover.csv \
    --output "$DAILY_PATH" \
    --margin-output "$MARGIN_PATH"

printf '正在生成市场情绪仪表盘...\n'
"$PYTHON_BIN" market_sentiment_dashboard.py \
    --turnover hs_a_share_turnover.csv \
    --extra "$DAILY_PATH" \
    --intraday market_sentiment_intraday.csv \
    --margin "$MARGIN_PATH" \
    --output "$DASHBOARD_PATH"

"$PYTHON_BIN" market_watchlist_server.py \
    --dashboard "$DASHBOARD_PATH" --port "$DASHBOARD_PORT" \
    --instance-token "$SERVER_INSTANCE_TOKEN" &
SERVER_PID=$!

server_ready=0
for _ in {1..20}; do
    if "$PYTHON_BIN" -c "import json, urllib.request; data=json.load(urllib.request.urlopen('${DASHBOARD_BASE_URL}health', timeout=1)); assert data.get('instance_token') == '$SERVER_INSTANCE_TOKEN'" >/dev/null 2>&1; then
        server_ready=1
        break
    fi
    sleep 0.2
done
if [[ "$server_ready" -ne 1 ]]; then
    pause_on_error "本地仪表盘服务启动失败：$DASHBOARD_BASE_URL"
fi

if open_dashboard "$DASHBOARD_URL"; then
    printf '仪表盘已在新的 Chrome 标签页中打开：%s\n' "$DASHBOARD_URL"
fi

printf '\n仪表盘所有数据源将每60秒刷新。\n'
printf '自选股支持A/港/美/韩股的代码、名称、拼音和英文搜索。\n'
printf '添加后会立即拉取行情，不需要重启仪表盘。\n'
printf '刷新会持续运行，直到按 Ctrl+C 停止启动器。\n\n'

"$PYTHON_BIN" market_intraday_sentiment.py \
    --watch-seconds 60 \
    --turnover hs_a_share_turnover.csv \
    --output market_sentiment_intraday.csv \
    --dashboard-output "$DASHBOARD_PATH"

printf '\nDashboard 服务仍在运行：%s\n' "$DASHBOARD_URL"
printf '按 Ctrl+C 可停止服务并关闭启动器。\n'
while true; do
    sleep 3600
done
