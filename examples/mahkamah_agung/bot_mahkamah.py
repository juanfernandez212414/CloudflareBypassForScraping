"""
Bot unduh PDF putusan Mahkamah Agung (putusan3.mahkamahagung.go.id) skala besar.

Alur kerja dua fase, semuanya tercatat di SQLite sehingga bisa dilanjutkan:
  1. crawl   : telusuri halaman daftar -> simpan URL detail putusan ke tabel `docs`
  2. download: untuk tiap putusan PENDING -> buka halaman detail -> unduh PDF

Clearance Cloudflare diambil dari server bypass lokal (endpoint /cookies).
cf_clearance terikat ke IP + User-Agent (+ sidik jari TLS), jadi bot ini HARUS
berjalan di mesin/IP yang sama dengan container bypass.

Pemakaian:
  python bot_mahkamah.py            # crawl lalu download
  python bot_mahkamah.py crawl
  python bot_mahkamah.py download
  python bot_mahkamah.py stats
"""
import os
import re
import sys
import time
import random
import sqlite3
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    # curl_cffi meniru sidik jari TLS Chrome; requests biasa sering tetap kena
    # challenge walau cookie cf_clearance sudah benar.
    from curl_cffi import requests as http
    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:  # pragma: no cover
    http = requests
    IMPERSONATE = {}

# ---------------------------------------------------------------- konfigurasi
BASE_URL = os.environ.get("MA_BASE_URL", "https://putusan3.mahkamahagung.go.id")
YEAR = os.environ.get("MA_YEAR", "2025")
LIST_URL = BASE_URL + "/direktori/index/tahunjenis/putus/tahun/{year}/page/{page}.html"
BYPASS_URL = os.environ.get("BYPASS_URL", "http://localhost:8000")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", f"/mnt/data_1_5TB/putusan_{YEAR}")
DB_NAME = os.environ.get("DB_NAME", os.path.join(DOWNLOAD_DIR, "bot_state.db"))
WORKERS = int(os.environ.get("WORKERS", "2"))          # jangan terlalu tinggi
DELAY = float(os.environ.get("DELAY", "1.0"))          # jeda per request per worker
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "5"))
TIMEOUT = 60

# Selector bisa diubah lewat env tanpa edit kode. Bila selector tidak cocok,
# bot jatuh ke pola regex pada href (lihat find_detail_links / find_pdf_link).
SEL_DETAIL = os.environ.get("SEL_DETAIL", "div.spost strong a")
SEL_PDF = os.environ.get("SEL_PDF", "a[href*='download_file'][href*='/pdf/']")
RE_DETAIL = re.compile(r"/direktori/putusan/([0-9a-f]+)\.html")
RE_PDF = re.compile(r"/direktori/download_file/[0-9a-f]+/pdf/")

CHALLENGE_MARKERS = ("Just a moment", "cf-chl", "challenge-platform", "Attention Required")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot_ma")


# ------------------------------------------------------------------- database
def db_connect():
    conn = sqlite3.connect(DB_NAME, timeout=60, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pages (
            page INTEGER PRIMARY KEY, status TEXT, n_links INTEGER, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS docs (
            id TEXT PRIMARY KEY, url TEXT, status TEXT DEFAULT 'PENDING',
            attempts INTEGER DEFAULT 0, file TEXT, size INTEGER, error TEXT,
            updated_at TEXT);
        CREATE INDEX IF NOT EXISTS docs_status ON docs(status);
    """)
    return conn


db_lock = threading.Lock()


def db_exec(conn, sql, args=()):
    with db_lock:
        conn.execute(sql, args)
        conn.commit()


# ---------------------------------------------------------------- HTTP client
class BlockedError(Exception):
    pass


class Client:
    """Menyimpan cookie+UA bersama dan memperbaruinya otomatis saat diblokir."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cookies, self.user_agent = {}, ""
        self.generation = 0
        self.refresh(force=False)

    def refresh(self, force=True, seen_generation=None):
        with self.lock:
            # thread lain sudah memperbarui saat kita menunggu lock
            if seen_generation is not None and seen_generation != self.generation:
                return
            if force:
                # /cookies selalu mengembalikan cache (s.d. ~29 menit). Paksa
                # pembuatan cookie baru lewat /html?bypassCookieCache=true.
                log.warning("Clearance ditolak, memaksa pembuatan cookie baru...")
                requests.get(f"{BYPASS_URL}/html", params={
                    "url": BASE_URL, "bypassCookieCache": "true"}, timeout=180)
            log.info("Meminta clearance dari bypass server...")
            r = requests.get(f"{BYPASS_URL}/cookies", params={"url": BASE_URL}, timeout=180)
            r.raise_for_status()
            data = r.json()
            self.cookies, self.user_agent = data["cookies"], data["user_agent"]
            self.generation += 1
            log.info("Clearance OK (cookie: %s)", ", ".join(self.cookies))

    def get(self, url, **kw):
        for attempt in range(3):
            gen = self.generation
            headers = {"User-Agent": self.user_agent, "Referer": BASE_URL + "/"}
            try:
                r = http.get(url, headers=headers, cookies=self.cookies,
                             timeout=TIMEOUT, **IMPERSONATE, **kw)
            except Exception as e:
                log.warning("Error jaringan %s: %s", url, e)
                time.sleep(5 * (attempt + 1))
                continue
            ctype = r.headers.get("content-type", "")
            blocked = r.status_code in (403, 503) or (
                "text/html" in ctype and any(m in r.text[:5000] for m in CHALLENGE_MARKERS))
            if blocked:
                self.refresh(force=True, seen_generation=gen)
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("retry-after", 60))
                log.warning("429 Too Many Requests, tidur %ss", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        raise BlockedError(f"Gagal mengambil {url} setelah 3 percobaan")


def polite_sleep():
    time.sleep(DELAY + random.uniform(0, DELAY / 2))


# --------------------------------------------------------------------- parsing
def find_detail_links(html):
    soup = BeautifulSoup(html, "html.parser")
    hrefs = [a.get("href", "") for a in soup.select(SEL_DETAIL)]
    if not any(RE_DETAIL.search(h) for h in hrefs):
        hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
    out = {}
    for h in hrefs:
        m = RE_DETAIL.search(h)
        if m:
            out[m.group(1)] = urljoin(BASE_URL, h)
    return out


def find_pdf_link(html):
    soup = BeautifulSoup(html, "html.parser")
    a = soup.select_one(SEL_PDF)
    if a is None:
        a = next((x for x in soup.find_all("a", href=True) if RE_PDF.search(x["href"])), None)
    return urljoin(BASE_URL, a["href"]) if a else None


def pdf_path(doc_id):
    # 900rb file dalam satu folder memperlambat filesystem; pecah per 2 karakter.
    sub = os.path.join(DOWNLOAD_DIR, doc_id[:2])
    os.makedirs(sub, exist_ok=True)
    return os.path.join(sub, doc_id + ".pdf")


# --------------------------------------------------------------------- fase 1
def crawl(conn, client):
    row = conn.execute("SELECT MAX(page) FROM pages WHERE status='DONE'").fetchone()
    page = (row[0] or 0) + 1
    empty_streak = 0
    while empty_streak < 3:
        url = LIST_URL.format(year=YEAR, page=page)
        try:
            links = find_detail_links(client.get(url).text)
        except Exception as e:
            log.error("Halaman %d gagal: %s (lanjut lagi nanti)", page, e)
            time.sleep(60)
            continue
        with db_lock:
            conn.executemany("INSERT OR IGNORE INTO docs (id, url) VALUES (?, ?)", links.items())
            conn.execute("INSERT OR REPLACE INTO pages VALUES (?, 'DONE', ?, datetime('now','localtime'))",
                         (page, len(links)))
            conn.commit()
        log.info("Halaman %d: %d putusan", page, len(links))
        empty_streak = empty_streak + 1 if not links else 0
        page += 1
        polite_sleep()
    log.info("Crawl selesai di halaman %d", page - 1)


# --------------------------------------------------------------------- fase 2
def download_one(conn, client, doc_id, url):
    path = pdf_path(doc_id)
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            db_exec(conn, "UPDATE docs SET status='DONE', file=? WHERE id=?", (path, doc_id))
            return
        pdf_url = find_pdf_link(client.get(url).text)
        polite_sleep()
        if not pdf_url:
            db_exec(conn, "UPDATE docs SET status='NO_PDF', updated_at=datetime('now','localtime') WHERE id=?", (doc_id,))
            return
        r = client.get(pdf_url)
        body = r.content
        # tanpa cek ini, halaman challenge/error HTML bisa tersimpan sebagai .pdf
        if not body.startswith(b"%PDF"):
            raise ValueError(f"bukan PDF (content-type={r.headers.get('content-type')})")
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
        db_exec(conn, "UPDATE docs SET status='DONE', file=?, size=?, error=NULL, "
                      "updated_at=datetime('now','localtime') WHERE id=?", (path, len(body), doc_id))
    except Exception as e:
        log.warning("Gagal %s: %s", doc_id, e)
        db_exec(conn, "UPDATE docs SET attempts=attempts+1, error=?, status=CASE WHEN attempts+1>=? "
                      "THEN 'FAILED' ELSE 'PENDING' END, updated_at=datetime('now','localtime') WHERE id=?",
                (str(e)[:500], MAX_ATTEMPTS, doc_id))
    finally:
        polite_sleep()


def download(conn, client):
    while True:
        batch = conn.execute("SELECT id, url FROM docs WHERE status='PENDING' "
                             "ORDER BY attempts, rowid LIMIT 500").fetchall()
        if not batch:
            break
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for doc_id, url in batch:
                pool.submit(download_one, conn, client, doc_id, url)
        stats(conn)
    log.info("Tidak ada lagi putusan PENDING.")


def stats(conn):
    rows = conn.execute("SELECT status, COUNT(*) FROM docs GROUP BY status").fetchall()
    pages = conn.execute("SELECT COUNT(*) FROM pages WHERE status='DONE'").fetchone()[0]
    log.info("Halaman: %d | %s", pages, " | ".join(f"{s}: {n}" for s, n in rows))


def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    conn = db_connect()
    if mode == "stats":
        return stats(conn)
    client = Client()
    if mode in ("all", "crawl"):
        crawl(conn, client)
    if mode in ("all", "download"):
        download(conn, client)
    stats(conn)


if __name__ == "__main__":
    main()
