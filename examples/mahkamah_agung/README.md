# Bot Unduh Putusan Mahkamah Agung

Mengunduh PDF putusan dari `putusan3.mahkamahagung.go.id` dalam jumlah besar
(±900 ribu file per tahun) memakai endpoint `/cookies` dari server bypass ini.
Progres tersimpan di SQLite, jadi bot bisa dihentikan dan dijalankan lagi
kapan saja tanpa mengulang dari awal.

## Jalankan di mana: VPS atau lokal?

**Rekomendasi: jalankan di komputer lokal yang punya disk 1,5 TB.**

| | VPS (2 vCPU / 2 GB / 20 GB) | Lokal (disk 1,5 TB) |
|---|---|---|
| Ruang disk | **20 GB, jauh tidak cukup.** 900 rb PDF diperkirakan butuh ratusan GB | Cukup |
| Durasi | ±10–11 hari nonstop; kredit Rp 31 rb (tarif Rp 149 rb/bulan) hanya cukup ±6 hari | Gratis, selama komputer menyala |
| RAM | 2 GB cukup asal pakai `docker-compose.yml` di folder ini + swap | Biasanya lebih longgar |
| IP | IP datacenter lebih sering kena challenge Cloudflare | IP rumahan biasanya lebih lancar |

VPS tetap berguna untuk **uji coba singkat**: jalankan beberapa jam, lalu cek
`python bot_mahkamah.py stats` untuk melihat ukuran rata-rata per file dan
kecepatan nyata. Dari situ kebutuhan disk total bisa dihitung. Kalau tetap ingin
memakai VPS untuk unduhan penuh, tambahkan block storage minimal ±1 TB dan isi
kredit untuk ±2 minggu. Bot otomatis menjeda unduhan saat sisa disk di bawah
`MIN_FREE_GB`, jadi disk tidak akan penuh total.

## Cara kerja

1. **crawl**: menelusuri halaman daftar
   `/direktori/index/tahunjenis/putus/tahun/2025/page/N.html` (20 putusan per
   halaman), lalu menyimpan ID, judul, dan tanggal ke tabel `docs`. Hanya tab
   "Putusan" (`#tabs-1`) yang diambil; tab "Penting" dan "Terpopuler" diabaikan.
2. **download**: membuka halaman detail tiap putusan, menyimpan metadata
   (nomor, lembaga peradilan, amar, dll.), lalu mengunduh PDF.

Perintah `run` (bawaan) menjalankan keduanya bersamaan.

Perilaku penting:

- **Cookie Cloudflare diperbarui otomatis** bila ditolak (lewat
  `/html?bypassCookieCache=true`, karena `/cookies` bisa mengembalikan cookie
  lama dari cache).
- **Hanya file PDF yang disimpan.** File diperiksa harus diawali `%PDF` dan
  ukurannya harus sesuai, sehingga halaman error tidak tersimpan sebagai PDF.
- **Batas request global** (`REQ_PER_SEC`) agar server MA tidak terbebani.
  Bila terjadi banyak kegagalan beruntun, semua worker dijeda 5 menit.
- **Percobaan ulang bertahap:** putusan yang gagal dicoba lagi setelah 10, 20,
  40, lalu 80 menit, kemudian ditandai `FAILED`.
- **Lokasi file:** PDF disimpan di `DOWNLOAD_DIR/pdf/<2 karakter hash>/<id>.pdf`,
  tersebar ke 256 folder.
- **Berhenti dengan rapi:** Ctrl+C atau SIGTERM menunggu pekerjaan yang sedang
  berjalan selesai.

## Instalasi (Ubuntu)

```bash
# 1. Docker (lewati bila sudah ada)
sudo apt update && sudo apt install -y docker.io docker-compose-v2 python3-venv git
sudo usermod -aG docker $USER && newgrp docker

# 2. Kode
git clone https://github.com/juanfernandez212414/CloudflareBypassForScraping.git
cd CloudflareBypassForScraping/examples/mahkamah_agung

# 3. Server bypass (port hanya dibuka untuk localhost)
docker compose up -d
docker logs -f cloudflare-bypass        # Ctrl+C bila sudah "running"

# 4. Python
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 5. Konfigurasi
cp .env.example .env && chmod 600 .env
nano .env                               # isi DOWNLOAD_DIR, token Telegram, dll.
```

**Khusus VPS 2 GB:** tambahkan swap supaya browser tidak kehabisan RAM.

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**Khusus WSL (Windows):** bila `DOWNLOAD_DIR` ada di drive Windows (`/mnt/d/...`),
set `DB_NAME` ke folder Linux (mis. `/home/USER/ma/bot_state.db`). SQLite tidak
aman dan lambat di drive Windows.

## Menjalankan

Uji coba dulu di terminal:

```bash
./venv/bin/python bot_mahkamah.py
```

Pastikan di log muncul `Crawl: N halaman (±… putusan)` dan file PDF mulai masuk
ke `DOWNLOAD_DIR/pdf/`. Hentikan dengan Ctrl+C, lalu jalankan sebagai service
agar tetap berjalan setelah SSH ditutup atau server reboot:

```bash
sed -e "s|__DIR__|$PWD|g" -e "s|__USER__|$USER|g" bot-mahkamah.service \
  | sudo tee /etc/systemd/system/bot-mahkamah.service
sudo systemctl daemon-reload
sudo systemctl enable --now bot-mahkamah

journalctl -u bot-mahkamah -f          # lihat log
sudo systemctl stop bot-mahkamah       # hentikan (progres tetap tersimpan)
```

Perintah lain:

| Perintah | Fungsi |
|---|---|
| `python bot_mahkamah.py stats` | Progres, kecepatan, perkiraan selesai, sisa disk |
| `python bot_mahkamah.py report` | Kirim progres ke Telegram |
| `python bot_mahkamah.py export hasil.csv` | Ekspor metadata (nomor, pengadilan, amar, lokasi file) ke CSV |
| `python bot_mahkamah.py retry-failed` | Antrekan ulang putusan/halaman yang gagal |
| `python bot_mahkamah.py update` | Ambil putusan yang baru diunggah (daftar diurutkan dari unggahan terbaru) |
| `python bot_mahkamah.py crawl` / `download` | Jalankan satu fase saja |

## Konfigurasi (`.env`)

| Variabel | Default | Keterangan |
|---|---|---|
| `MA_YEAR` | `2025` | Tahun putusan |
| `DOWNLOAD_DIR` | `/mnt/data_1_5TB/putusan_<tahun>` | Folder PDF, log, dan (default) database |
| `DB_NAME` | `<DOWNLOAD_DIR>/bot_state.db` | Database progres |
| `WORKERS` | `3` | Unduhan paralel |
| `REQ_PER_SEC` | `2` | Batas total request per detik ke situs MA |
| `MAX_ATTEMPTS` | `5` | Percobaan sebelum ditandai `FAILED` |
| `MIN_FREE_GB` | `2` | Unduhan dijeda bila sisa disk di bawah ini |
| `BYPASS_URL` | `http://localhost:8000` | Alamat server bypass |
| `SEL_LIST_ITEM`, `SEL_PDF` | lihat skrip | Selector CSS; ubah hanya bila struktur situs berubah |

**Perkiraan waktu:** satu putusan butuh 2 request (halaman detail + PDF). Pada
2 req/detik, kecepatannya ±1 putusan/detik atau ±86 ribu per hari, jadi 900
ribu putusan selesai dalam ±10–11 hari. Menaikkan `REQ_PER_SEC` mempercepat,
tetapi menambah beban server MA dan risiko IP diblokir. Naikkan bertahap
sambil memantau jumlah `Gagal`.

## Laporan harian Telegram (sebelum 09:00 WIB)

1. Buat bot lewat @BotFather. Isi `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID`
   di `.env`, lalu tes dengan `./venv/bin/python bot_mahkamah.py report`.
2. Pastikan jam server memakai WIB. Cron Ubuntu mengikuti zona waktu sistem,
   dan VPS sering diset ke UTC:
   ```bash
   sudo timedatectl set-timezone Asia/Jakarta && sudo systemctl restart cron
   ```
3. Buka `crontab -e`, lalu tambahkan (ganti path-nya):
   ```
   45 8 * * * cd /path/ke/examples/mahkamah_agung && ./venv/bin/python bot_mahkamah.py report >> report.log 2>&1
   ```

Contoh isi laporan:

```
Laporan Bot Putusan MA 2025 (25-10-2026 08:45)
- Terunduh: 86,120 / ±900,000 (9.6%)
- 24 jam terakhir: +85,904 | 1 jam terakhir: +3,571
- Antre: 812,447 | Gagal: 12 | Tanpa PDF: 1,421
- Crawl daftar: selesai (gagal: 0)
- Ukuran: 41.2 GB (rata-rata 502.1 KB/file) | Sisa disk: 1.3 TB
- Perkiraan selesai: ±9.5 hari
- Bot: BERJALAN | Server bypass: OK
```

## Test

```bash
./venv/bin/pip install pytest
./venv/bin/python -m pytest -q test_bot_mahkamah.py
```

Test memakai server tiruan yang meniru struktur HTML asli situs MA, termasuk
tab samping, paginasi, halaman tanpa PDF, respons non-PDF, dan cookie lama
yang ditolak Cloudflare.
