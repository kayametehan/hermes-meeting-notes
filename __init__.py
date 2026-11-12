"""meeting-notes — Türkçe toplantı kaydı -> özet + yapılacaklar + hatırlatma (Hermes plugin)."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import engine, pipeline, store

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
_gpu_lock = threading.Lock()  # aynı anda tek ağır transkripsiyon


def _ok(**kw) -> str:
    return json.dumps({"success": True, **kw}, ensure_ascii=False)


def _err(msg: str) -> str:
    return json.dumps({"success": False, "error": msg}, ensure_ascii=False)


# ---------------------------------------------------------------- bildirim
def _notify(text: str) -> None:
    """Bitişi mesajlaşma platformuna iletir (`hermes send`; LLM/gateway gerektirmez)."""
    target = store.config().get("notify_target")
    exe = shutil.which("hermes")
    if not target or not exe:
        return
    try:
        for i in range(0, len(text), 3800):
            subprocess.run([exe, "send", "-q", "-t", target, text[i:i + 3800]], timeout=60,
                           capture_output=True)
    except Exception as e:  # bildirim başarısızlığı işi bozmasın
        log.warning("meeting-notes: bildirim gönderilemedi: %s", e)


# ---------------------------------------------------------------- iş hattı
def run_pipeline(llm, job_id: str, audio: str, title: str, notify: bool) -> dict:
    cfg = store.config()
    mid = job_id
    folder = store.meeting_dir(mid)
    jsonl = folder / "transcript.jsonl"
    try:
        with _gpu_lock:
            store.update_job(job_id, status="transcribing", progress=0.0)
            last = [0.0]

            def prog(p):
                if p - last[0] >= 0.01:
                    last[0] = p
                    store.update_job(job_id, progress=round(p, 3))

            segs = engine.transcribe_file(audio, jsonl, on_progress=prog, cfg=cfg)
        text = engine.segments_to_text(segs)
        (folder / "transcript.txt").write_text(text, encoding="utf-8")
        if not text.strip():
            raise RuntimeError("Kayıtta konuşma algılanamadı.")

        store.update_job(job_id, status="summarizing", progress=1.0)
        data = pipeline.summarize(llm, text, cfg.get("chunk_chars", 14000), title,
                                  on_stage=lambda s: store.update_job(job_id, stage=s))
        md = pipeline.render_markdown(title, data)
        (folder / "summary.md").write_text(md, encoding="utf-8")
        new = store.add_tasks(data.get("tasks", []), meeting=mid)
        store.update_job(job_id, status="done", stage="", summary_path=str(folder / "summary.md"),
                         transcript_path=str(folder / "transcript.txt"), new_tasks=len(new),
                         finished=datetime.now().isoformat(timespec="seconds"))
        if notify:
            _notify(f"✅ Toplantı işlendi: {title}\n\n{md}\nTüm açık görevler için: /yapilacaklar")
        return {"markdown": md, "new_tasks": len(new)}
    except Exception as e:
        log.exception("meeting-notes iş hatası")
        store.update_job(job_id, status="error", error=str(e))
        if notify:
            _notify(f"❌ Toplantı işlenemedi ({title}): {e}\nAynı dosyayla tekrar denersen kaldığı yerden devam eder.")
        raise


# ---------------------------------------------------------------- araçlar
def _make_tools(ctx):
    def meeting_process(args: dict, **_):
        audio = (args.get("audio_path") or "").strip().strip('"')
        if not audio or not Path(audio).expanduser().is_file():
            return _err(f"Ses dosyası bulunamadı: {audio!r}")
        title = (args.get("title") or Path(audio).stem).strip()
        wait = bool(args.get("wait", False))
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        store.update_job(job_id, status="queued", title=title, audio=audio, progress=0.0,
                         started=datetime.now().isoformat(timespec="seconds"))
        audio = str(Path(audio).expanduser())
        if wait:
            r = run_pipeline(ctx.llm, job_id, audio, title, notify=False)
            return _ok(job_id=job_id, status="done", summary_markdown=r["markdown"], new_tasks=r["new_tasks"])

        def bg():
            try:
                run_pipeline(ctx.llm, job_id, audio, title, notify=True)
            except Exception:
                pass
        threading.Thread(target=bg, name=f"meeting-{job_id}", daemon=True).start()
        return _ok(job_id=job_id, status="started",
                   note="Arka planda işleniyor. Bitince özet otomatik gönderilir; durum için meeting_status. "
                        "Kullanıcıya şimdi sadece 'işleme alındı' de, uzun kayıtlarda süre kaydın 1/3'ü–1 katı olabilir.")

    def meeting_status(args: dict, **_):
        js = store.jobs()
        jid = (args.get("job_id") or "").strip()
        if jid:
            return _ok(job=js.get(jid)) if jid in js else _err("Böyle bir iş yok.")
        recent = sorted(js.items(), key=lambda kv: kv[0], reverse=True)[:5]
        return _ok(jobs=[{"job_id": k, **v} for k, v in recent])

    def meeting_tasks(args: dict, **_):
        action = args.get("action", "list")
        if action == "list":
            tasks = store.list_tasks(include_done=bool(args.get("include_done")))
            return _ok(text=store.format_tasks(tasks), tasks=tasks)
        if action == "add":
            new = store.add_tasks([{"action": args.get("text", ""), "owner": args.get("owner", ""),
                                    "due_text": args.get("due", "")}])
            return _ok(added=new) if new else _err("text boş.")
        ref = str(args.get("ref", "")).strip()
        if action in ("done", "undo", "remove") and not ref:
            return _err("ref gerekli (görev id'si veya listedeki sıra numarası).")
        if action == "done":
            t = store.set_done(ref, True)
        elif action == "undo":
            t = store.set_done(ref, False)
        elif action == "remove":
            t = store.remove_task(ref)
        else:
            return _err(f"Bilinmeyen action: {action}")
        if not t:
            return _err("Görev bulunamadı.")
        return _ok(task=t, open=store.format_tasks(store.list_tasks()))

    props = {
        "meeting_process": {
            "audio_path": {"type": "string", "description": "Ses/video dosyasının tam yolu (ogg, m4a, mp3, wav, mp4...)"},
            "title": {"type": "string", "description": "Toplantı başlığı (opsiyonel)"},
            "wait": {"type": "boolean", "description": "true ise bitene kadar bekler (sadece kısa kayıtlar için)"},
        },
        "meeting_status": {"job_id": {"type": "string"}},
        "meeting_tasks": {
            "action": {"type": "string", "enum": ["list", "add", "done", "undo", "remove"]},
            "ref": {"type": "string", "description": "done/undo/remove için: görev id'si ya da açık listedeki sıra no"},
            "text": {"type": "string"}, "owner": {"type": "string"}, "due": {"type": "string"},
            "include_done": {"type": "boolean"},
        },
    }
    desc = {
        "meeting_process": "Bir toplantı ses kaydını yerelde Türkçe Whisper ile yazıya çevirir, özet/karar/yapılacakları çıkarır ve görev listesine ekler. Uzun kayıtlar arka planda çalışır; bitince özet kullanıcıya otomatik gönderilir.",
        "meeting_status": "Toplantı işleme işlerinin durumunu ve ilerlemesini gösterir.",
        "meeting_tasks": "Toplantılardan çıkan yapılacaklar listesini gösterir/ekler/tamamlar/siler.",
    }
    required = {"meeting_process": ["audio_path"], "meeting_status": [], "meeting_tasks": []}
    fns = {"meeting_process": meeting_process, "meeting_status": meeting_status, "meeting_tasks": meeting_tasks}
    emoji = {"meeting_process": "🎙️", "meeting_status": "⏳", "meeting_tasks": "📝"}
    for name, fn in fns.items():
        ctx.register_tool(
            name=name, toolset="meeting-notes",
            schema={"name": name, "description": desc[name],
                    "parameters": {"type": "object", "properties": props[name], "required": required[name]}},
            handler=fn, emoji=emoji[name])


# ---------------------------------------------------------------- STT sağlayıcısı
def _make_stt_provider():
    from agent.transcription_provider import TranscriptionProvider

    class TurkishWhisper(TranscriptionProvider):
        @property
        def name(self) -> str:
            return "tr-whisper"

        @property
        def display_name(self) -> str:
            return "Türkçe Whisper (yerel)"

        def is_available(self) -> bool:
            return engine.is_ready()

        def list_models(self):
            return [{"id": k, "display": v} for k, v in engine.MODELS.items()]

        def default_model(self):
            return "turbo"

        def transcribe(self, file_path, *, model=None, language=None, **extra):
            try:
                cfg = dict(store.config())
                if model:
                    cfg["model"] = model
                if language:
                    cfg["language"] = language.split("-")[0]
                out = store.data_dir() / "stt-tmp" / (uuid.uuid4().hex + ".jsonl")
                out.parent.mkdir(parents=True, exist_ok=True)
                with _gpu_lock:
                    segs = engine.transcribe_file(file_path, out, cfg=cfg)
                out.unlink(missing_ok=True)
                return {"success": True, "transcript": engine.segments_to_text(segs, timestamps=False),
                        "provider": self.name}
            except Exception as e:
                return {"success": False, "transcript": "", "provider": self.name, "error": str(e)}

    return TurkishWhisper()


# ---------------------------------------------------------------- CLI: hermes meeting ...
def _setup_cli(p):
    sub = p.add_subparsers(dest="mcmd")
    sub.add_parser("setup", help="STT ortamını kur ve modeli indir")
    sub.add_parser("doctor", help="Kurulumu kontrol et")
    pr = sub.add_parser("process", help="Bir ses dosyasını doğrudan işle (Telegram 20 MB sınırı olmadan)")
    pr.add_argument("audio")
    pr.add_argument("--title", default="")
    pr.add_argument("--transcript-only", action="store_true", help="Sadece yazıya çevir (LLM yok)")
    rm = sub.add_parser("reminders", help="Günlük hatırlatma cron işlerini kur")
    rm.add_argument("--times", default="09:00,18:00", help="Virgüllü saatler, örn 09:00,13:00,18:00")
    rm.add_argument("--deliver", default="telegram", help="Teslim hedefi (telegram, discord, telegram:chat_id ...)")
    cf = sub.add_parser("config", help="Ayar göster/değiştir")
    cf.add_argument("key", nargs="?")
    cf.add_argument("value", nargs="?")
    sub.add_parser("tasks", help="Açık görevleri listele")


def _handle_cli(args):
    cmd = getattr(args, "mcmd", None)
    if cmd == "setup":
        engine.setup(print)
        print("Model indiriliyor (ilk seferde birkaç yüz MB-GB)...")
        engine.prefetch_model(print)
        print("Hazır. Model:", engine.resolve_model(store.config()))
    elif cmd == "doctor":
        print("venv hazır:", engine.is_ready(), "|", engine.venv_python())
        print("ffmpeg gerekmiyor (PyAV ile çözülüyor).")
        print("hermes send:", bool(shutil.which("hermes")))
        print("ayarlar:", json.dumps(store.config(), ensure_ascii=False))
    elif cmd == "tasks":
        print(store.format_tasks(store.list_tasks()))
    elif cmd == "config":
        if not args.key:
            print(json.dumps(store.config(), ensure_ascii=False, indent=2))
        else:
            val = args.value
            try:
                val = json.loads(val)
            except (TypeError, ValueError):
                pass
            print(json.dumps(store.save_config({args.key: val}), ensure_ascii=False, indent=2))
    elif cmd == "process":
        src = str(Path(args.audio).expanduser())
        if args.transcript_only:
            out = store.data_dir() / "stt-tmp" / (uuid.uuid4().hex + ".jsonl")
            out.parent.mkdir(parents=True, exist_ok=True)
            segs = engine.transcribe_file(src, out, on_progress=lambda p: print(f"\r%{p*100:5.1f}", end="", flush=True))
            print()
            print(engine.segments_to_text(segs))
        else:
            print("CLI'dan özet için Hermes'e dosyayı ver: hermes chat -q \"meeting_process ile şu kaydı işle: "
                  f"{src}\" (LLM erişimi Hermes oturumundan gelir). Sadece yazı için --transcript-only kullan.")
    elif cmd == "reminders":
        exe = shutil.which("hermes") or "hermes"
        # Cron betikleri <hermes home>/scripts/ altında olmak zorunda: ince bir sarmalayıcı yazıyoruz.
        scripts = store.hermes_home() / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        wrapper = scripts / "meeting_notes_reminder.py"
        wrapper.write_text(
            "import runpy, sys\n"
            f"sys.argv = [sys.argv[0]] + sys.argv[1:]\n"
            f"runpy.run_path({str(HERE / 'reminder.py')!r}, run_name='__main__')\n", encoding="utf-8")
        failed = 0
        for t in [x.strip() for x in args.times.split(",") if x.strip()]:
            hh, mm = t.split(":")
            cmdline = [exe, "cron", "create", f"{int(mm)} {int(hh)} * * *", "--no-agent",
                       "--script", wrapper.name, "--deliver", args.deliver,
                       "--name", f"meeting-notes hatırlatma {t}"]
            print("$", " ".join(cmdline))
            failed += subprocess.run(cmdline).returncode != 0
        if failed:
            print(f"UYARI: {failed} cron işi oluşturulamadı.")
            return 1
        print("Tamam. Silmek için: hermes cron list / hermes cron remove <id>")
    else:
        print("Alt komutlar: setup, doctor, process, reminders, config, tasks")


# ---------------------------------------------------------------- giriş noktası
def register(ctx):
    _make_tools(ctx)
    try:
        ctx.register_transcription_provider(_make_stt_provider())
    except Exception as e:
        log.warning("meeting-notes: STT sağlayıcısı kaydedilemedi: %s", e)
    ctx.register_cli_command(name="meeting", help="Toplantı notları: kurulum, hatırlatma, ayarlar",
                             setup_fn=_setup_cli, handler_fn=_handle_cli)
    ctx.register_command(name="yapilacaklar",
                         handler=lambda raw: "📝 Açık yapılacaklar:\n" + store.format_tasks(store.list_tasks()),
                         description="Toplantılardan çıkan açık yapılacakları göster")
    skill = HERE / "skill" / "SKILL.md"
    if skill.exists():
        ctx.register_skill("meeting-notes", skill,
                           description="Toplantı kaydı geldiğinde özet, görev ve hatırlatma akışı")
