# Tek komutla kurulum / guncelleme (git gerekmez):
#   irm https://raw.githubusercontent.com/kayametehan/hermes-meeting-notes/main/get.ps1 | iex
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$tmp = Join-Path $env:TEMP "mn-install"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
Write-Host "Indiriliyor..."
Invoke-WebRequest "https://github.com/kayametehan/hermes-meeting-notes/archive/refs/heads/main.zip" -OutFile "$tmp\mn.zip"
Expand-Archive "$tmp\mn.zip" -DestinationPath $tmp -Force
powershell -ExecutionPolicy Bypass -File "$tmp\hermes-meeting-notes-main\install.ps1"
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
Write-Host "`n--- Kontrol ---"
hermes meeting doctor
Write-Host "`n--- Ornek ses testi ---"
Invoke-WebRequest "https://raw.githubusercontent.com/kayametehan/hermes-meeting-notes/main/samples/ornek-toplanti.ogg" -OutFile "$tmp\ornek.ogg"
hermes meeting process "$tmp\ornek.ogg" --transcript-only
Write-Host "`n--- Otomatik izleyici (gelen kutusu) ---"
hermes meeting autostart
hermes meeting inbox
Write-Host "`nHAZIR: Kayitlari acilan 'Toplanti-Inbox' klasorune at; ozet 'ozetler' icinde, gorevler 'Yapilacaklar.md' dosyasinda."
