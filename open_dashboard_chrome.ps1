param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl
)

$ErrorActionPreference = "Stop"

$httpChoice = Get-ItemProperty `
    -LiteralPath "HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice" `
    -ErrorAction SilentlyContinue
if ($httpChoice.ProgId -notlike "ChromeHTML*") {
    throw "HTTP default browser is not Chrome (ProgId=$($httpChoice.ProgId))."
}

$launchUrl = $BaseUrl.TrimEnd("/") + "/?dashboardLaunch=" + ([Guid]::NewGuid().ToString("N")) + "#watchlist"
Write-Host "Requesting a new Chrome tab: $launchUrl" -ForegroundColor Cyan
Start-Process -FilePath $launchUrl -ErrorAction Stop
Start-Sleep -Milliseconds 1500
Write-Host "Chrome launch command completed: $launchUrl" -ForegroundColor Green
