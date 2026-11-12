"""Cron (--no-agent) betiği: açık yapılacakları stdout'a yazar, Hermes teslim eder.
Sadece standart kütüphane; LLM çağırmaz. Açık görev yoksa hiçbir şey yazmaz."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

tasks = store.list_tasks()
if tasks:
    label = sys.argv[1] if len(sys.argv) > 1 else "Hatırlatma"
    print(f"⏰ {label} — {len(tasks)} açık yapılacak\n\n{store.format_tasks(tasks)}")
