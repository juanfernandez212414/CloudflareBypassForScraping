"""Test bot_mahkamah dengan server tiruan yang meniru struktur asli situs MA:
tab samping di halaman daftar, paginasi yang dipotong situs, filter sidebar
(Direktori/Klasifikasi/Pengadilan/Tahun) beserta jumlahnya, dan Cloudflare.

Jalankan: pytest -q test_bot_mahkamah.py
"""
import csv
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bot_mahkamah as bot  # noqa: E402

PER_PAGE, CAP = 4, 5   # situs tiruan: 4 putusan/halaman, paginasi dipotong di 5 halaman
TOP = ["pidana-umum-1", "perdata-agama-1", "perdata-1"]
SUB = {"perceraian": "perdata-agama-1", "waris": "perdata-agama-1"}


def make_docs():
    docs = []

    def add(court, top, sub, n):
        for _ in range(n):
            docs.append({"id": f"11f1a5{len(docs):04d}ab31353438", "court": court, "top": top, "sub": sub})

    add("pn-a", "pidana-umum-1", None, 6)
    # PA B melebihi batas -> dipecah per kategori; perdata agama masih melebihi
    # -> dipecah per klasifikasi. 4 putusan hanya berkategori induk.
    add("pa-b", "perdata-agama-1", None, 4)
    add("pa-b", "perdata-agama-1", "perceraian", 14)
    add("pa-b", "perdata-agama-1", "waris", 9)
    add("pa-b", "perdata-1", None, 3)
    add("pn-c", "pidana-umum-1", None, 5)
    return docs


DOCS = make_docs()
NO_PDF_ID = DOCS[-1]["id"]                                       # di pn-c
NOT_PDF_ID = next(d["id"] for d in DOCS if d["sub"] == "waris")  # PDF rusak


def matches(doc, filters):
    if filters.get("tahun", "2025") != "2025" or filters.get("tahunjenis", "putus") != "putus":
        return False
    if "pengadilan" in filters and doc["court"] != filters["pengadilan"]:
        return False
    kat = filters.get("kategori")
    return kat is None or kat in (doc["top"], doc["sub"])


def spost(base, doc):
    return f"""<div class="spost clearfix"><div class="entry-c">
      <div class="small"><a href="{base}/pengadilan.html">Pengadilan</a></div>
      <div class="small"><i class="icon-time color"></i><strong>Register :</strong> 13-05-2025 &#8212;
        <strong>Putus :</strong> 24-06-2025 &#8212; <strong>Upload :</strong> 31-08-2025</div>
      <strong><a href="{base}/direktori/putusan/{doc['id']}.html">Putusan {doc['court']} {doc['id'][-8:]}</a></strong>
      <br/><div>Tanggal 24 Juni 2025</div></div></div>"""


def card(title, entries):
    rows = "".join(f'<div class="form-check"><p class="card-text"><a href="{href}">{label} '
                   f'<span class="badge badge-success">{count}</span></a></p></div>'
                   for href, label, count in entries if count)
    return f"""<div class="card bg-success mb-3"><div class="card-header"><h5><div class="togglet">
      {title}</div></h5></div><div class="card-body"><div class="form-check"><p class="card-text">
      <a href="#">Semua</a></p></div>{rows}</div></div>"""


def list_page(base, filters, page):
    url = lambda f: base + bot.filters_path(f) + ".html"  # noqa: E731
    found = [d for d in DOCS if matches(d, filters)]
    shown_last = min(max(1, math.ceil(len(found) / PER_PAGE)), CAP)
    rows = found[(page - 1) * PER_PAGE: page * PER_PAGE] if page <= shown_last else []
    side = "".join(spost(base, {"id": f"sidebar{i}", "court": "x"}) for i in range(3))
    links = "".join(f'<li><a href="{base}{bot.filters_path(filters)}/page/{n}.html" class="page-link" '
                    f'data-ci-pagination-page="{n}">{n}</a></li>' for n in range(2, shown_last + 1))
    no_court = {k: v for k, v in filters.items() if k != "pengadilan"}
    courts = sorted({d["court"] for d in DOCS})
    kat = filters.get("kategori")
    sub_cats = [c for c, parent in SUB.items() if parent == kat] if kat in TOP else []
    cards = (
        card("Direktori", [(base + f"/direktori/index/kategori/{t}.html", t, sum(d["top"] == t for d in DOCS))
                           for t in TOP])
        + card("Klasifikasi", [(url(dict(filters, kategori=c)), c, sum(matches(d, dict(filters, kategori=c))
                                                                        for d in DOCS)) for c in sub_cats])
        + card("Pengadilan", [(url(dict(no_court, pengadilan=c)), c.upper(),
                               sum(matches(d, dict(no_court, pengadilan=c)) for d in DOCS)) for c in courts])
        + card("Tahun", [(url(dict(filters, tahunjenis="putus", tahun="2025")), "2025", len(found))]))
    return f"""<html><body><div class="row"><div class="col_two_third">
      <div id="tabs-1"><div id="popular-post-list-sidebar">{''.join(spost(base, d) for d in rows)}</div></div>
      <div id="tabs-2"><div id="popular-post-list-sidebar">{side}</div></div>
      <div id="tabs-3"><div id="popular-post-list-sidebar">{side}</div></div>
      <ul class="pagination">{links}</ul></div><div class="sidebar">{cards}</div></div></body></html>"""


def detail_page(base, did, with_pdf=True):
    pdf = (f'<li><a href="{base}/direktori/download_file/ff{did}/pdf/{did}">1_Pdt.G/2025.pdf</a></li>'
           if with_pdf else "")
    return f"""<html><body><table class="table">
      <tr><td colspan="2"><h2>Putusan PA B</h2></td></tr>
      <tr><td><b>Nomor</b></td><td>{did[-4:]}/Pdt.G/2025</td></tr>
      <tr><td><b>Lembaga Peradilan</b></td><td>PA B</td></tr>
      <tr><td><b>Tanggal Dibacakan</b></td><td>24 Juni 2025</td></tr></table>
      <ul><li>Download Zip</li><li><a href="{base}/direktori/download_file/ff{did}/zip/{did}">zip</a></li>
      {pdf}</ul></body></html>"""


class Stub(BaseHTTPRequestHandler):
    """Gabungan server bypass + situs MA. Cookie 'basi' ditolak (challenge);
    hanya cookie baru dari /html?bypassCookieCache=true yang diterima."""

    state = {"fresh": False, "browser_runs": 0}

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="text/html; charset=utf-8", headers=()):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        path = self.path
        if path.startswith("/cookies"):
            value = "fresh" if self.state["fresh"] else "stale"
            return self.send(200, '{"cookies": {"cf_clearance": "%s"}, "user_agent": "UA/1"}' % value,
                             "application/json")
        if path.startswith("/html"):
            assert "bypassCookieCache=true" in path
            self.state["fresh"] = True
            self.state["browser_runs"] += 1
            return self.send(200, "ok")
        if path.startswith("/cache/stats"):
            return self.send(200, "{}", "application/json")
        cookie = self.headers.get("cookie") or ""
        if "cf_clearance=fresh" not in cookie or self.headers.get("user-agent") != "UA/1":
            return self.send(403, "<title>Just a moment...</title>")
        if path.startswith("/direktori/index/"):
            page = re.search(r"/page/(\d+)\.html$", path)
            return self.send(200, list_page(base, bot.parse_filters(path), int(page.group(1)) if page else 1))
        m = re.search(r"/direktori/putusan/(\w+)\.html$", path)
        if m:
            return self.send(200, detail_page(base, m.group(1), with_pdf=m.group(1) != NO_PDF_ID))
        m = re.search(r"/download_file/\w+/pdf/(\w+)$", path)
        if m:
            if m.group(1) == NOT_PDF_ID:
                return self.send(200, "<html>Maaf, file tidak tersedia</html>")
            return self.send(200, b"%PDF-1.4 " + m.group(1).encode() + b"\n%%EOF\n", "application/pdf")
        self.send(404, "")


@pytest.fixture
def stub():
    Stub.state.update(fresh=False, browser_runs=0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def run_bot(base, tmp_path, *args):
    env = dict(os.environ, MA_BASE_URL=base, BYPASS_URL=base, MA_YEAR="2025", MA_CAP_PAGES=str(CAP),
               DOWNLOAD_DIR=str(tmp_path), REQ_PER_SEC="100", WORKERS="3", MAX_ATTEMPTS="1",
               MIN_FREE_GB="0", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    return subprocess.run([sys.executable, os.path.join(HERE, "bot_mahkamah.py"), *args],
                          env=env, capture_output=True, text=True, timeout=120)


def test_filters_roundtrip():
    href = "https://putusan3.mahkamahagung.go.id/direktori/index/pengadilan/pn-x/kategori/korupsi-1/page/3.html"
    f = bot.parse_filters(href)
    assert f == {"pengadilan": "pn-x", "kategori": "korupsi-1"}
    assert bot.filters_path(dict(f, tahun="2025", tahunjenis="putus")) == \
        "/direktori/index/pengadilan/pn-x/kategori/korupsi-1/tahunjenis/putus/tahun/2025"
    assert bot.ROOT_PATH == "/direktori/index/tahunjenis/putus/tahun/2025"


def test_parse_list_ignores_sidebar_tabs_and_reads_facets():
    items, last, facets = bot.parse_list(list_page("https://x", {"tahunjenis": "putus", "tahun": "2025"}, 1))
    assert [i["id"] for i in items] == [d["id"] for d in DOCS[:PER_PAGE]]
    assert last == CAP
    assert items[0]["tgl_putus"] == "24-06-2025" and items[0]["tgl_upload"] == "31-08-2025"
    courts = {f["pengadilan"]: count for f, _, count in facets["pengadilan"]}
    assert courts == {"pa-b": 30, "pn-a": 6, "pn-c": 5}
    assert ({"tahunjenis": "putus", "tahun": "2025"}, "2025", len(DOCS)) in facets["tahun"]


def test_split_order():
    facets = bot.parse_list(list_page("https://x", {"tahun": "2025", "tahunjenis": "putus"}, 1))[2]
    dim, kids, crawl_parent = bot.split_children({"tahun": "2025", "tahunjenis": "putus"}, facets)
    assert dim == "pengadilan" and not crawl_parent and len(kids) == 3
    f = {"pengadilan": "pa-b", "tahun": "2025", "tahunjenis": "putus"}
    dim, kids, _ = bot.split_children(f, bot.parse_list(list_page("https://x", f, 1))[2])
    assert dim == "kategori" and len(kids) == len(TOP)
    f["kategori"] = "perdata-agama-1"
    dim, kids, crawl_parent = bot.split_children(f, bot.parse_list(list_page("https://x", f, 1))[2])
    assert dim == "klasifikasi" and crawl_parent and len(kids) == 2


def test_parse_detail_picks_pdf_not_zip():
    meta, pdf_url, name = bot.parse_detail(detail_page(bot.BASE_URL, "abc123"))
    assert meta["Nomor"] == "c123/Pdt.G/2025" and meta["Lembaga Peradilan"] == "PA B"
    assert pdf_url.endswith("/pdf/abc123") and name == "1_Pdt.G/2025.pdf"
    assert bot.parse_detail(detail_page(bot.BASE_URL, "abc123", with_pdf=False))[1] is None


def test_end_to_end(stub, tmp_path):
    result = run_bot(stub, tmp_path)
    assert result.returncode == 0, result.stderr

    db = sqlite3.connect(tmp_path / "bot_state.db")
    status = dict(db.execute("SELECT id, status FROM docs"))
    # semua putusan terjangkau walau tiap daftar dipotong situs di 20 putusan,
    # termasuk yang hanya berkategori induk; tanpa putusan dari tab samping
    assert set(status) == {d["id"] for d in DOCS}
    assert status[NO_PDF_ID] == "NO_PDF"
    assert status[NOT_PDF_ID] == "FAILED"
    assert sum(s == "DONE" for s in status.values()) == len(DOCS) - 2
    for (path,) in db.execute("SELECT file FROM docs WHERE status='DONE'"):
        assert open(path, "rb").read(4) == b"%PDF"
    assert not list(tmp_path.rglob("*.part"))
    assert db.execute("SELECT v FROM kv WHERE k='crawl_finished'").fetchone()
    assert db.execute("SELECT v FROM kv WHERE k='expected_total'").fetchone() == (str(len(DOCS)),)
    slices = dict(db.execute("SELECT path, status FROM slices"))
    assert slices[bot.ROOT_PATH] == "SPLIT"
    assert slices["/direktori/index/pengadilan/pa-b/tahunjenis/putus/tahun/2025"] == "SPLIT"
    assert slices["/direktori/index/pengadilan/pa-b/kategori/perdata-agama-1/tahunjenis/putus/tahun/2025"] == "DONE"
    assert set(slices.values()) == {"SPLIT", "DONE"}
    # cookie basi ditolak -> satu kali pembuatan clearance baru, bukan berulang
    assert Stub.state["browser_runs"] == 1

    # dijalankan ulang: tidak ada yang diunduh ulang, hanya cek putusan baru
    mtimes = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*.pdf")}
    assert run_bot(stub, tmp_path).returncode == 0
    assert {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*.pdf")} == mtimes

    # retry-failed mengantrekan ulang yang gagal
    assert "1 putusan" in run_bot(stub, tmp_path, "retry-failed").stdout
    assert db.execute("SELECT status FROM docs WHERE id=?", (NOT_PDF_ID,)).fetchone()[0] == "PENDING"

    stats = run_bot(stub, tmp_path, "stats").stdout
    assert f"Terunduh: {len(DOCS) - 2} / ±{len(DOCS)}" in stats
    assert f"Putusan terdaftar: {len(DOCS)} dari ±{len(DOCS)} di situs" in stats
    assert "Server bypass: OK" in stats
    assert "Target 7 hari: paling lambat" in stats

    # laporan ditulis ke file (laporan akhir tiap run + perintah report)
    report = tmp_path / "laporan.log"
    before = report.read_text(encoding="utf-8").count("Laporan Bot Putusan MA")
    assert run_bot(stub, tmp_path, "report").returncode == 0
    assert report.read_text(encoding="utf-8").count("Laporan Bot Putusan MA") == before + 1

    out = tmp_path / "meta.csv"
    assert run_bot(stub, tmp_path, "export", str(out)).returncode == 0
    rows = list(csv.DictReader(open(out, encoding="utf-8")))
    assert len(rows) == len(DOCS)
    assert {r["Lembaga Peradilan"] for r in rows if r["status"] == "DONE"} == {"PA B"}


def test_migrates_old_database(stub, tmp_path):
    # DB versi lama: tabel pages tanpa kolom slice
    old = sqlite3.connect(tmp_path / "bot_state.db")
    old.executescript("""
        CREATE TABLE pages (page INTEGER PRIMARY KEY, status TEXT, n_links INTEGER);
        CREATE TABLE docs (id TEXT PRIMARY KEY, url TEXT, status TEXT, attempts INTEGER,
                           file TEXT, size INTEGER, error TEXT, updated_at TEXT);
        INSERT INTO pages VALUES (1, 'DONE', 20);
    """)
    old.commit()
    old.close()
    result = run_bot(stub, tmp_path)
    assert result.returncode == 0, result.stderr
    db = sqlite3.connect(tmp_path / "bot_state.db")
    assert db.execute("SELECT COUNT(*) FROM docs WHERE status='DONE'").fetchone()[0] == len(DOCS) - 2


def test_check_command(stub, tmp_path):
    result = run_bot(stub, tmp_path, "check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SEMUA OK" in result.stdout
    assert "per pengadilan: 3 bagian" in result.stdout
    assert (tmp_path / "cek_contoh.pdf").read_bytes().startswith(b"%PDF")
    # uji awal tidak mengubah progres maupun memulai hitungan target 7 hari
    db = sqlite3.connect(tmp_path / "bot_state.db")
    assert db.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM kv WHERE k='started_at'").fetchone()[0] == 0


def test_check_reports_bypass_down(tmp_path):
    result = run_bot("http://127.0.0.1:9", tmp_path, "check")
    assert result.returncode == 1
    assert "[GAGAL] Server bypass" in result.stdout
