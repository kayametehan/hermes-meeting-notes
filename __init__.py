"""meeting-notes — Türkçe toplantı kaydı -> özet + yapılacaklar + hatırlatma (Hermes plugin)."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import os
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
def run_pipeline(llm, job_id: str, audio: str, title: str, notify: bool, transcript_jsonl: str = "") -> dict:
    cfg = store.config()
    mid = job_id
    folder = store.meeting_dir(mid)
    jsonl = folder / "transcript.jsonl"
    try:
        if transcript_jsonl and Path(transcript_jsonl).is_file():
            segs = [json.loads(l) for l in Path(transcript_jsonl).read_text(encoding="utf-8").splitlines() if l.strip()]
            jsonl.write_text(Path(transcript_jsonl).read_text(encoding="utf-8"), encoding="utf-8")
        else:
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
        tjson = (args.get("transcript_path") or "").strip()
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        store.update_job(job_id, status="queued", title=title, audio=audio, progress=0.0,
                         started=datetime.now().isoformat(timespec="seconds"))
        audio = str(Path(audio).expanduser())
        if wait:
            r = run_pipeline(ctx.llm, job_id, audio, title, notify=False, transcript_jsonl=tjson)
            return _ok(job_id=job_id, status="done", summary_markdown=r["markdown"], new_tasks=r["new_tasks"])

        def bg():
            try:
                run_pipeline(ctx.llm, job_id, audio, title, notify=True)
            except Exception:
                pass
        threading.Thread(target=bg, name=f"meeting-{job_id}", daemon=True).start()
        if store.config().get("notify_target"):
            note = ("Arka planda işleniyor. Bitince özet otomatik gönderilir; durum için meeting_status. "
                    "Kullanıcıya şimdi sadece 'işleme alındı' de.")
        else:
            note = ("Arka planda işleniyor. Otomatik bildirim YOK: kullanıcıya bitince haber verilmeyecek. "
                    "Kullanıcı sorunca meeting_status çağır; status=done ise summary_markdown alanını göster. "
                    "Uzun kayıtlarda süre kaydın 1/3'ü ile 1 katı arasında olabilir.")
        return _ok(job_id=job_id, status="started", note=note)

    def meeting_status(args: dict, **_):
        js = store.jobs()
        jid = (args.get("job_id") or "").strip()
        def with_summary(k, v):
            v = {"job_id": k, **v}
            sp = v.get("summary_path")
            if v.get("status") == "done" and sp and Path(sp).is_file():
                v["summary_markdown"] = Path(sp).read_text(encoding="utf-8")
            return v
        if jid:
            return _ok(job=with_summary(jid, js[jid])) if jid in js else _err("Böyle bir iş yok.")
        recent = sorted(js.items(), key=lambda kv: kv[0], reverse=True)[:5]
        return _ok(jobs=[with_summary(k, v) for k, v in recent])

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
            "transcript_path": {"type": "string", "description": "Hazır transkript (jsonl) yolu; verilirse yazıya çevirme atlanır"},
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


# ---------------------------------------------------------------- Gelen kutusu klasörü (bot)
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac", ".flac", ".mp4", ".webm", ".wma", ".amr", ".mkv"}
_watch_started = False


def inbox_dir() -> Path:
    cfg = store.config()
    d = Path(os.path.expandvars(cfg.get("inbox_dir") or "")).expanduser() if cfg.get("inbox_dir") \
        else Path.home() / "Toplanti-Inbox"
    for sub in ("", "isleniyor", "islenen", "ozetler", "hatali"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def _toast(title: str, msg: str) -> None:
    """Masaüstü bildirimi (en iyi çaba; başarısız olursa sessiz geçer)."""
    title, msg = title.replace("'", " ")[:80], msg.replace("'", " ").replace("\n", " ")[:200]
    try:
        if sys.platform == "win32":
            ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
                  "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
                  f"$x.GetElementsByTagName('text')[0].AppendChild($x.CreateTextNode('{title}'))|Out-Null;"
                  f"$x.GetElementsByTagName('text')[1].AppendChild($x.CreateTextNode('{msg}'))|Out-Null;"
                  "$n=[Windows.UI.Notifications.ToastNotification]::new($x);"
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($n)")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=20)
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
                           capture_output=True, timeout=10)
    except Exception:
        pass


def _write_tasks_file(inbox: Path) -> None:
    body = "# Açık yapılacaklar\n\n" + store.format_tasks(store.list_tasks()) + "\n"
    (inbox / "Yapilacaklar.md").write_text(body, encoding="utf-8")


def _process_inbox_file(llm, inbox: Path, f: Path) -> None:
    claimed = inbox / "isleniyor" / f.name
    try:
        os.replace(f, claimed)  # atomik sahiplenme: birden fazla süreç aynı dosyayı işlemez
    except OSError:
        return  # başka süreç aldı ya da dosya hâlâ kilitli
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    title = f.stem
    store.update_job(job_id, status="queued", title=title, audio=str(claimed), progress=0.0,
                     started=datetime.now().isoformat(timespec="seconds"), source="inbox")
    try:
        r = run_pipeline(llm, job_id, str(claimed), title, notify=bool(store.config().get("notify_target")))
        (inbox / "ozetler" / f"{title}.md").write_text(r["markdown"], encoding="utf-8")
        os.replace(claimed, inbox / "islenen" / f.name)
        _write_tasks_file(inbox)
        _toast("Toplantı işlendi", f"{title}: {r['new_tasks']} yeni görev. Özet: ozetler klasöründe")
    except Exception as e:
        log.exception("meeting-notes inbox hatası")
        try:
            os.replace(claimed, inbox / "hatali" / f.name)
        except OSError:
            pass
        (inbox / "hatali" / f"{title}.HATA.txt").write_text(str(e), encoding="utf-8")
        _toast("Toplantı işlenemedi", f"{title}: {str(e)[:120]}")


def _watch_loop(llm) -> None:
    seen: dict = {}
    while True:
        try:
            inbox = inbox_dir()
            for f in sorted(inbox.iterdir()):
                if not f.is_file() or f.suffix.lower() not in AUDIO_EXT or f.name.startswith((".", "~")):
                    continue
                st = f.stat()
                sig = (st.st_size, st.st_mtime)
                if seen.get(f) != sig:  # boyut/zaman değişti: kopyalama sürüyor olabilir
                    seen[f] = sig
                    continue
                if time.time() - st.st_mtime < 10:
                    continue
                seen.pop(f, None)
                _process_inbox_file(llm, inbox, f)
        except Exception:
            log.exception("meeting-notes izleyici hatası")
        time.sleep(15)


def start_watcher(llm) -> None:
    global _watch_started
    if _watch_started or not store.config().get("watch_inbox", False):
        return
    _watch_started = True
    inbox_dir()
    threading.Thread(target=_watch_loop, args=(llm,), name="meeting-inbox", daemon=True).start()
    log.info("meeting-notes: gelen kutusu izleniyor: %s", inbox_dir())


# ---------------------------------------------------------------- Bağımsız izleyici: hermes meeting watch
def _latest_job_for(audio: str):
    items = [(k, v) for k, v in store.jobs().items() if v.get("audio") == audio]
    return sorted(items, key=lambda kv: kv[0])[-1] if items else (None, None)


def _handle_with_hermes(inbox: Path, f: Path, exe: str) -> None:
    """Sahiplen -> STT (bu süreçte, zaman aşımı yok) -> özet için Hermes'i çağır -> sonuçları yerleştir."""
    claimed = inbox / "isleniyor" / f.name
    try:
        os.replace(f, claimed)
    except OSError:
        return
    title = f.stem
    print(f"[{datetime.now():%H:%M:%S}] İşleniyor: {f.name}", flush=True)
    try:
        jsonl = store.data_dir() / "stt-tmp" / (f.name + ".jsonl")
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        engine.transcribe_file(str(claimed), jsonl, cfg=store.config())
        prompt = ("meeting_process aracını şu argümanlarla çağır: "
                  f"audio_path={json.dumps(str(claimed))}, transcript_path={json.dumps(str(jsonl))}, wait=true. "
                  "Başka hiçbir şey yapma; araç bitince sadece 'tamam' yaz.")
        r = subprocess.run([exe, "chat", "-q", prompt], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=engine.clean_env())
        _, job = _latest_job_for(str(claimed))
        if not job or job.get("status") != "done":
            err = (job or {}).get("error") or (r.stdout + r.stderr)[-800:] or "Hermes özet aracını çağırmadı."
            raise RuntimeError(err)
        sp = Path(job["summary_path"])
        (inbox / "ozetler" / f"{title}.md").write_text(sp.read_text(encoding="utf-8"), encoding="utf-8")
        os.replace(claimed, inbox / "islenen" / f.name)
        _write_tasks_file(inbox)
        jsonl.unlink(missing_ok=True)
        _toast("Toplantı işlendi", f"{title}: {job.get('new_tasks', 0)} yeni görev. Özet: ozetler klasöründe")
        print(f"[{datetime.now():%H:%M:%S}] Bitti: {title}", flush=True)
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] HATA {title}: {e}", flush=True)
        try:
            os.replace(claimed, inbox / "hatali" / f.name)
        except OSError:
            pass
        (inbox / "hatali" / f"{title}.HATA.txt").write_text(str(e), encoding="utf-8")
        _toast("Toplantı işlenemedi", f"{title}: {str(e)[:120]}")


def watch_forever() -> None:
    exe = shutil.which("hermes") or "hermes"
    inbox = inbox_dir()
    print(f"Gelen kutusu izleniyor: {inbox}  (Ctrl+C ile durdur)", flush=True)
    seen: dict = {}
    # Önceki çalışmadan yarım kalanları geri al
    for f in (inbox / "isleniyor").glob("*"):
        if f.is_file():
            os.replace(f, inbox / f.name)
    while True:
        try:
            for f in sorted(inbox.iterdir()):
                if not f.is_file() or f.suffix.lower() not in AUDIO_EXT or f.name.startswith((".", "~")):
                    continue
                st = f.stat()
                sig = (st.st_size, st.st_mtime)
                if seen.get(f) != sig:
                    seen[f] = sig
                    continue
                if time.time() - st.st_mtime < 10:
                    continue
                seen.pop(f, None)
                _handle_with_hermes(inbox, f, exe)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("izleyici hatası:", e, flush=True)
        time.sleep(15)


def _install_autostart() -> None:
    exe = shutil.which("hermes")
    if not exe:
        print("hermes yolu bulunamadı.")
        return
    if sys.platform == "win32":
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        startup.mkdir(parents=True, exist_ok=True)
        vbs = startup / "HermesToplantiIzleyici.vbs"
        vbs.write_text('CreateObject("WScript.Shell").Run """' + exe + '"" meeting watch", 0, False\r\n', encoding="ascii")
        print("Otomatik başlatma eklendi:", vbs)
        flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
        subprocess.Popen([exe, "meeting", "watch"], creationflags=flags, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        print("İzleyici şimdi de arka planda başlatıldı.")
    else:
        print("Bu sistemde elle başlat:  nohup hermes meeting watch > /tmp/meeting-watch.log 2>&1 &")


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
    rm.add_argument("--deliver", default="local", help="Teslim hedefi: local (masaüstü bildirimi + kayıt), telegram, telegram:chat_id ...")
    cf = sub.add_parser("config", help="Ayar göster/değiştir")
    cf.add_argument("key", nargs="?")
    cf.add_argument("value", nargs="?")
    sub.add_parser("tasks", help="Açık görevleri listele")
    sub.add_parser("watch", help="Gelen kutusunu izle ve kayıtları kendiliğinden işle (arka plan botu)")
    sub.add_parser("autostart", help="Windows açılışında izleyiciyi otomatik başlat ve şimdi başlat")
    sub.add_parser("inbox", help="Gelen kutusu klasörünü göster/oluştur ve aç")


def _handle_cli(args):
    cmd = getattr(args, "mcmd", None)
    if cmd == "setup":
        engine.setup(print)
        print("Model indiriliyor (ilk seferde birkaç yüz MB-GB)...")
        engine.prefetch_model(print)
        print("Hazır. Model:", engine.resolve_model(store.config()))
    elif cmd == "doctor":
        err = engine.import_error()
        print("venv hazır:", not err, "|", engine.venv_python())
        if err:
            print("içe aktarma hatası:\n" + err)
        print("ffmpeg gerekmiyor (PyAV ile çözülüyor).")
        print("hermes send:", bool(shutil.which("hermes")))
        print("ayarlar:", json.dumps(store.config(), ensure_ascii=False))
    elif cmd == "tasks":
        print(store.format_tasks(store.list_tasks()))
    elif cmd == "watch":
        try:
            watch_forever()
        except KeyboardInterrupt:
            print("Durduruldu.")
    elif cmd == "autostart":
        _install_autostart()
    elif cmd == "inbox":
        d = inbox_dir()
        print("Gelen kutusu:", d)
        print("Kayıtları bu klasöre at. Özetler: ozetler\\ , görev listesi: Yapilacaklar.md")
        try:
            if sys.platform == "win32":
                os.startfile(str(d))  # noqa
        except Exception:
            pass
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
        # Idempotent: önceki meeting-notes cron işlerini sil, sonra yeniden kur
        lst = subprocess.run([exe, "cron", "list", "--all"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        last_id = None
        for line in lst.splitlines():
            m = re.match(r"^\s*([0-9a-f]{12})\s+\[", line)
            if m:
                last_id = m.group(1)
                continue
            m = re.match(r"^\s*Name:\s+(.*)$", line)
            if m and last_id and m.group(1).strip().startswith("meeting-notes"):
                subprocess.run([exe, "cron", "remove", last_id], capture_output=True)
                print("Eski iş silindi:", last_id)
                last_id = None
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
        start_watcher(ctx.llm)
    except Exception as e:
        log.warning("meeting-notes: izleyici başlatılamadı: %s", e)
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
