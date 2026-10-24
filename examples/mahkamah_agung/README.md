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
| Durasi | Target 7 hari nonstop; kredit Rp 31 rb (tarif Rp 149 rb/bulan) hanya cukup ±6 hari | Gratis, selama komputer menyala 7 hari penuh |
| RAM | 2 GB cukup asal pakai `docker-compose.yml` di folder ini + swap | Biasanya lebih longgar |
| IP | IP datacenter lebih sering kena challenge Cloudflare | IP rumahan biasanya lebih lancar |

VPS tetap berguna untuk **uji coba singkat**: jalankan beberapa jam, lalu cek
`python bot_mahkamah.py stats` untuk melihat ukuran rata-rata per file dan
kecepatan nyata. Dari situ kebutuhan disk total bisa dihitung. Kalau tetap ingin
memakai VPS untuk unduhan penuh, tambahkan block storage minimal ±1 TB dan isi
kredit untuk minimal 7 hari. Bot otomatis menjeda unduhan saat sisa disk di bawah
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
nano .env                               # isi DOWNLOAD_DIR, dll.
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
| `python bot_mahkamah.py report` | Tambahkan laporan progres ke `laporan.log` |
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
| `WORKERS` | `8` | Unduhan paralel |
| `REQ_PER_SEC` | `4` | Batas total request per detik ke situs MA |
| `TARGET_DAYS` | `7` | Target selesai, dihitung sejak bot pertama kali jalan |
| `REPORT_FILE` | `<DOWNLOAD_DIR>/laporan.log` | File laporan progres |
| `REPORT_EVERY_MIN` | `60` | Selama bot jalan, laporan ditulis tiap N menit (0 = mati) |
| `MAX_ATTEMPTS` | `5` | Percobaan sebelum ditandai `FAILED` |
| `MIN_FREE_GB` | `2` | Unduhan dijeda bila sisa disk di bawah ini |
| `BYPASS_URL` | `http://localhost:8000` | Alamat server bypass |
| `SEL_LIST_ITEM`, `SEL_PDF` | lihat skrip | Selector CSS; ubah hanya bila struktur situs berubah |

## Target 7 hari

Satu putusan butuh 2 request (halaman detail + PDF), ditambah ±45 ribu request
untuk halaman daftar. Pengaturan bawaan `REQ_PER_SEC=4` berarti ±2 putusan per
detik atau ±170 ribu per hari. Dengan begitu, 900 ribu putusan selesai dalam
**±5,5 hari**, dan masih ada cadangan ±1,5 hari untuk gangguan (Cloudflare,
server MA lambat, listrik/internet mati).

Bagian Target di laporan menunjukkan apakah jadwal tercapai:

- **AMAN**: laju 1 jam terakhir sudah cukup untuk selesai sebelum tenggat.
- **TERTINGGAL**: naikkan `REQ_PER_SEC` (mis. ke 5) dan `WORKERS` (mis. ke 10)
  di `.env`, lalu `sudo systemctl restart bot-mahkamah`. Sebelum itu, cek dulu
  apakah jumlah `Gagal` melonjak atau log berisi `kegagalan beruntun`. Kalau
  ya, masalahnya pemblokiran, bukan kecepatan, dan menaikkan kecepatan justru
  memperparah.

Kecepatan nyata juga dibatasi oleh seberapa cepat server MA merespons. Jika
server butuh rata-rata lebih dari ±2 detik per request, 8 worker tidak cukup
untuk mencapai 4 req/detik, dan hal itu akan terlihat sebagai laju di bawah
target. Karena itu, cek laporan pada jam-jam pertama.

Komputer, Docker, dan bot harus menyala terus selama 7 hari. Gunakan service
systemd di atas supaya bot otomatis jalan lagi setelah reboot.

## Laporan ke file log

Tidak perlu Telegram. Laporan ditambahkan ke `DOWNLOAD_DIR/laporan.log`:

- otomatis setiap 60 menit selama bot berjalan,
- saat bot berhenti (laporan akhir),
- setiap kali `python bot_mahkamah.py report` dijalankan.

Untuk laporan harian sebelum 09:00 WIB, jadwalkan perintah `report` lewat cron.
Laporan ini tetap tertulis walaupun bot sedang mati, dan akan berstatus
`BERHENTI sejak ...`.

1. Pastikan jam server memakai WIB. Cron Ubuntu mengikuti zona waktu sistem:
   ```bash
   sudo timedatectl set-timezone Asia/Jakarta && sudo systemctl restart cron
   ```
2. Buka `crontab -e`, lalu tambahkan (ganti path-nya):
   ```
   45 8 * * * cd /path/ke/examples/mahkamah_agung && ./venv/bin/python bot_mahkamah.py report > /dev/null 2>&1
   ```

Melihat laporan:

```bash
tail -n 12 /mnt/data_1_5TB/putusan_2025/laporan.log    # laporan terakhir
./venv/bin/python bot_mahkamah.py stats               # kondisi saat ini, tanpa menulis ke file
```

Contoh isi laporan:

```
Laporan Bot Putusan MA 2025 (26-10-2026 08:45)
- Terunduh: 301,550 / ±900,000 (33.5%)
- 24 jam terakhir: +171,230 | 1 jam terakhir: +7,180
- Antre: 596,811 | Gagal: 38 | Tanpa PDF: 1,601
- Crawl daftar: selesai (gagal: 0)
- Ukuran: 144.3 GB (rata-rata 502.1 KB/file) | Sisa disk: 1.2 TB
- Perkiraan selesai: ±3.5 hari
- Target 7 hari: paling lambat 31-10-2026 02:49 (sisa 4.7 hari)
- Laju sekarang: 7,180/jam | Laju minimal: 5,300/jam -> AMAN
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
