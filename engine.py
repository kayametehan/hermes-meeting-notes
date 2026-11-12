"""STT motoru: plugin'e özel sanal ortamı kurar ve worker.py'yi çalıştırır.
Neden ayrı ortam? Hermes'in Python sürümünden/paketlerinden bağımsız kalır (ctranslate2
tekerlekleri her Python sürümünde yok) ve Windows/macOS/Linux'ta aynı çalışır."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import store

MODELS = {
    # Whisper large-v3-turbo tabanlı Türkçe ince ayar (CPU'da bile makul hız)
    "turbo": "RsGoksel/RsGoksel_ITU_Mainframe-ct2",
    # Whisper large-v3 tabanlı Türkçe ince ayar (daha ağır; GPU önerilir)
    "large": "oguzhangokboru/whisper-large-v3-tr",
    # İnce ayarsız resmi çok dilli yedek
    "base-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
}
REQUIREMENTS = ["faster-whisper>=1.1,<2", "av>=11,<17"]
IS_WIN = os.name == "nt"


def venv_dir() -> Path:
    return store.data_dir() / "venv"


def venv_python() -> Path:
    return venv_dir() / ("Scripts/python.exe" if IS_WIN else "bin/python")


def resolve_model(cfg: dict) -> str:
    m = cfg.get("model", "turbo")
    return MODELS.get(m, m)  # takma ad ya da doğrudan HF repo / yerel klasör


def is_ready() -> bool:
    p = venv_python()
    if not p.exists():
        return False
    r = subprocess.run([str(p), "-c", "import faster_whisper, av"], capture_output=True)
    return r.returncode == 0


def _run(cmd, log):
    log("$ " + " ".join(str(c) for c in cmd))
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Komut başarısız ({r.returncode}): {' '.join(map(str, cmd))}\n{r.stdout[-1500:]}")


def _find_python():
    """uv yoksa: 3.10–3.13 arası bir Python bul."""
    cands = []
    if IS_WIN and shutil.which("py"):
        cands += [["py", f"-3.{v}"] for v in (12, 11, 13, 10)]
    cands += [[f"python3.{v}"] for v in (12, 11, 13, 10)] + [["python3"], ["python"]]
    for c in cands:
        if not shutil.which(c[0]):
            continue
        r = subprocess.run(c + ["-c", "import sys;print(sys.version_info[:2]>=(3,10) and sys.version_info[:2]<=(3,13))"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip() == "True":
            return c
    return None


def setup(log=print) -> None:
    """Sanal ortamı kurar. İdempotent."""
    if is_ready():
        log("Ortam hazır.")
        return
    vd = venv_dir()
    uv = shutil.which("uv")
    if uv:
        _run([uv, "venv", "--python", "3.12", "--seed", str(vd)], log)
        _run([uv, "pip", "install", "--python", str(venv_python())] + REQUIREMENTS, log)
    else:
        py = _find_python()
        if not py:
            raise RuntimeError("Python 3.10-3.13 bulunamadı ve 'uv' kurulu değil. "
                               "Windows: winget install astral-sh.uv   |   macOS/Linux: brew install uv")
        _run(py + ["-m", "venv", str(vd)], log)
        _run([str(venv_python()), "-m", "pip", "install", "-q", "--upgrade", "pip"], log)
        _run([str(venv_python()), "-m", "pip", "install", "-q"] + REQUIREMENTS, log)
    if not is_ready():
        raise RuntimeError("Ortam kuruldu ama faster-whisper içe aktarılamıyor.")
    log("Ortam kuruldu.")


def prefetch_model(log=print) -> None:
    """Modeli önceden indirir (ilk kullanımda beklememek için)."""
    model = resolve_model(store.config())
    code = ("from huggingface_hub import snapshot_download;"
            f"print(snapshot_download({model!r}))")
    _run([str(venv_python()), "-c", code], log)


def transcribe_file(audio: str, out_jsonl: Path, on_progress=None, cfg: dict | None = None) -> list:
    """Sesi metne çevirir; segment listesi döndürür. Kaldığı yerden devam eder."""
    cfg = cfg or store.config()
    if not is_ready():
        setup(lambda m: None)
    cmd = [str(venv_python()), str(Path(__file__).with_name("worker.py")), audio, str(out_jsonl),
           "--model", resolve_model(cfg), "--language", cfg.get("language", "tr"),
           "--device", cfg.get("device", "auto"), "--beam", str(cfg.get("beam_size", 5))]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", HF_HUB_DISABLE_TELEMETRY="1")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", env=env)
    tail = []
    for line in p.stdout:
        line = line.rstrip()
        if line.startswith("PROGRESS ") and on_progress:
            try:
                on_progress(float(line.split()[1]))
            except ValueError:
                pass
        else:
            tail = (tail + [line])[-25:]
    if p.wait() != 0:
        raise RuntimeError("Transkripsiyon başarısız:\n" + "\n".join(tail[-12:]))
    segs = []
    with open(out_jsonl, encoding="utf-8") as f:
        for line in f:
            try:
                segs.append(json.loads(line))
            except ValueError:
                pass
    return segs


def ts(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def segments_to_text(segs: list, timestamps: bool = True) -> str:
    return "\n".join((f"[{ts(s['start'])}] " if timestamps else "") + s["text"] for s in segs)
