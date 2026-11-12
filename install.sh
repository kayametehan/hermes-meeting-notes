#!/usr/bin/env bash
# macOS / Linux kurulumu:  bash install.sh [09:00,18:00] [telegram]
set -euo pipefail
TIMES="${1:-09:00,18:00}"; DELIVER="${2:-local}"
command -v hermes >/dev/null || { echo "hermes bulunamadı"; exit 1; }
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
HH="${HERMES_HOME:-$HOME/.hermes}"; DEST="$HH/plugins/meeting-notes"
rm -rf "$DEST"; mkdir -p "$HH/plugins"; cp -R "$(cd "$(dirname "$0")" && pwd)" "$DEST"; rm -rf "$DEST/.git"
hermes plugins enable meeting-notes
hermes meeting setup
hermes config set stt.provider tr-whisper
hermes meeting reminders --times "$TIMES" --deliver "$DELIVER"
echo "Tamam."
