#!/bin/zsh
# Runs `cinekahani publish run`: uploads approved, scheduled items that are ready. Safe to run often.
# YouTube holds scheduled videos itself; Instagram Reels are posted by the first run after their time.
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
source .venv/bin/activate
mkdir -p logs
echo "=== $(date) ===" >>logs/publish.log
out=$(cinekahani publish run 2>&1); code=$?
echo "$out" >>logs/publish.log
if echo "$out" | grep -q "^uploaded\|^scheduled"; then
  osascript -e "display notification \"$(echo "$out" | grep -c '^uploaded\|^scheduled') item(s) sent\" with title \"Cine Kahani\""
fi
if [ $code -ne 0 ]; then
  osascript -e 'display notification "An upload failed. See logs/publish.log" with title "Cine Kahani"'
fi
exit $code
