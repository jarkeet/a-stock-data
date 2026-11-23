#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

pause_on_error() {
    local message="$1"
    printf '\n错误：%s\n' "$message" >&2
    exit 1
}

if [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    pause_on_error "未找到 Python 3。请先安装 python3。"
fi

if ! "$PYTHON_BIN" -c "import numpy, pandas, requests" >/dev/null 2>&1; then
    pause_on_error "缺少依赖，请执行：$PYTHON_BIN -m pip install numpy pandas requests openpyxl xlrd"
fi

DASHBOARD_PATH="$SCRIPT_DIR/market_sentiment_dashboard.html"
TURNOVER_PATH="$SCRIPT_DIR/hs_a_share_turnover.csv"
DAILY_PATH="$SCRIPT_DIR/market_sentiment_daily.csv"
MARGIN_PATH="$SCRIPT_DIR/market_margin_balance.csv"
DASHBOARD_PORT="${DASHBOARD_PORT:-8765}"
DASHBOARD_BASE_URL="http://127.0.0.1:${DASHBOARD_PORT}/"
DASHBOARD_URL="$DASHBOARD_BASE_URL"
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
        if [[ -d "/Applications/Google Chrome.app" ]]; then
            nohup open -a "Google Chrome" "$target" >/dev/null 2>&1 &
        else
            printf '未检测到 Chrome，改用系统默认浏览器。\n'
            nohup open "$target" >/dev/null 2>&1 &
        fi
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

if [[ ! -f "$TURNOVER_PATH" ]]; then
    printf '首次运行，正在初始化沪深A股日成交额缓存...\n'
    "$PYTHON_BIN" -c "import pandas as pd; from market_turnover_sentiment import hs_a_share_turnover; hs_a_share_turnover((pd.Timestamp.today() - pd.Timedelta(days=365)).strftime('%Y-%m-%d'), cache_path='$TURNOVER_PATH')"
fi

printf '正在刷新近两年沪深两市融资余额...\n'
"$PYTHON_BIN" market_capital_risk_appetite.py \
    --turnover "$TURNOVER_PATH" \
    --output "$DAILY_PATH" \
    --margin-output "$MARGIN_PATH"

printf '正在刷新恐惧贪婪指数...\n'
"$PYTHON_BIN" market_fear_greed.py || printf '警告：恐惧贪婪指数刷新失败，将沿用已有缓存。\n' >&2

printf '正在刷新沪深300/中证500股债利差...\n'
"$PYTHON_BIN" market_hs300_equity_bond.py || printf '警告：股债利差刷新失败，将沿用已有缓存。\n' >&2

printf '正在刷新中国巴菲特指数...\n'
"$PYTHON_BIN" market_buffett_index.py || printf '警告：巴菲特指数刷新失败，将沿用已有缓存。\n' >&2

printf '正在生成市场情绪仪表盘...\n'
"$PYTHON_BIN" market_sentiment_dashboard.py \
    --turnover "$TURNOVER_PATH" \
    --extra "$DAILY_PATH" \
    --intraday market_sentiment_intraday.csv \
    --margin "$MARGIN_PATH" \
    --output "$DASHBOARD_PATH"

release_existing_dashboard_server() {
    if ! command -v lsof >/dev/null 2>&1; then
        return 0
    fi
    local pids=""
    pids="$(lsof -tiTCP:"$DASHBOARD_PORT" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -z "$pids" ]]; then
        return 0
    fi
    local pid="" cmd=""
    for pid in $pids; do
        cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
        if [[ "$cmd" == *"market_watchlist_server.py"* ]]; then
            printf '检测到旧的仪表盘服务（PID %s）占用端口 %s，正在关闭...\n' "$pid" "$DASHBOARD_PORT"
            kill "$pid" 2>/dev/null || true
        else
            pause_on_error "端口 ${DASHBOARD_PORT} 已被其他进程占用（PID ${pid}: ${cmd:-unknown}）。请先释放端口，或改用 DASHBOARD_PORT=8766 ./run_dashboard.sh"
        fi
    done
    for _ in {1..15}; do
        if [[ -z "$(lsof -tiTCP:"$DASHBOARD_PORT" -sTCP:LISTEN 2>/dev/null || true)" ]]; then
            return 0
        fi
        sleep 0.2
    done
    pause_on_error "旧的仪表盘服务未能及时释放端口 ${DASHBOARD_PORT}，请手动结束后重试。"
}

release_existing_dashboard_server

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
