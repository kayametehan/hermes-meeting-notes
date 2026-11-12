"""Cron (--no-agent) betiği: açık yapılacakları stdout'a yazar, Hermes teslim eder.
Sadece standart kütüphane; LLM çağırmaz. Açık görev yoksa hiçbir şey yazmaz."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

tasks = store.list_tasks()
if tasks:
    label = sys.argv[1] if len(sys.argv) > 1 else "Hatırlatma"
    text = f"⏰ {label} — {len(tasks)} açık yapılacak\n\n{store.format_tasks(tasks)}"
    print(text)
    # Telegram yoksa masaüstü bildirimi (en iyi çaba)
    try:
        import subprocess
        short = f"{len(tasks)} açık yapılacak: " + "; ".join(t["text"] for t in tasks[:3]).replace("'", " ")
        if sys.platform == "win32":
            ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
                  "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
                  "$x.GetElementsByTagName('text')[0].AppendChild($x.CreateTextNode('Toplanti hatirlatma'))|Out-Null;"
                  f"$x.GetElementsByTagName('text')[1].AppendChild($x.CreateTextNode('{short[:200]}'))|Out-Null;"
                  "$n=[Windows.UI.Notifications.ToastNotification]::new($x);"
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($n)")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=20)
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e", f'display notification "{short[:200]}" with title "Toplanti hatirlatma"'],
                           capture_output=True, timeout=10)
    except Exception:
        pass
