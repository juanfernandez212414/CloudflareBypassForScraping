# Bot Unduh Putusan Mahkamah Agung

Contoh pemakaian endpoint `/cookies` untuk mengunduh PDF putusan dari
`putusan3.mahkamahagung.go.id` dalam jumlah besar (±900 ribu file), dengan
progres tersimpan di SQLite agar bisa dilanjutkan setelah terputus.

## 1. Jalankan bypass server

```bash
docker compose up -d          # dari root repo, server di http://localhost:8000
```

Bot **harus** berjalan di mesin yang sama (IP yang sama) dengan container:
`cf_clearance` terikat ke IP dan User-Agent.

## 2. Pasang dependensi & jalankan bot

```bash
sudo apt install -y python3-venv sqlite3
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt

# jalankan di latar belakang (tetap hidup setelah SSH ditutup)
nohup python bot_mahkamah.py > bot.log 2>&1 &
tail -f bot.log
python bot_mahkamah.py stats   # cek progres kapan saja
```

Bot bekerja dalam dua fase: `crawl` (mengumpulkan URL putusan dari halaman
daftar) lalu `download` (membuka halaman detail dan mengunduh PDF). Keduanya
bisa dijalankan terpisah: `python bot_mahkamah.py crawl` / `download`.
Bila dihentikan (Ctrl+C, reboot), jalankan lagi; bot melanjutkan dari
posisi terakhir.

Konfigurasi lewat variabel lingkungan:

| Variabel | Default | Keterangan |
|---|---|---|
| `MA_YEAR` | `2025` | Tahun putusan |
| `DOWNLOAD_DIR` | `/mnt/data_1_5TB/putusan_<tahun>` | Folder tujuan (PDF dipecah ke subfolder 2 karakter) |
| `DB_NAME` | `<DOWNLOAD_DIR>/bot_state.db` | Basis data progres |
| `WORKERS` | `2` | Unduhan paralel |
| `DELAY` | `1.0` | Jeda (detik) per request per worker |
| `MAX_ATTEMPTS` | `5` | Percobaan sebelum ditandai `FAILED` |
| `SEL_DETAIL`, `SEL_PDF` | lihat skrip | Selector CSS; bila tidak cocok bot memakai pola URL `/direktori/putusan/<id>.html` dan `/direktori/download_file/.../pdf/...` |

Status putusan di tabel `docs`: `PENDING`, `DONE`, `NO_PDF` (halaman tanpa
lampiran PDF), `FAILED`. Untuk mencoba ulang yang gagal:

```bash
sqlite3 /mnt/data_1_5TB/putusan_2025/bot_state.db \
  "UPDATE docs SET status='PENDING', attempts=0 WHERE status='FAILED'"
```

**Perkiraan waktu:** tiap putusan butuh 2 request. Dengan `WORKERS=2` dan
`DELAY=1` hasilnya kira-kira 1 putusan/detik, jadi 900 ribu putusan butuh
±10–11 hari. Menaikkan `WORKERS` mempercepat, tetapi juga menambah beban ke
server MA dan memperbesar risiko IP diblokir; naikkan bertahap dan pantau
jumlah `FAILED`.

## 3. Laporan harian ke Telegram

```bash
cat > .env <<'ENV'
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
DB_FILE=/mnt/data_1_5TB/putusan_2025/bot_state.db
TARGET_DIR=/mnt/data_1_5TB/putusan_2025
ENV
chmod 600 .env
./report.sh                    # tes kirim sekali
```

Jadwalkan pukul 08:45 WIB setiap hari (`crontab -e`):

```
CRON_TZ=Asia/Jakarta
45 8 * * * /path/ke/examples/mahkamah_agung/report.sh
```

Cron memakai zona waktu server; `CRON_TZ` memastikan jadwal tetap 08:45 WIB
walau server diset ke UTC (cek dengan `timedatectl`).
