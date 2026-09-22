param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl
)

$ErrorActionPreference = "Stop"

$httpChoice = Get-ItemProperty `
    -LiteralPath "HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice" `
    -ErrorAction SilentlyContinue
$browserProgId = if ($httpChoice -and $httpChoice.ProgId) { $httpChoice.ProgId } else { "DefaultBrowser" }

$launchUrl = $BaseUrl.TrimEnd("/") + "/?dashboardLaunch=" + ([Guid]::NewGuid().ToString("N")) + "#watchlist"
Write-Host "Opening dashboard in browser ($browserProgId): $launchUrl" -ForegroundColor Cyan
Start-Process -FilePath $launchUrl -ErrorAction Stop
Start-Sleep -Milliseconds 1500
Write-Host "Browser launch command completed: $launchUrl" -ForegroundColor Green
