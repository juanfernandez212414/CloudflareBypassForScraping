#!/bin/bash
# Laporan harian progres bot MA ke Telegram.
# Kredensial dibaca dari file .env (jangan ditulis langsung di skrip):
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_CHAT_ID=...
#   DB_FILE=/mnt/data_1_5TB/putusan_2025/bot_state.db
#   TARGET_DIR=/mnt/data_1_5TB/putusan_2025
set -u
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a

DB_FILE="${DB_FILE:-/mnt/data_1_5TB/putusan_2025/bot_state.db}"
TARGET_DIR="${TARGET_DIR:-/mnt/data_1_5TB/putusan_2025}"

q() { sqlite3 "$DB_FILE" "$1" 2>/dev/null || echo "?"; }

DONE=$(q "SELECT COUNT(*) FROM docs WHERE status='DONE'")
PENDING=$(q "SELECT COUNT(*) FROM docs WHERE status='PENDING'")
FAILED=$(q "SELECT COUNT(*) FROM docs WHERE status='FAILED'")
NO_PDF=$(q "SELECT COUNT(*) FROM docs WHERE status='NO_PDF'")
TOTAL=$(q "SELECT COUNT(*) FROM docs")
LAST24=$(q "SELECT COUNT(*) FROM docs WHERE status='DONE' AND updated_at >= datetime('now','localtime','-1 day')")
PAGES=$(q "SELECT COUNT(*) FROM pages WHERE status='DONE'")
DISK=$(du -sh "$TARGET_DIR" 2>/dev/null | cut -f1)
FREE=$(df -h "$TARGET_DIR" 2>/dev/null | awk 'NR==2{print $4}')

if pgrep -f bot_mahkamah.py >/dev/null; then BOT="BERJALAN"; else BOT="BERHENTI"; fi
if curl -s -m 10 -o /dev/null -w '%{http_code}' http://localhost:8000/docs | grep -q 200; then
  BYPASS="OK"; else BYPASS="MATI"; fi

MESSAGE="Laporan Harian Bot MA ($(date '+%d-%m-%Y %H:%M'))
- Terunduh: ${DONE} / ${TOTAL} (24 jam terakhir: +${LAST24})
- Antre: ${PENDING} | Gagal: ${FAILED} | Tanpa PDF: ${NO_PDF}
- Halaman daftar: ${PAGES}
- Disk terpakai: ${DISK} | Sisa: ${FREE}
- Bot: ${BOT} | Bypass server: ${BYPASS}"

curl -s -m 30 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
     -d chat_id="${TELEGRAM_CHAT_ID}" \
     --data-urlencode text="${MESSAGE}" >/dev/null
