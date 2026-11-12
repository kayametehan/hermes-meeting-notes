# meeting-notes (Hermes plugin)

Telegram'a (ya da Hermes'in bağlı olduğu herhangi bir kanala) gönderdiğin toplantı ses kaydını:

1. **Yerelde, ücretsiz** Türkçe'ye ince ayarlı Whisper (faster-whisper/CTranslate2) ile yazıya çevirir,
2. özet, kararlar ve yapılacakları **Hermes'in kendi modeliyle** çıkarır (ayrı LLM/API anahtarı yok),
3. görevleri saklar, 4. belirlediğin saatlerde açık görevleri hatırlatır (LLM çağırmayan cron).

## Kurulum

**Windows, tek komut (git gerekmez; kurar, günceller, kendini test eder):**
```powershell
irm https://raw.githubusercontent.com/kayametehan/hermes-meeting-notes/main/get.ps1 | iex
```

**Windows (tek komut, PowerShell):**
```powershell
git clone https://github.com/kayametehan/hermes-meeting-notes; cd hermes-meeting-notes; powershell -ExecutionPolicy Bypass -File .\install.ps1
```
(git yoksa repo sayfasından *Code → Download ZIP*.)

Önkoşul: Hermes Agent kurulu ve Telegram gateway'i çalışıyor.

**Windows (PowerShell):**
```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Times "09:00,18:00" -Deliver telegram
```
**macOS / Linux:** `bash install.sh 09:00,18:00 telegram`

Script: uv'yi kurar → eklentiyi `~/.hermes/plugins/meeting-notes` altına kopyalar ve etkinleştirir →
STT için eklentiye özel Python ortamı + modeli indirir → `stt.provider: tr-whisper` yapar →
hatırlatma cron işlerini kurar. ffmpeg gerekmez (ses çözme PyAV ile).
Sonra `hermes gateway restart`.

Başka cihaz/hesap: klasörü kopyala, aynı scripti çalıştır. Her cihazın verisi kendi
`~/.hermes/plugin-data/meeting-notes/` klasöründedir.

## Kullanım

- Telegram'a kaydı **dosya/ses olarak** gönder. Hermes `meeting_process` aracını çağırır; işlem arka planda
  sürer, bitince özet + görevler aynı sohbete gelir.
- `/yapilacaklar` ya da "yapılacaklarım ne?" → liste. "3 bitti" → tamamlar.
- `hermes meeting process kayit.m4a --transcript-only` → bilgisayarda yazıya çevir (boyut sınırı yok).
- `hermes meeting config model large` → daha ağır/doğru model (GPU önerilir). Seçenekler: `turbo` (varsayılan),
  `large`, `base-turbo` ya da herhangi bir HF/yerel CTranslate2 model yolu.
- `hermes meeting reminders --times 08:30,13:00,18:00` → saatleri yeniden kur (eskileri `hermes cron remove`).

## Modeller

| ad | repo | not |
|---|---|---|
| `turbo` (varsayılan) | `RsGoksel/RsGoksel_ITU_Mainframe-ct2` | large-v3-turbo + Türkçe LoRA; CPU'da da kullanılabilir |
| `large` | `oguzhangokboru/whisper-large-v3-tr` | large-v3 + Türkçe ince ayar; model kartı WER ≈ %8.8 (kendi doğrulama setinde) |
| `base-turbo` | `deepdml/faster-whisper-large-v3-turbo-ct2` | ince ayarsız çok dilli yedek |

Model kartlarındaki WER değerleri geliştiricilerin kendi ölçümüdür; kendi kayıtlarında karşılaştırmadan "en iyi"
diye kabul etme. `hermes meeting config model <ad>` ile değiştirip aynı kaydı dene.

## Sınırlar

- **Telegram bot API 20 MB üstü dosyayı indiremez.** 1-5 saatlik kayıt için önce küçült:
  `ffmpeg -i kayit.m4a -ac 1 -c:a libopus -b:a 16k kayit.ogg` (~7 MB/saat) ya da `hermes meeting process`.
- Süre: GPU'da (CUDA) hızlıdır; CPU'da turbo model kabaca gerçek zamanın 1/3-1 katı sürer. Kesilirse aynı
  dosyayla tekrar çağırınca kaldığı yerden devam eder. RAM: ~1,2 GB / 5 saat ses (ses belleğe alınır).
- Bilgisayar ve Hermes gateway açık olmalı. Konuşmacı ayrımı yok.
