"""Plugin'in kendi sanal ortamında çalışır (faster-whisper burada kurulu).
Kullanım: python worker.py <ses> <çıktı.jsonl> --model M --language tr --device auto --beam 5

- Her segmenti çıktı dosyasına anında yazar (JSON satırı: start,end,text).
- Kesilirse aynı komut tekrar çalıştırıldığında kaldığı yerden devam eder.
- İlerlemeyi stdout'a "PROGRESS <0-1>" olarak basar.
"""
import argparse
import json
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("out")
    ap.add_argument("--model", required=True)
    ap.add_argument("--language", default="tr")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--beam", type=int, default=5)
    a = ap.parse_args()

    import ctranslate2
    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio

    cuda = ctranslate2.get_cuda_device_count() > 0
    device = a.device if a.device != "auto" else ("cuda" if cuda else "cpu")
    compute = "float16" if device == "cuda" else "int8"
    print(f"INFO device={device} compute={compute} model={a.model}", flush=True)

    resume_from = 0.0
    if os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as f:
            for line in f:
                try:
                    resume_from = max(resume_from, json.loads(line)["end"])
                except (ValueError, KeyError):
                    pass

    model = WhisperModel(a.model, device=device, compute_type=compute)
    audio = decode_audio(a.audio, sampling_rate=16000)
    duration = len(audio) / 16000.0
    print(f"INFO duration={duration:.0f}s resume_from={resume_from:.0f}s", flush=True)

    kw = dict(language=a.language, beam_size=a.beam, vad_filter=True,
              condition_on_previous_text=False)
    if resume_from > 1:
        kw["clip_timestamps"] = [resume_from]
    segments, _ = model.transcribe(audio, **kw)
    with open(a.out, "a", encoding="utf-8") as f:
        for s in segments:
            text = s.text.strip()
            if text:
                f.write(json.dumps({"start": round(s.start, 2), "end": round(s.end, 2), "text": text},
                                   ensure_ascii=False) + "\n")
                f.flush()
            print(f"PROGRESS {min(1.0, s.end / duration):.4f}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    sys.exit(main())
