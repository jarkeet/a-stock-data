param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl,

    [Parameter(Mandatory = $true)]
    [string]$SnapshotPath,

    [Parameter(Mandatory = $true)]
    [string]$ChromeLauncherPath,

    [Parameter(Mandatory = $false)]
    [string]$FailureSignalPath = "",

    [Parameter(Mandatory = $false)]
    [int]$TimeoutSeconds = 600,

    [Parameter(Mandatory = $false)]
    [string]$SuccessSignalPath = ""
)

$ErrorActionPreference = "Stop"
$startedAt = Get-Date
$healthUrl = $BaseUrl.TrimEnd("/") + "/health"

for ($attempt = 0; $attempt -lt $TimeoutSeconds; $attempt++) {
    if ($FailureSignalPath -and (Test-Path -LiteralPath $FailureSignalPath) -and
        ((Get-Item -LiteralPath $FailureSignalPath).LastWriteTime -ge $startedAt)) {
        Write-Host "Dashboard startup was cancelled because the initial refresh failed." `
            -ForegroundColor Yellow
        exit 1
    }
    $snapshotReady = (Test-Path -LiteralPath $SnapshotPath) -and
        ((Get-Item -LiteralPath $SnapshotPath).LastWriteTime -ge $startedAt)
    if ($snapshotReady) {
        try {
            $health = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 1
            if ($health.StatusCode -eq 200) {
                & "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
                    -NoLogo -NoProfile -ExecutionPolicy Bypass `
                    -File $ChromeLauncherPath -BaseUrl $BaseUrl
                $exitCode = $LASTEXITCODE
                if ($exitCode -eq 0 -and $SuccessSignalPath) {
                    Set-Content -LiteralPath $SuccessSignalPath -Value (Get-Date).ToString("o")
                }
                exit $exitCode
            }
        }
        catch {
            # The main launcher may still be starting the local server.
        }
    }
    Start-Sleep -Seconds 1
}

Write-Error "Timed out waiting for the refreshed snapshot and dashboard server."
exit 1
