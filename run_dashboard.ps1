$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Wait-BeforeExit {
    param([string]$Message)
    Write-Host ""
    Write-Host $Message -ForegroundColor Red
    Write-Host "Press Enter to close this window..."
    [void](Read-Host)
}

# 优先使用项目内 .venv 虚拟环境中的 Python
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$pythonExecutable = $null
$pythonArguments = @()
if (Test-Path -LiteralPath $venvPython) {
    $pythonExecutable = $venvPython
}
else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
        $pythonArguments = @("-3")
    }
    if ($pythonCommand) {
        $pythonExecutable = $pythonCommand.Source
    }
}

if (-not $pythonExecutable) {
    Wait-BeforeExit "Python 3 was not found. Install Python 3 and add it to PATH."
    exit 1
}

try {
    & $pythonExecutable @pythonArguments -c "import numpy, pandas, requests" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "missing dependencies"
    }
}
catch {
    Wait-BeforeExit "Missing dependencies. Run: python -m pip install numpy pandas requests openpyxl xlrd"
    exit 1
}

$dashboardPath = Join-Path $PSScriptRoot "market_sentiment_dashboard.html"
$intradayScript = Join-Path $PSScriptRoot "market_intraday_sentiment.py"
$capitalScript = Join-Path $PSScriptRoot "market_capital_risk_appetite.py"
$fearGreedScript = Join-Path $PSScriptRoot "market_fear_greed.py"
$equityBondScript = Join-Path $PSScriptRoot "market_hs300_equity_bond.py"
$buffettScript = Join-Path $PSScriptRoot "market_buffett_index.py"
$dashboardScript = Join-Path $PSScriptRoot "market_sentiment_dashboard.py"
$watchlistServerScript = Join-Path $PSScriptRoot "market_watchlist_server.py"
$turnoverPath = Join-Path $PSScriptRoot "hs_a_share_turnover.csv"
$intradayOutput = Join-Path $PSScriptRoot "market_sentiment_intraday.csv"
$dailyOutput = Join-Path $PSScriptRoot "market_sentiment_daily.csv"
$marginOutput = Join-Path $PSScriptRoot "market_margin_balance.csv"
$startupFailureSignal = Join-Path $PSScriptRoot ".dashboard_startup_failed"
$dashboardPort = if ($env:DASHBOARD_PORT) { [int]$env:DASHBOARD_PORT } else { 8765 }
$dashboardBaseUrl = "http://127.0.0.1:$dashboardPort/"
$dashboardUrl = $dashboardBaseUrl
$dashboardOpenedSignal = Join-Path $PSScriptRoot ".dashboard_opened"

Remove-Item -LiteralPath $startupFailureSignal -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $dashboardOpenedSignal -Force -ErrorAction SilentlyContinue

function Stop-StaleDashboardPythonProcesses {
    param(
        [string[]]$ScriptPaths,
        [int]$Port
    )

    $patterns = $ScriptPaths | ForEach-Object {
        [regex]::Escape([IO.Path]::GetFullPath($_))
    }
    $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $staleProcesses = @($allProcesses |
        Where-Object {
            $candidate = $_
            $candidate.Name -in @("python.exe", "python3.exe", "py.exe") -and
            $candidate.CommandLine -and
            ($patterns | Where-Object { $candidate.CommandLine -match $_ })
        })

    # Also inspect the listening port in case process enumeration or command-line
    # formatting misses a stale server. Never stop an unrelated port owner.
    $listenerParameters = @{
        LocalPort = $Port
        State = "Listen"
        ErrorAction = "SilentlyContinue"
    }
    $listeners = @(Get-NetTCPConnection @listenerParameters)
    foreach ($listener in $listeners) {
        $owner = $allProcesses | Where-Object {
            $_.ProcessId -eq $listener.OwningProcess
        } | Select-Object -First 1
        if (-not $owner) {
            throw "Port $Port is occupied by process $($listener.OwningProcess), but its command line could not be inspected."
        }
        $belongsToDashboard = $owner.CommandLine -and
            ($patterns | Where-Object { $owner.CommandLine -match $_ })
        if (-not $belongsToDashboard) {
            throw "Port $Port is occupied by unrelated process $($owner.ProcessId): $($owner.Name)"
        }
        $staleProcesses += $owner
    }
    $staleProcesses = @($staleProcesses | Sort-Object -Property ProcessId -Unique)
    foreach ($staleProcess in $staleProcesses) {
        Write-Host "Stopping stale dashboard process $($staleProcess.ProcessId)..." `
            -ForegroundColor Yellow
        Stop-Process -Id $staleProcess.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($staleProcesses) {
        $deadline = (Get-Date).AddSeconds(5)
        do {
            $remaining = @($staleProcesses | Where-Object {
                Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
            })
            if (-not $remaining) { break }
            Start-Sleep -Milliseconds 100
        } while ((Get-Date) -lt $deadline)
        if ($remaining) {
            $remainingIds = ($remaining.ProcessId -join ", ")
            throw "Could not stop stale dashboard processes: $remainingIds"
        }
    }
}

# A launcher closed without cleanup can leave Python holding port 8765 or
# overwriting the dashboard. Only stop stale processes from this project.
$staleProcessParameters = @{
    ScriptPaths = @($intradayScript, $watchlistServerScript)
    Port = $dashboardPort
}
Stop-StaleDashboardPythonProcesses @staleProcessParameters

# 首次运行缺少成交额缓存时自动初始化近一年数据
if (-not (Test-Path -LiteralPath $turnoverPath)) {
    Write-Host "首次运行，正在初始化沪深A股日成交额缓存..." -ForegroundColor Cyan
    & $pythonExecutable @pythonArguments -c "import pandas as pd; from market_turnover_sentiment import hs_a_share_turnover; hs_a_share_turnover((pd.Timestamp.today() - pd.Timedelta(days=365)).strftime('%Y-%m-%d'), cache_path=r'$turnoverPath')"
    if ($LASTEXITCODE -ne 0) {
        Set-Content -LiteralPath $startupFailureSignal -Value (Get-Date).ToString("o")
        Wait-BeforeExit "初始化沪深A股日成交额缓存失败，请检查上方错误信息。"
        exit $LASTEXITCODE
    }
}

Write-Host "Refreshing the intraday snapshot before opening the dashboard..." -ForegroundColor Cyan
# Refresh intraday data before rendering/opening HTML so the browser never starts
# from the previous run's stale turnover estimate.
$initialRefreshArguments = @()
$initialRefreshArguments += $pythonArguments
$initialRefreshArguments += $intradayScript
$initialRefreshArguments += "--turnover"
$initialRefreshArguments += $turnoverPath
$initialRefreshArguments += "--output"
$initialRefreshArguments += $intradayOutput
$initialRefreshArguments += "--dashboard-output"
$initialRefreshArguments += $dashboardPath
$initialRefreshArguments += "--workers"
$initialRefreshArguments += "16"
& $pythonExecutable @initialRefreshArguments
if ($LASTEXITCODE -ne 0) {
    Set-Content -LiteralPath $startupFailureSignal -Value (Get-Date).ToString("o")
    Wait-BeforeExit "Initial intraday refresh failed. The dashboard was not opened to avoid showing stale data. Review the error above."
    exit $LASTEXITCODE
}

Write-Host "Refreshing two-year Shanghai + Shenzhen margin-balance history..." -ForegroundColor Cyan
$capitalArguments = @()
$capitalArguments += $pythonArguments
$capitalArguments += $capitalScript
$capitalArguments += "--turnover"
$capitalArguments += $turnoverPath
$capitalArguments += "--output"
$capitalArguments += $dailyOutput
$capitalArguments += "--margin-output"
$capitalArguments += $marginOutput
& $pythonExecutable @capitalArguments
if ($LASTEXITCODE -ne 0) {
    Wait-BeforeExit "Shanghai + Shenzhen margin-balance refresh failed. Review the error above."
    exit $LASTEXITCODE
}

# 刷新恐惧贪婪指数、股债利差与中国巴菲特指数，单项失败时沿用已有缓存
$optionalModules = @(
    @{ Name = "恐惧贪婪指数"; Script = $fearGreedScript },
    @{ Name = "沪深300/中证500股债利差"; Script = $equityBondScript },
    @{ Name = "中国巴菲特指数"; Script = $buffettScript }
)
foreach ($module in $optionalModules) {
    Write-Host "正在刷新$($module.Name)..." -ForegroundColor Cyan
    try {
        & $pythonExecutable @pythonArguments $module.Script
        if ($LASTEXITCODE -ne 0) {
            Write-Host "警告：$($module.Name)刷新失败，将沿用已有缓存。" -ForegroundColor Yellow
        }
    }
    catch {
        Write-Host "警告：$($module.Name)刷新失败，将沿用已有缓存。" -ForegroundColor Yellow
    }
}

# The intraday script rendered once before the margin history was refreshed.
# Render again so the page opened below contains the new two-market series.
& $pythonExecutable @pythonArguments $dashboardScript `
    --turnover $turnoverPath --extra $dailyOutput --intraday $intradayOutput `
    --margin $marginOutput --output $dashboardPath
if ($LASTEXITCODE -ne 0) {
    Wait-BeforeExit "Dashboard regeneration with margin history failed."
    exit $LASTEXITCODE
}

$serverArguments = @()
$serverArguments += $pythonArguments
$serverArguments += $watchlistServerScript
$serverArguments += "--dashboard"
$serverArguments += $dashboardPath
$serverArguments += "--port"
$serverArguments += "$dashboardPort"
$serverInstanceToken = [Guid]::NewGuid().ToString("N")
$serverArguments += "--instance-token"
$serverArguments += $serverInstanceToken
$serverProcess = Start-Process -FilePath $pythonExecutable `
    -ArgumentList $serverArguments -PassThru -WindowStyle Hidden

try {
    $serverReady = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ($serverProcess.HasExited) {
            throw "The new dashboard server exited before becoming ready."
        }
        try {
            $health = Invoke-WebRequest -UseBasicParsing -Uri "${dashboardBaseUrl}health" -TimeoutSec 1
            $healthPayload = $health.Content | ConvertFrom-Json
            if ($health.StatusCode -eq 200 -and
                $healthPayload.instance_token -eq $serverInstanceToken) {
                $serverReady = $true
                break
            }
        }
        catch {
            # The child process may still be importing dependencies.
        }
        if (-not $serverReady) {
            # Also wait when an old listener answered with a mismatched token;
            # otherwise all attempts can be consumed before the new child starts.
            Start-Sleep -Milliseconds 200
        }
    }
    if (-not $serverReady) {
        throw "Local dashboard server did not start on $dashboardBaseUrl"
    }

    $chromeLauncherScript = Join-Path $PSScriptRoot "open_dashboard_chrome.ps1"
    $opened = $false
    for ($i = 0; $i -lt 15; $i++) {
        if (Test-Path -LiteralPath $dashboardOpenedSignal) {
            $opened = $true
            break
        }
        Start-Sleep -Milliseconds 200
    }
    if (-not $opened -and (Test-Path -LiteralPath $chromeLauncherScript)) {
        try {
            & "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
                -NoLogo -NoProfile -ExecutionPolicy Bypass `
                -File $chromeLauncherScript -BaseUrl $dashboardBaseUrl
            Set-Content -LiteralPath $dashboardOpenedSignal -Value (Get-Date).ToString("o")
        }
        catch {
            # 浏览器调起失败不阻断服务主循环
        }
    }

    Write-Host ""
    Write-Host "The dashboard is ready; the browser opener is handling the new tab." -ForegroundColor Green
    Write-Host "The opened page is based on the latest intraday snapshot."
    Write-Host "The watchlist supports code/name/pinyin/English search across A/HK/US/KR."
    Write-Host "New additions fetch quotes immediately; no dashboard restart is needed."
    Write-Host "Intraday sources refresh every 60 seconds; SWA and CSI official valuation series refresh once daily."
    Write-Host "Refreshing continues until you stop this launcher."
    Write-Host ""

    $watchArguments = @()
    $watchArguments += $pythonArguments
    $watchArguments += $intradayScript
    $watchArguments += "--watch-seconds"
    $watchArguments += "60"
    $watchArguments += "--turnover"
    $watchArguments += $turnoverPath
    $watchArguments += "--output"
    $watchArguments += $intradayOutput
    $watchArguments += "--dashboard-output"
    $watchArguments += $dashboardPath
    & $pythonExecutable @watchArguments

    if ($LASTEXITCODE -ne 0) {
        Wait-BeforeExit "The live refresh process stopped unexpectedly. Review the error above."
        exit $LASTEXITCODE
    }

    Write-Host ""
    Write-Host "The dashboard server is still running at $dashboardUrl" -ForegroundColor Green
    Write-Host "Press Enter to stop the server and close this launcher."
    [void](Read-Host)
}
finally {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "The dashboard server has stopped." -ForegroundColor Green
