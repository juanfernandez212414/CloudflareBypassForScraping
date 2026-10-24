"""
Bot unduh PDF putusan Mahkamah Agung (putusan3.mahkamahagung.go.id) skala besar.

Cara kerja:
  - crawl    : telusuri halaman daftar per tahun -> simpan putusan ke SQLite
  - download : buka halaman detail tiap putusan -> simpan metadata + unduh PDF

  Situs MA hanya menampilkan ±500 halaman (±10 rb putusan) per daftar, padahal
  satu tahun bisa ±900 rb putusan. Karena itu daftar dipecah menjadi "bagian"
  per pengadilan, lalu per kategori/klasifikasi bila masih terlalu besar,
  memakai filter (beserta jumlahnya) di sidebar halaman daftar.
  Perintah `run` (bawaan) menjalankan keduanya bersamaan. Semua progres ada di
  SQLite, jadi bot bisa dihentikan/di-restart kapan saja dan lanjut sendiri.

Clearance Cloudflare diambil dari server bypass lokal (endpoint /cookies).
cf_clearance terikat ke IP + User-Agent, jadi bot HARUS berjalan di mesin/IP
yang sama dengan container bypass.

Perintah:
  python bot_mahkamah.py [run]        crawl + download bersamaan
  python bot_mahkamah.py crawl        hanya kumpulkan daftar putusan
  python bot_mahkamah.py download     hanya unduh yang sudah terdaftar
  python bot_mahkamah.py update       cek putusan yang baru diunggah (halaman awal)
  python bot_mahkamah.py stats        tampilkan progres
  python bot_mahkamah.py report       tulis progres ke laporan.log (untuk cron)
  python bot_mahkamah.py export [f]   ekspor metadata ke CSV
  python bot_mahkamah.py retry-failed antrekan ulang putusan/halaman yang gagal
"""
import argparse
import csv
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import signal
import sqlite3
import threading
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    # curl_cffi meniru sidik jari TLS Chrome; requests biasa sering tetap kena
    # challenge walau cookie cf_clearance sudah benar.
    from curl_cffi import requests as cffi
except ImportError:  # pragma: no cover
    cffi = None


def load_env_file(path):
    """Baca KEY=VALUE dari .env tanpa menimpa variabel yang sudah di-set."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    value = value.split(" #", 1)[0].strip().strip("'\"")
                    os.environ.setdefault(key.strip(), value)
    except FileNotFoundError:
        pass


load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ---------------------------------------------------------------- konfigurasi
BASE_URL = os.environ.get("MA_BASE_URL", "https://putusan3.mahkamahagung.go.id").rstrip("/")
YEAR = os.environ.get("MA_YEAR", "2025")
LIST_PATH = os.environ.get("MA_LIST_PATH", "/direktori/index/tahunjenis/putus/tahun/{year}").format(year=YEAR)
BYPASS_URL = os.environ.get("BYPASS_URL", "http://localhost:8000").rstrip("/")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", f"/mnt/data_1_5TB/putusan_{YEAR}")
DB_NAME = os.environ.get("DB_NAME", os.path.join(DOWNLOAD_DIR, "bot_state.db"))
# Tiap putusan = 2 request (halaman detail + PDF). 4 req/detik ~ 2 putusan/detik
# ~ 170 rb/hari, jadi 900 rb putusan selesai ±5,5 hari (sisa waktu untuk gangguan).
WORKERS = int(os.environ.get("WORKERS", "8"))             # unduhan paralel
REQ_PER_SEC = float(os.environ.get("REQ_PER_SEC", "4"))    # batas total request ke situs MA
TARGET_DAYS = float(os.environ.get("TARGET_DAYS", "7"))   # target selesai sejak bot pertama jalan
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "5"))
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "2"))   # jeda unduh bila disk hampir penuh
TIMEOUT = int(os.environ.get("TIMEOUT", "60"))
REPORT_FILE = os.environ.get("REPORT_FILE", os.path.join(DOWNLOAD_DIR, "laporan.log"))
REPORT_EVERY_MIN = int(os.environ.get("REPORT_EVERY_MIN", "60"))  # laporan otomatis selama bot jalan
# Daftar dengan halaman terakhir >= angka ini dianggap terpotong oleh situs
# (terlihat: kategori Korupsi 44.203 putusan tapi paginasi berhenti di 499).
CAP_PAGES = int(os.environ.get("MA_CAP_PAGES", "450"))
MAX_SPLIT_DEPTH = 6

# Struktur HTML (diverifikasi pada halaman asli situs MA). Halaman daftar punya
# tiga tab: #tabs-1 "Putusan" (daftar sebenarnya, 20/halaman), #tabs-2 "Penting"
# dan #tabs-3 "Terpopuler" yang isinya sama di setiap halaman -> harus diabaikan.
SEL_LIST_ITEM = os.environ.get("SEL_LIST_ITEM", "#tabs-1 div.spost")
SEL_PDF = os.environ.get("SEL_PDF", "a[href*='/direktori/download_file/'][href*='/pdf/']")
RE_DETAIL = re.compile(r"/direktori/putusan/([A-Za-z0-9]+)\.html")
RE_FILTER_PATH = re.compile(r"/direktori/index/(.+?)(?:/page/\d+)?\.html$")
FILTER_ORDER = ("pengadilan", "kategori", "tahunjenis", "tahun")
RE_LIST_DATE = re.compile(r"(Register|Putus|Upload)\s*:\s*([0-9]{2}-[0-9]{2}-[0-9]{4})")
CHALLENGE_MARKERS = ("Just a moment", "cf-chl", "challenge-platform", "cf-browser-verification",
                     "Attention Required", "Sorry, you have been blocked")

log = logging.getLogger("bot_ma")


class Stopped(Exception):
    """Bot diminta berhenti (Ctrl+C / SIGTERM)."""


class TransientError(Exception):
    """Gangguan jaringan/server, bukan kesalahan dokumen."""


class ParseError(Exception):
    """Halaman terunduh tapi isinya tidak seperti yang diharapkan."""


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------- database
class DB:
    DOC_COLUMNS = {
        "url": "TEXT", "judul": "TEXT", "tgl_register": "TEXT", "tgl_putus": "TEXT",
        "tgl_upload": "TEXT", "status": "TEXT DEFAULT 'PENDING'", "attempts": "INTEGER DEFAULT 0",
        "next_try": "REAL DEFAULT 0", "nomor": "TEXT", "pdf_url": "TEXT", "nama_file": "TEXT",
        "file": "TEXT", "size": "INTEGER", "meta": "TEXT", "error": "TEXT",
        "added_at": "TEXT", "updated_at": "TEXT",
    }

    def __init__(self, path):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # versi lama menelusuri satu daftar saja (terpotong ±10 rb); progres
        # halamannya tidak berlaku lagi, data putusan tetap dipakai
        pages_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(pages)")}
        if pages_cols and "slice" not in pages_cols:
            self.conn.execute("DROP TABLE pages")
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='kv'").fetchone():
                self.conn.execute("DELETE FROM kv WHERE k IN ('crawl_finished', 'last_page')")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS slices (
                path TEXT PRIMARY KEY, label TEXT, depth INTEGER, count INTEGER, last_page INTEGER,
                status TEXT DEFAULT 'NEW', attempts INTEGER DEFAULT 0, error TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS pages (
                slice TEXT, page INTEGER, status TEXT, n_links INTEGER, n_new INTEGER,
                attempts INTEGER DEFAULT 0, error TEXT, updated_at TEXT, PRIMARY KEY (slice, page));
            CREATE TABLE IF NOT EXISTS docs (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        """)
        # migrasi: tambahkan kolom yang belum ada (mis. DB dari versi lama)
        have = {row[1] for row in self.conn.execute("PRAGMA table_info(docs)")}
        for col, decl in self.DOC_COLUMNS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE docs ADD COLUMN {col} {decl}")
        self.conn.execute("UPDATE docs SET status='PENDING' WHERE status IS NULL")
        self.conn.execute("UPDATE docs SET attempts=0 WHERE attempts IS NULL")
        self.conn.execute("UPDATE docs SET next_try=0 WHERE next_try IS NULL")
        self.conn.executescript("""
            CREATE INDEX IF NOT EXISTS docs_queue ON docs(status, next_try);
            CREATE INDEX IF NOT EXISTS docs_updated ON docs(status, updated_at);
        """)
        self.conn.commit()

    def execute(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def query(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def scalar(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0][0] if rows else None

    def get_kv(self, key, default=None):
        value = self.scalar("SELECT v FROM kv WHERE k=?", (key,))
        return default if value is None else value

    def set_kv(self, key, value):
        self.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, str(value)))

    def add_docs(self, items):
        """Simpan putusan dari halaman daftar; kembalikan jumlah yang baru."""
        with self.lock:
            before = self.conn.total_changes
            self.conn.executemany(
                "INSERT OR IGNORE INTO docs (id, url, judul, tgl_register, tgl_putus, tgl_upload, added_at, "
                "status, attempts, next_try) VALUES (:id, :url, :judul, :tgl_register, :tgl_putus, :tgl_upload, "
                ":added_at, 'PENDING', 0, 0)",
                [dict(item, added_at=now_str()) for item in items])
            self.conn.commit()
            return self.conn.total_changes - before


# ---------------------------------------------------------------- HTTP client
class RateLimiter:
    """Batas request global (semua thread) supaya server MA tidak terbebani."""

    def __init__(self, per_sec):
        self.interval = 1.0 / per_sec if per_sec > 0 else 0
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self, stop):
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.interval
        if stop.wait(max(0.0, at - time.monotonic())):
            raise Stopped()


class Bypass:
    """Cookie cf_clearance + User-Agent dari server bypass, dipakai semua thread."""

    MIN_FORCE_INTERVAL = 60  # detik; cegah browser diluncurkan terus-menerus

    def __init__(self, stop):
        self.stop = stop
        self.lock = threading.Lock()
        self.cookies, self.user_agent, self.generation = {}, "", 0
        self.forced_at = 0.0

    def refresh(self, seen_generation=None, force=False):
        with self.lock:
            # thread lain sudah memperbarui saat kita menunggu lock
            if seen_generation is not None and seen_generation != self.generation:
                return
            if force and self.stop.wait(max(0.0, self.forced_at + self.MIN_FORCE_INTERVAL - time.time())):
                raise Stopped()
            delay = 15
            while not self.stop.is_set():
                try:
                    if force:
                        # /cookies mengembalikan cache (s.d. ~29 menit) walau sudah
                        # ditolak Cloudflare; /html?bypassCookieCache=true memaksa
                        # browser membuat clearance baru dan memperbarui cache.
                        log.warning("Clearance ditolak, membuat clearance baru...")
                        self.forced_at = time.time()
                        requests.get(f"{BYPASS_URL}/html", timeout=300, params={
                            "url": BASE_URL + "/", "bypassCookieCache": "true"}).raise_for_status()
                    r = requests.get(f"{BYPASS_URL}/cookies", params={"url": BASE_URL + "/"}, timeout=300)
                    r.raise_for_status()
                    data = r.json()
                    self.cookies, self.user_agent = data["cookies"], data["user_agent"]
                    self.generation += 1
                    log.info("Clearance OK (cookie: %s)", ", ".join(sorted(self.cookies)) or "-")
                    return
                except requests.RequestException as e:
                    log.error("Server bypass %s gagal: %s. Coba lagi dalam %ds", BYPASS_URL, e, delay)
                    self.stop.wait(delay)
                    delay = min(delay * 2, 600)
            raise Stopped()


class Breaker:
    """Jeda semua worker bila banyak kegagalan beruntun (situs down/IP dibatasi)."""

    THRESHOLD, PAUSE = 10, 300

    def __init__(self, stop):
        self.stop, self.lock = stop, threading.Lock()
        self.failures, self.pause_until = 0, 0.0

    def ok(self):
        with self.lock:
            self.failures = 0

    def fail(self):
        with self.lock:
            self.failures += 1
            if self.failures >= self.THRESHOLD and time.time() >= self.pause_until:
                log.error("%d kegagalan beruntun, jeda %d detik", self.failures, self.PAUSE)
                self.pause_until = time.time() + self.PAUSE
                self.failures = 0

    def wait(self):
        remaining = self.pause_until - time.time()
        if remaining > 0 and self.stop.wait(remaining):
            raise Stopped()


def is_challenge(r):
    if r.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if "text/html" not in r.headers.get("content-type", ""):
        return False
    head = r.text[:20000]
    return any(m in head for m in CHALLENGE_MARKERS)


class Http:
    def __init__(self, stop):
        self.stop = stop
        self.bypass = Bypass(stop)
        self.limiter = RateLimiter(REQ_PER_SEC)
        self.breaker = Breaker(stop)
        self.local = threading.local()

    def _session(self):
        # satu sesi per thread (koneksi keep-alive); dibuat ulang tiap clearance baru
        st = self.local
        if getattr(st, "generation", None) != self.bypass.generation:
            old = getattr(st, "session", None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            st.session = cffi.Session(impersonate="chrome") if cffi else requests.Session()
            st.generation = self.bypass.generation
        return st.session

    def get(self, url, referer=None):
        if self.bypass.generation == 0:
            self.bypass.refresh(seen_generation=0)
        last_error = None
        for attempt in range(4):
            self.breaker.wait()
            self.limiter.wait(self.stop)
            generation = self.bypass.generation
            headers = {"User-Agent": self.bypass.user_agent, "Referer": referer or BASE_URL + "/",
                       "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7"}
            try:
                r = self._session().get(url, headers=headers, cookies=self.bypass.cookies, timeout=TIMEOUT)
            except Exception as e:
                last_error = f"jaringan: {e}"
            else:
                if is_challenge(r):
                    last_error = f"diblokir Cloudflare (HTTP {r.status_code})"
                    self.bypass.refresh(seen_generation=generation, force=True)
                    continue
                if r.status_code == 404:
                    self.breaker.ok()
                    raise ParseError("HTTP 404")
                if r.status_code == 429 or r.status_code >= 500:
                    last_error = f"HTTP {r.status_code}"
                    retry_after = r.headers.get("retry-after", "") if r.status_code == 429 else ""
                    wait = min(int(retry_after), 600) if retry_after.isdigit() else 0
                    if self.stop.wait(max(wait, 10 * (attempt + 1))):
                        raise Stopped()
                    continue
                if r.status_code >= 400:
                    raise ParseError(f"HTTP {r.status_code}")
                self.breaker.ok()
                return r
            if self.stop.wait(5 * (attempt + 1)):
                raise Stopped()
        self.breaker.fail()
        raise TransientError(f"{url}: {last_error}")


# --------------------------------------------------------------------- parsing
def parse_filters(href):
    """'/direktori/index/pengadilan/pn-x/tahun/2025.html' -> {'pengadilan': 'pn-x', 'tahun': '2025'}"""
    match = RE_FILTER_PATH.search(urlparse(href).path)
    if not match:
        return {}
    parts = match.group(1).split("/")
    return dict(zip(parts[::2], parts[1::2])) if len(parts) % 2 == 0 else {}


def filters_path(filters):
    keys = [k for k in FILTER_ORDER if k in filters] + sorted(k for k in filters if k not in FILTER_ORDER)
    return "/direktori/index/" + "/".join(f"{k}/{filters[k]}" for k in keys)


ROOT_PATH = filters_path(parse_filters(LIST_PATH + ".html"))


def list_url(path, page):
    return f"{BASE_URL}{path}.html" if page == 1 else f"{BASE_URL}{path}/page/{page}.html"


def parse_facets(soup):
    """Filter di sidebar: {judul kartu: [(filter, label, jumlah)]}, mis. kartu
    'pengadilan' berisi tiap pengadilan beserta jumlah putusannya."""
    facets = {}
    for card in soup.select("div.card"):
        head = card.select_one(".card-header")
        title = head.get_text(" ", strip=True).lower() if head else ""
        for badge in card.select("a span.badge"):
            link = badge.find_parent("a")
            filters = parse_filters(link.get("href", ""))
            if not filters:
                continue
            label = " ".join(t.strip() for t in link.find_all(string=True, recursive=False)).strip()
            digits = re.sub(r"\D", "", badge.get_text())
            facets.setdefault(title, []).append((filters, label, int(digits) if digits else None))
    return facets


def parse_list(html):
    """Kembalikan (daftar putusan, nomor halaman terakhir, filter sidebar)."""
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    for entry in soup.select(SEL_LIST_ITEM):
        link = entry.select_one("strong > a[href*='/direktori/putusan/']") or \
            next((a for a in entry.find_all("a", href=True) if RE_DETAIL.search(a["href"])), None)
        match = RE_DETAIL.search(link["href"]) if link else None
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        dates = dict(RE_LIST_DATE.findall(entry.get_text(" ", strip=True)))
        items.append({
            "id": match.group(1), "url": urljoin(BASE_URL + "/", link["href"]),
            "judul": link.get_text(" ", strip=True), "tgl_register": dates.get("Register"),
            "tgl_putus": dates.get("Putus"), "tgl_upload": dates.get("Upload"),
        })
    last = 1
    for a in soup.select("a.page-link"):
        value = a.get("data-ci-pagination-page") or ""
        match = re.search(r"/page/(\d+)\.html", a.get("href", ""))
        for candidate in (value, match.group(1) if match else ""):
            if candidate.isdigit():
                last = max(last, int(candidate))
    return items, last, parse_facets(soup)


def parse_detail(html):
    """Kembalikan (metadata, url pdf, nama file pdf asli) dari halaman detail."""
    soup = BeautifulSoup(html, "html.parser")
    meta = {}
    table = soup.select_one("table.table")
    if table is not None:
        for tr in table.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) >= 2:
                key = tds[0].get_text(" ", strip=True)
                if key and len(key) <= 40:
                    meta[key] = tds[1].get_text(" ", strip=True)
    link = soup.select_one(SEL_PDF)
    if link is None:
        return meta, None, None
    return meta, urljoin(BASE_URL + "/", link["href"]), link.get_text(" ", strip=True) or None


def pdf_path(doc_id):
    # ID putusan berbasis waktu (awalannya mirip), jadi folder dibagi pakai hash
    # agar ±900rb file tersebar rata ke 256 folder.
    shard = hashlib.md5(doc_id.encode()).hexdigest()[:2]
    return os.path.join(DOWNLOAD_DIR, "pdf", shard, doc_id + ".pdf")


# ---------------------------------------------------------------------- crawl
def split_children(filters, facets):
    """Pecah bagian yang terpotong. Kembalikan (dimensi, anak, crawl_induk).

    Urutan: per pengadilan (tiap putusan tepat di satu pengadilan), lalu per
    kategori utama, lalu per klasifikasi. Klasifikasi bisa tidak mencakup
    putusan yang hanya diberi kategori induk, jadi induknya tetap ditelusuri
    sejauh yang ditampilkan situs (crawl_induk=True)."""
    def card(name):
        return [entry for title, entries in facets.items() if title.startswith(name) for entry in entries]

    def children(key, entries, keep_count, skip=None):
        out = {}
        for f, label, count in entries:
            if f.get(key) and f[key] != skip:
                child = dict(filters, **{key: f[key]})
                out.setdefault(filters_path(child), (label, count if keep_count else None))
        return out

    if "pengadilan" not in filters:
        kids = children("pengadilan", card("pengadilan"), keep_count=True)
        if kids:
            return "pengadilan", kids, False
    if "kategori" not in filters:
        # kartu "Direktori" = daftar kategori utama (jumlahnya global, bukan per filter)
        kids = children("kategori", card("direktori"), keep_count=False)
        if kids:
            return "kategori", kids, False
    else:
        kids = children("kategori", card("klasifikasi"), keep_count=False, skip=filters["kategori"])
        if kids:
            return "klasifikasi", kids, True
    return None, {}, True


def record_page(db, path, page, items, n_new):
    db.execute("INSERT OR REPLACE INTO pages (slice, page, status, n_links, n_new, attempts, error, updated_at) "
               "VALUES (?, ?, 'DONE', ?, ?, 0, NULL, ?)", (path, page, len(items), n_new, now_str()))


def analyse_slice(db, http, path, label, depth, count):
    """Ambil halaman 1 sebuah bagian; pecah bila daftarnya terpotong situs."""
    items, last, facets = parse_list(http.get(list_url(path, 1)).text)
    if depth == 0:
        if not items:
            raise ParseError(f"Tidak ada putusan di {list_url(path, 1)}. Struktur HTML berubah? "
                             "Cek SEL_LIST_ITEM.")
        # jumlah total tahun ini (kartu "Tahun") & jumlah per pengadilan, untuk laporan
        root = parse_filters(path + ".html")
        total = next((c for title, entries in facets.items() if title.startswith("tahun")
                      for f, _, c in entries if f == root and c), None)
        if total:
            db.set_kv("expected_total", total)
            count = total
    n_new = db.add_docs(items)
    record_page(db, path, 1, items, n_new)
    # terpotong bila paginasi mentok di batas situs, atau jumlah di sidebar
    # lebih banyak daripada yang bisa ditampilkan halamannya
    capped = last >= CAP_PAGES or bool(count and count > last * 20 + 20)
    status = "CRAWL"
    if capped and depth < MAX_SPLIT_DEPTH:
        dimension, kids, crawl_parent = split_children(parse_filters(path + ".html"), facets)
        if kids:
            with db.lock:
                db.conn.executemany(
                    "INSERT OR IGNORE INTO slices (path, label, depth, count, updated_at) VALUES (?, ?, ?, ?, ?)",
                    [(p, f"{label} > {lab}", depth + 1, c, now_str()) for p, (lab, c) in kids.items()])
                db.conn.commit()
            log.info("Bagian '%s' terpotong (%d halaman%s), dipecah per %s menjadi %d bagian",
                     label, last, f", {count:,} putusan" if count else "", dimension, len(kids))
            status = "CRAWL" if crawl_parent else "SPLIT"
        else:
            log.warning("Bagian '%s' terpotong di %d halaman dan tidak bisa dipecah lagi; "
                        "sebagian putusannya mungkin tidak terjangkau", label, last)
    elif capped:
        log.warning("Bagian '%s' terpotong dan batas kedalaman pemecahan tercapai", label)
    db.execute("UPDATE slices SET status=?, last_page=?, count=COALESCE(?, count), attempts=0, error=NULL, "
               "updated_at=? WHERE path=?", (status, last, count, now_str(), path))


def crawl_slice(db, http, stop, path, label, last):
    """Telusuri semua halaman sebuah bagian yang belum selesai."""
    done = {row[0] for row in db.query(
        "SELECT page FROM pages WHERE slice=? AND status IN ('DONE', 'FAILED')", (path,))}
    page, found = 1, 0
    while page <= last and not stop.is_set():
        if page in done:
            page += 1
            continue
        try:
            html = http.get(list_url(path, page), referer=list_url(path, page - 1)).text
            items, page_last, _ = parse_list(html)
            n_new = db.add_docs(items)
            record_page(db, path, page, items, n_new)
            found += n_new
            if not items:
                break  # halaman kosong = akhir daftar
            last = max(last, min(page_last, CAP_PAGES + 100))
            if page % 50 == 0:
                log.info("Bagian '%s': halaman %d/%d", label, page, last)
        except Stopped:
            return
        except Exception as e:
            attempts = (db.scalar("SELECT attempts FROM pages WHERE slice=? AND page=?", (path, page)) or 0) + 1
            status = "FAILED" if attempts >= MAX_ATTEMPTS else "PENDING"
            db.execute("INSERT OR REPLACE INTO pages (slice, page, status, attempts, error, updated_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (path, page, status, attempts, str(e)[:500], now_str()))
            log.warning("Bagian '%s' halaman %d gagal (%d/%d): %s", label, page, attempts, MAX_ATTEMPTS, e)
            if status == "PENDING":
                if stop.wait(30):
                    return
                continue
        page += 1
    if not stop.is_set():
        db.execute("UPDATE slices SET status='DONE', last_page=?, updated_at=? WHERE path=?",
                   (last, now_str(), path))
        log.info("Bagian '%s' selesai: %d halaman, +%d putusan baru", label, last, found)


def crawl(db, http, stop):
    """Telusuri daftar tahun ini, dipecah per bagian sampai semuanya selesai."""
    db.execute("INSERT OR IGNORE INTO slices (path, label, depth, updated_at) VALUES (?, ?, 0, ?)",
               (ROOT_PATH, f"Tahun {YEAR}", now_str()))
    while not stop.is_set():
        rows = db.query("SELECT path, label, depth, count, status, last_page FROM slices "
                        "WHERE status IN ('NEW', 'CRAWL') ORDER BY rowid LIMIT 1")
        if not rows:
            break
        path, label, depth, count, status, last = rows[0]
        if status == "CRAWL":
            crawl_slice(db, http, stop, path, label, last or 1)
            continue
        try:
            analyse_slice(db, http, path, label, depth, count)
        except Stopped:
            return
        except Exception as e:
            if depth == 0 and isinstance(e, ParseError):
                raise
            attempts = (db.scalar("SELECT attempts FROM slices WHERE path=?", (path,)) or 0) + 1
            status = "FAILED" if attempts >= MAX_ATTEMPTS else "NEW"
            db.execute("UPDATE slices SET status=?, attempts=?, error=?, updated_at=? WHERE path=?",
                       (status, attempts, str(e)[:500], now_str(), path))
            log.warning("Bagian '%s' gagal dibuka (%d/%d): %s", label, attempts, MAX_ATTEMPTS, e)
            if status == "NEW" and stop.wait(30):
                return
    if stop.is_set():
        return
    db.set_kv("crawl_finished", now_str())
    total = db.scalar("SELECT COUNT(*) FROM docs")
    expected = int(db.get_kv("expected_total", 0))
    log.info("Crawl selesai: %d putusan terdaftar%s", total, f" dari {expected:,} menurut situs" if expected else "")
    if expected and total < expected * 0.98:
        log.warning("Putusan terdaftar (%d) jauh di bawah jumlah di situs (%d). Cek bagian yang gagal/terpotong "
                    "di log, lalu jalankan retry-failed.", total, expected)


def update(db, http, stop, quiet_pages=3):
    """Daftar diurutkan dari unggahan terbaru: telusuri dari halaman 1 sampai
    beberapa halaman berturut-turut tidak berisi putusan baru."""
    page, quiet, total_new = 1, 0, 0
    while quiet < quiet_pages and not stop.is_set():
        try:
            items, last, _ = parse_list(http.get(list_url(ROOT_PATH, page)).text)
        except Stopped:
            break
        except (TransientError, ParseError) as e:
            log.warning("Update berhenti di halaman %d: %s", page, e)
            break
        n_new = db.add_docs(items)
        total_new += n_new
        quiet = quiet + 1 if n_new == 0 else 0
        if not items or page >= last:
            break
        page += 1
    log.info("Update: %d putusan baru dari %d halaman awal", total_new, page)
    return total_new


# ------------------------------------------------------------------- download
def wait_for_disk(stop):
    warned = False
    while shutil.disk_usage(DOWNLOAD_DIR).free < MIN_FREE_GB * 1024 ** 3:
        if not warned:
            log.error("Sisa disk < %.1f GB, unduhan dijeda sampai ada ruang kosong", MIN_FREE_GB)
            warned = True
        if stop.wait(300):
            raise Stopped()


def download_one(db, http, stop, doc_id, url):
    path = pdf_path(doc_id)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        db.execute("UPDATE docs SET status='DONE', file=?, size=?, updated_at=? WHERE id=?",
                   (path, os.path.getsize(path), now_str(), doc_id))
        return
    try:
        wait_for_disk(stop)
        meta, pdf_url, nama_file = parse_detail(http.get(url).text)
        if not meta:
            raise ParseError("halaman detail tanpa tabel metadata")
        db.execute("UPDATE docs SET nomor=?, meta=?, pdf_url=?, nama_file=? WHERE id=?",
                   (meta.get("Nomor"), json.dumps(meta, ensure_ascii=False), pdf_url, nama_file, doc_id))
        if not pdf_url:
            db.execute("UPDATE docs SET status='NO_PDF', error=NULL, updated_at=? WHERE id=?", (now_str(), doc_id))
            return
        r = http.get(pdf_url, referer=url)
        body = r.content
        # tanpa cek ini, halaman error HTML bisa tersimpan sebagai .pdf
        if b"%PDF" not in body[:1024]:
            raise ParseError(f"bukan PDF (content-type={r.headers.get('content-type')}, {len(body)} byte)")
        # Content-Length hanya bisa dibandingkan bila respons tidak dikompres
        expected = r.headers.get("content-length", "")
        encoded = r.headers.get("content-encoding", "identity").lower() not in ("", "identity")
        if expected.isdigit() and not encoded and int(expected) != len(body):
            raise TransientError(f"unduhan terpotong ({len(body)}/{expected} byte)")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
        db.execute("UPDATE docs SET status='DONE', file=?, size=?, error=NULL, updated_at=? WHERE id=?",
                   (path, len(body), now_str(), doc_id))
    except Stopped:
        raise
    except Exception as e:
        # semua error lain juga dicatat; tanpa ini putusan langsung diambil
        # ulang oleh antrean dan dicoba terus-menerus tanpa jeda
        if not isinstance(e, (TransientError, ParseError, OSError)):
            log.exception("Error tak terduga pada %s", doc_id)
            e = f"{type(e).__name__}: {e}"
        attempts = (db.scalar("SELECT attempts FROM docs WHERE id=?", (doc_id,)) or 0) + 1
        status = "FAILED" if attempts >= MAX_ATTEMPTS else "PENDING"
        # coba lagi nanti dengan jeda bertambah: 10, 20, 40, 80 menit
        next_try = time.time() + 600 * 2 ** (attempts - 1)
        db.execute("UPDATE docs SET status=?, attempts=?, next_try=?, error=?, updated_at=? WHERE id=?",
                   (status, attempts, next_try, str(e)[:500], now_str(), doc_id))
        log.warning("Gagal %s (%d/%d): %s", doc_id, attempts, MAX_ATTEMPTS, e)


def download(db, http, stop, crawl_done):
    """Ambil antrean PENDING dari DB dan bagikan ke WORKERS thread."""
    jobs = queue.Queue(maxsize=WORKERS * 2)
    inflight, inflight_lock = set(), threading.Lock()

    def worker():
        while True:
            job = jobs.get()
            if job is None:
                return
            try:
                if not stop.is_set():
                    download_one(db, http, stop, *job)
            except Stopped:
                pass
            except Exception:
                log.exception("Error tak terduga pada %s", job[0])
            finally:
                with inflight_lock:
                    inflight.discard(job[0])

    threads = [threading.Thread(target=worker, name=f"dl-{i}", daemon=True) for i in range(WORKERS)]
    for t in threads:
        t.start()
    try:
        while not stop.is_set():
            rows = db.query("SELECT id, url FROM docs WHERE status='PENDING' AND next_try<=? "
                            "ORDER BY next_try, rowid LIMIT 200", (time.time(),))
            with inflight_lock:
                rows = [row for row in rows if row[0] not in inflight]
                inflight.update(row[0] for row in rows)
            for row in rows:
                while not stop.is_set():
                    try:
                        jobs.put(row, timeout=1)
                        break
                    except queue.Full:
                        pass
            if rows:
                continue
            with inflight_lock:
                busy = bool(inflight)
            pending = db.scalar("SELECT COUNT(*) FROM docs WHERE status='PENDING'")
            if crawl_done.is_set() and not busy and pending == 0:
                log.info("Semua putusan sudah diproses.")
                break
            stop.wait(2 if not crawl_done.is_set() else 10)
    finally:
        for _ in threads:
            jobs.put(None)
        for t in threads:
            t.join()


# ---------------------------------------------------------- statistik/laporan
def collect_stats(db):
    counts = dict(db.query("SELECT status, COUNT(*) FROM docs GROUP BY status"))
    hour_ago = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
    day_ago = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 86400))
    slices = dict(db.query("SELECT status, COUNT(*) FROM slices GROUP BY status"))
    s = {
        "total": sum(counts.values()),
        "done": counts.get("DONE", 0), "pending": counts.get("PENDING", 0),
        "failed": counts.get("FAILED", 0), "no_pdf": counts.get("NO_PDF", 0),
        "last_hour": db.scalar("SELECT COUNT(*) FROM docs WHERE status='DONE' AND updated_at>=?", (hour_ago,)),
        "last_day": db.scalar("SELECT COUNT(*) FROM docs WHERE status='DONE' AND updated_at>=?", (day_ago,)),
        "bytes": db.scalar("SELECT COALESCE(SUM(size), 0) FROM docs WHERE status='DONE'"),
        "pages_done": db.scalar("SELECT COUNT(*) FROM pages WHERE status='DONE'"),
        "pages_failed": db.scalar("SELECT COUNT(*) FROM pages WHERE status='FAILED'"),
        "slices_total": sum(slices.values()),
        "slices_done": slices.get("DONE", 0) + slices.get("SPLIT", 0),
        "slices_failed": slices.get("FAILED", 0),
        "expected_total": int(db.get_kv("expected_total", 0)),
        "crawl_finished": db.get_kv("crawl_finished"),
        "heartbeat": float(db.get_kv("heartbeat", 0)),
        "stopped_at": float(db.get_kv("stopped_at", 0)),
        "disk_free": shutil.disk_usage(DOWNLOAD_DIR).free,
    }
    # perkiraan total: jumlah menurut situs (kartu "Tahun"), atau yang sudah terdaftar
    s["estimate"] = max(s["total"], s["expected_total"])
    remaining = s["estimate"] - s["done"] - s["no_pdf"] - s["failed"]
    # pakai laju 1 jam terakhir (paling mutakhir), atau rata-rata 24 jam
    s["per_hour"] = s["last_hour"] or s["last_day"] / 24
    s["eta_hours"] = remaining / s["per_hour"] if s["per_hour"] else None
    started = float(db.get_kv("started_at", 0))
    s["deadline"] = started + TARGET_DAYS * 86400 if started else None
    hours_left = (s["deadline"] - time.time()) / 3600 if started else None
    s["hours_left"] = hours_left
    s["need_per_hour"] = remaining / hours_left if hours_left and hours_left > 0 else None
    s["remaining"] = remaining
    return s


def format_duration(hours):
    return f"{hours:.0f} jam" if hours < 48 else f"{hours / 24:.1f} hari"


def target_lines(s):
    if not s["deadline"]:
        return []
    deadline = time.strftime("%d-%m-%Y %H:%M", time.localtime(s["deadline"]))
    head = f"- Target {TARGET_DAYS:g} hari: paling lambat {deadline}"
    if s["remaining"] <= 0:
        return [head + " -> SELESAI"]
    if s["hours_left"] <= 0:
        return [head + f" -> LEWAT TARGET, sisa {s['remaining']:,} putusan"]
    need = s["need_per_hour"]
    if s["per_hour"] >= need:
        verdict = "AMAN"
    else:
        verdict = "TERTINGGAL: naikkan REQ_PER_SEC/WORKERS atau cek jumlah Gagal"
    return [head + f" (sisa {format_duration(s['hours_left'])})",
            f"- Laju sekarang: {s['per_hour']:,.0f}/jam | Laju minimal: {need:,.0f}/jam -> {verdict}"]


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


def format_stats(s):
    if s["heartbeat"] and time.time() - s["heartbeat"] < 180 and s["stopped_at"] < s["heartbeat"]:
        bot = "BERJALAN"
    elif s["heartbeat"]:
        since = max(s["heartbeat"], s["stopped_at"])
        bot = "BERHENTI sejak " + time.strftime("%d-%m %H:%M", time.localtime(since))
    else:
        bot = "belum pernah jalan"
    try:
        requests.get(f"{BYPASS_URL}/cache/stats", timeout=10).raise_for_status()
        bypass = "OK"
    except requests.RequestException:
        bypass = "MATI"
    pct = 100 * s["done"] / s["estimate"] if s["estimate"] else 0
    crawl = "selesai" if s["crawl_finished"] else "berjalan"
    crawl += (f", {s['slices_done']:,}/{s['slices_total']:,} bagian, {s['pages_done']:,} halaman"
              f" (gagal: {s['slices_failed']} bagian, {s['pages_failed']} halaman)")
    site = f" dari ±{s['expected_total']:,} di situs" if s["expected_total"] else ""
    if s["remaining"] <= 0:
        eta = "sudah selesai"
    else:
        eta = "-" if s["eta_hours"] is None else "±" + format_duration(s["eta_hours"])
    avg = human_bytes(s["bytes"] / s["done"]) if s["done"] else "-"
    return "\n".join([
        f"Laporan Bot Putusan MA {YEAR} ({time.strftime('%d-%m-%Y %H:%M')})",
        f"- Terunduh: {s['done']:,} / ±{s['estimate']:,} ({pct:.1f}%)",
        f"- 24 jam terakhir: +{s['last_day']:,} | 1 jam terakhir: +{s['last_hour']:,}",
        f"- Antre: {s['pending']:,} | Gagal: {s['failed']:,} | Tanpa PDF: {s['no_pdf']:,}",
        f"- Crawl daftar: {crawl}",
        f"- Putusan terdaftar: {s['total']:,}{site}",
        f"- Ukuran: {human_bytes(s['bytes'])} (rata-rata {avg}/file) | Sisa disk: {human_bytes(s['disk_free'])}",
        f"- Perkiraan selesai: {eta}",
        *target_lines(s),
        f"- Bot: {bot} | Server bypass: {bypass}",
    ])


def write_report(db):
    """Tambahkan laporan progres ke REPORT_FILE dan kembalikan teksnya."""
    text = format_stats(collect_stats(db))
    with open(REPORT_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n\n")
    return text


def export_csv(db, path):
    fields = ["Tingkat Proses", "Klasifikasi", "Lembaga Peradilan", "Tanggal Dibacakan", "Amar"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "nomor", "judul", "tgl_putus", "tgl_upload", *fields,
                         "status", "file", "nama_file", "pdf_url", "url"])
        # dibaca bertahap: 900 rb baris sekaligus bisa menghabiskan RAM
        cursor = db.conn.execute("SELECT id, nomor, judul, tgl_putus, tgl_upload, meta, status, file, "
                                 "nama_file, pdf_url, url FROM docs ORDER BY rowid")
        n = 0
        for row in cursor:
            meta = json.loads(row[5]) if row[5] else {}
            writer.writerow([*row[:5], *(meta.get(k, "") for k in fields), *row[6:]])
            n += 1
    print(f"{n:,} baris diekspor ke {path}")


# ----------------------------------------------------------------------- main
def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    logfile = logging.handlers.RotatingFileHandler(
        os.path.join(DOWNLOAD_DIR, "bot.log"), maxBytes=20 * 1024 ** 2, backupCount=5, encoding="utf-8")
    logfile.setFormatter(fmt)
    root.addHandler(logfile)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bot unduh putusan Mahkamah Agung")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "crawl", "download", "update", "stats", "report", "export", "retry-failed"])
    parser.add_argument("output", nargs="?", help="file CSV untuk perintah export")
    args = parser.parse_args(argv)

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    db = DB(DB_NAME)
    if args.command == "stats":
        return print(format_stats(collect_stats(db)))
    if args.command == "report":
        return print(write_report(db))
    if args.command == "export":
        return export_csv(db, args.output or os.path.join(DOWNLOAD_DIR, "metadata.csv"))
    if args.command == "retry-failed":
        n = db.execute("UPDATE docs SET status='PENDING', attempts=0, next_try=0 WHERE status='FAILED'").rowcount
        # bagian yang punya halaman gagal ditelusuri ulang (halaman yang sudah selesai dilewati)
        db.execute("UPDATE slices SET status='CRAWL' WHERE status='DONE' AND path IN "
                   "(SELECT slice FROM pages WHERE status='FAILED')")
        p = db.execute("DELETE FROM pages WHERE status='FAILED'").rowcount
        b = db.execute("UPDATE slices SET status='NEW', attempts=0 WHERE status='FAILED'").rowcount
        if p or b:
            db.execute("DELETE FROM kv WHERE k='crawl_finished'")
        return print(f"{n:,} putusan, {b:,} bagian, dan {p:,} halaman diantrekan ulang")

    setup_logging()
    stop = threading.Event()

    def on_signal(signum, _frame):
        if stop.is_set():
            os._exit(1)  # sinyal kedua: keluar paksa
        log.warning("Berhenti... menunggu pekerjaan yang sedang berjalan (Ctrl+C lagi untuk paksa)")
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    def heartbeat():
        # tanda bot masih hidup (untuk status di laporan) + laporan berkala
        minutes = 0
        while not stop.wait(60):
            minutes += 1
            try:
                db.set_kv("heartbeat", time.time())
                if REPORT_EVERY_MIN > 0 and minutes % REPORT_EVERY_MIN == 0:
                    log.info("Laporan:\n%s", write_report(db))
            except Exception:
                log.exception("Gagal memperbarui heartbeat/laporan")

    db.set_kv("heartbeat", time.time())
    if db.get_kv("started_at") is None:
        db.set_kv("started_at", time.time())
    threading.Thread(target=heartbeat, name="heartbeat", daemon=True).start()

    log.info("Mulai '%s': tahun %s, %d worker, %.1f req/detik, simpan ke %s (%s)",
             args.command, YEAR, WORKERS, REQ_PER_SEC, DOWNLOAD_DIR,
             "curl_cffi" if cffi else "requests (pasang curl_cffi!)")
    http = Http(stop)
    crawl_done = threading.Event()
    try:
        if args.command == "update":
            update(db, http, stop)
        elif args.command == "crawl":
            crawl(db, http, stop)
        elif args.command == "download":
            crawl_done.set()
            download(db, http, stop, crawl_done)
        else:
            def crawl_thread():
                # gagal crawl tidak menghentikan unduhan antrean yang sudah ada
                try:
                    for attempt in range(5):
                        try:
                            if db.get_kv("crawl_finished"):
                                return update(db, http, stop)
                            return crawl(db, http, stop)
                        except TransientError as e:
                            log.error("Crawl gagal (%s), coba lagi dalam 5 menit", e)
                            if stop.wait(300):
                                return
                except Stopped:
                    pass
                except Exception:
                    log.exception("Crawl berhenti karena error; unduhan antrean tetap jalan")
                finally:
                    crawl_done.set()

            threading.Thread(target=crawl_thread, name="crawl", daemon=True).start()
            download(db, http, stop, crawl_done)
            # sebelum selesai, ambil putusan yang diunggah selama bot berjalan
            while not stop.is_set() and update(db, http, stop) > 0:
                download(db, http, stop, crawl_done)
    except Stopped:
        pass
    finally:
        stop.set()
        db.set_kv("stopped_at", time.time())
        log.info("Laporan akhir:\n%s", write_report(db))


if __name__ == "__main__":
    main()
