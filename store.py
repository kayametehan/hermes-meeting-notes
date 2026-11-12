"""Disk durumu: görevler, toplantılar, işler. Sadece standart kütüphane (hatırlatıcı betiği de kullanır)."""
import json
import os
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

NAME = "meeting-notes"
_lock = threading.RLock()


def hermes_home() -> Path:
    """Hermes veri klasörü. Windows varsayılanı %LOCALAPPDATA%\\hermes, diğerlerinde ~/.hermes."""
    env = (os.environ.get("HERMES_HOME") or "").strip()
    if env:
        return Path(os.path.expandvars(env)).expanduser()
    try:  # Hermes içinden çalışırken kesin cevap
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        pass
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "hermes"
        legacy = Path.home() / ".hermes"
        return base if base.exists() or not legacy.exists() else legacy
    return Path.home() / ".hermes"


def data_dir() -> Path:
    d = hermes_home() / "plugin-data" / NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def config() -> dict:
    cfg = _read(Path(__file__).with_name("config.default.json"), {})
    cfg.update(_read(data_dir() / "config.json", {}))
    return cfg


def save_config(patch: dict) -> dict:
    with _lock:
        cur = _read(data_dir() / "config.json", {})
        cur.update(patch)
        _write(data_dir() / "config.json", cur)
    return config()


# ------------------------------------------------------------------ görevler
def _tasks_path() -> Path:
    return data_dir() / "tasks.json"


def list_tasks(include_done: bool = False) -> list:
    with _lock:
        tasks = _read(_tasks_path(), [])
    return tasks if include_done else [t for t in tasks if not t.get("done")]


def add_tasks(items: list, meeting: str = "") -> list:
    with _lock:
        tasks = _read(_tasks_path(), [])
        new = []
        for it in items:
            text = (it.get("action") or it.get("text") or "").strip()
            if not text:
                continue
            t = {"id": uuid.uuid4().hex[:6], "text": text, "owner": (it.get("owner") or "").strip(),
                 "due": (it.get("due") or it.get("due_text") or "").strip(),
                 "due_iso": (it.get("due_iso") or "").strip(), "meeting": meeting, "done": False,
                 "created": datetime.now().isoformat(timespec="minutes")}
            tasks.append(t)
            new.append(t)
        _write(_tasks_path(), tasks)
    return new


def set_done(ref: str, done: bool = True):
    """ref: görev id'si veya açık listedeki 1-tabanlı sıra numarası."""
    with _lock:
        tasks = _read(_tasks_path(), [])
        open_ = [t for t in tasks if not t.get("done")]
        target = None
        if ref.isdigit() and 1 <= int(ref) <= len(open_):
            target = open_[int(ref) - 1]
        else:
            target = next((t for t in tasks if t["id"] == ref), None)
        if target is None:
            return None
        target["done"] = done
        _write(_tasks_path(), tasks)
        return target


def remove_task(ref: str):
    with _lock:
        tasks = _read(_tasks_path(), [])
        open_ = [t for t in tasks if not t.get("done")]
        target = open_[int(ref) - 1] if ref.isdigit() and 1 <= int(ref) <= len(open_) else \
            next((t for t in tasks if t["id"] == ref), None)
        if target is None:
            return None
        tasks.remove(target)
        _write(_tasks_path(), tasks)
        return target


def format_tasks(tasks: list) -> str:
    if not tasks:
        return "Açık yapılacak yok."
    lines = []
    for i, t in enumerate(tasks, 1):
        extra = ", ".join(x for x in (t.get("owner"), t.get("due")) if x)
        lines.append(f"{i}. {t['text']}" + (f" ({extra})" if extra else ""))
    return "\n".join(lines)


# ------------------------------------------------------------------ işler / toplantılar
def jobs() -> dict:
    return _read(data_dir() / "jobs.json", {})


def update_job(job_id: str, **fields) -> dict:
    with _lock:
        js = jobs()
        js.setdefault(job_id, {}).update(fields)
        _write(data_dir() / "jobs.json", js)
        return js[job_id]


def meeting_dir(meeting_id: str) -> Path:
    d = data_dir() / "meetings" / meeting_id
    d.mkdir(parents=True, exist_ok=True)
    return d
