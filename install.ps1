# Windows kurulumu: PowerShell'de çalıştır ->  powershell -ExecutionPolicy Bypass -File .\install.ps1
# Önkoşul: Hermes Agent kurulu olmalı (hermes komutu çalışmalı).
param(
  [string]$Times = "09:00,18:00",
  [string]$Deliver = "telegram"
)
$ErrorActionPreference = "Stop"

if (-not (Get-Command hermes -ErrorAction SilentlyContinue)) {
  throw "hermes komutu bulunamadı. Önce Hermes Agent'ı kur: https://hermes-agent.nousresearch.com/docs/"
}

# uv: Python 3.12'yi ve paketleri kullanıcı alanına kurar, yönetici izni gerekmez
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "uv kuruluyor..."
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

# Hermes veri klasörü: HERMES_HOME > %LOCALAPPDATA%\hermes (Windows varsayılanı) > %USERPROFILE%\.hermes
if ($env:HERMES_HOME) { $hermesHome = $env:HERMES_HOME }
elseif (Test-Path (Join-Path $env:LOCALAPPDATA "hermes")) { $hermesHome = Join-Path $env:LOCALAPPDATA "hermes" }
elseif (Test-Path (Join-Path $env:USERPROFILE ".hermes")) { $hermesHome = Join-Path $env:USERPROFILE ".hermes" }
else { $hermesHome = Join-Path $env:LOCALAPPDATA "hermes" }
Write-Host "Hermes klasörü: $hermesHome"
$dest = Join-Path $hermesHome "plugins\meeting-notes"
if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
Copy-Item -Recurse -Force $PSScriptRoot $dest
Remove-Item -Recurse -Force (Join-Path $dest ".git") -ErrorAction SilentlyContinue

hermes plugins enable meeting-notes
hermes meeting setup                       # sanal ortam + Türkçe Whisper modeli (ilk seferde indirir)
hermes config set stt.provider tr-whisper  # gateway'e gelen ses notları da bu modelle yazıya çevrilsin
hermes meeting reminders --times $Times --deliver $Deliver
Write-Host "`nTamam. Telegram'dan ses kaydı göndermeyi dene. Gateway açıksa: hermes gateway restart"
