$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Wait-BeforeExit {
    param([string]$Message)
    Write-Host ""
    Write-Host $Message -ForegroundColor Red
    Write-Host "Press Enter to close this window..."
    [void](Read-Host)
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$pythonArguments = @()
if (-not $pythonCommand) {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
    $pythonArguments = @("-3")
}

if (-not $pythonCommand) {
    Wait-BeforeExit "Python 3 was not found. Install Python 3 and add it to PATH."
    exit 1
}

try {
    & $pythonCommand.Source @pythonArguments -c "import pandas, requests" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "missing dependencies"
    }
}
catch {
    Wait-BeforeExit "Missing dependencies. Run: python -m pip install pandas requests"
    exit 1
}

$dashboardPath = Join-Path $PSScriptRoot "market_sentiment_dashboard.html"
$intradayScript = Join-Path $PSScriptRoot "market_intraday_sentiment.py"
$capitalScript = Join-Path $PSScriptRoot "market_capital_risk_appetite.py"
$dashboardScript = Join-Path $PSScriptRoot "market_sentiment_dashboard.py"
$watchlistServerScript = Join-Path $PSScriptRoot "market_watchlist_server.py"
$turnoverPath = Join-Path $PSScriptRoot "hs_a_share_turnover.csv"
$intradayOutput = Join-Path $PSScriptRoot "market_sentiment_intraday.csv"
$dailyOutput = Join-Path $PSScriptRoot "market_sentiment_daily.csv"
$marginOutput = Join-Path $PSScriptRoot "market_margin_balance.csv"
$startupFailureSignal = Join-Path $PSScriptRoot ".dashboard_startup_failed"
$dashboardPort = 8765
$dashboardBaseUrl = "http://127.0.0.1:$dashboardPort/"
$dashboardUrl = "${dashboardBaseUrl}#watchlist"

Remove-Item -LiteralPath $startupFailureSignal -Force -ErrorAction SilentlyContinue

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
& $pythonCommand.Source @initialRefreshArguments
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
& $pythonCommand.Source @capitalArguments
if ($LASTEXITCODE -ne 0) {
    Wait-BeforeExit "Shanghai + Shenzhen margin-balance refresh failed. Review the error above."
    exit $LASTEXITCODE
}

# The intraday script rendered once before the margin history was refreshed.
# Render again so the page opened below contains the new two-market series.
& $pythonCommand.Source @pythonArguments $dashboardScript `
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
$serverProcess = Start-Process -FilePath $pythonCommand.Source `
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

    Write-Host ""
    Write-Host "The dashboard is ready; the independent Chrome opener is handling the new tab." -ForegroundColor Green
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
    & $pythonCommand.Source @watchArguments

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
