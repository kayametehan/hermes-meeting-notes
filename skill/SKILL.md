---
name: meeting-notes
description: "Use when the user sends a meeting audio recording (or asks about meeting tasks/reminders). Runs the Turkish STT + summary + task pipeline."
---

# Toplantı kaydı akışı

Kullanıcı bir toplantı ses kaydı (ses notu, m4a/mp3/ogg/wav dosyası, video) gönderdiğinde:

1. Dosyanın yolunu mesajdan/ekten al. Metne çevrilmiş kısa bir ses notu gelmişse ve bu bir toplantı
   kaydı değilse, normal konuşma gibi cevap ver; araç çağırma.
2. `meeting_process(audio_path=..., title=...)` çağır. Varsayılan arka planda çalışır. Yanıttaki `note`
   alanını oku: otomatik bildirim yoksa (Telegram bağlı değil) kullanıcıya "işleme aldım, durumu sorabilirsin"
   de; sorunca `meeting_status` çağır, bitmişse `summary_markdown`'ı göster. Aynı kaydı tekrar tekrar işleme.
3. İlerleme sorulursa `meeting_status` kullan. Hata varsa gerçek hata mesajını ilet; aynı dosyayla
   yeniden çağırmak kaldığı yerden devam eder.
4. Telegram 20 MB'tan büyük dosyayı indiremez. Dosya gelmediyse kullanıcıya şunu öner:
   `ffmpeg -i kayit.m4a -ac 1 -c:a libopus -b:a 16k kayit.ogg` ile küçültüp tekrar göndermek ya da
   bilgisayarda `hermes meeting process <dosya> --transcript-only` kullanmak.

## Görevler

- "Yapılacaklarım ne?" -> `meeting_tasks(action="list")`, numaralı listeyi olduğu gibi göster.
- "3 numara bitti" -> `meeting_tasks(action="done", ref="3")`.
- Kullanıcı elle görev eklemek isterse `action="add"`.

## Hatırlatma

Hatırlatmalar LLM çağırmayan cron işleridir. Kurulum: `hermes meeting reminders --times 09:00,18:00
--deliver telegram`. Saat değişikliği isterse bu komutu yeni saatlerle çalıştır ve eski işleri
`hermes cron list` / `hermes cron remove <id>` ile sil.

## Dürüstlük

Transkript otomatik üretilir; isim ve sayılarda hata olabilir. Özetteki önemli bir karar şüpheliyse
`transcript.txt` dosyasına bak (yol `meeting_status` çıktısında).
