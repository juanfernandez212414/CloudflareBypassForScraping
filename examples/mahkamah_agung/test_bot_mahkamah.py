"""Test bot_mahkamah dengan HTML tiruan yang meniru struktur asli situs MA.

Jalankan: pytest -q test_bot_mahkamah.py
"""
import csv
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

PAGES, PER_PAGE = 3, 4


def doc_id(page, i):
    return f"11f1a5{page:02d}{i:02d}ab3135343834{page}{i}"


def spost(base, did, title):
    return f"""<div class="spost clearfix"><div class="entry-c">
      <div class="small"><a href="{base}/pengadilan.html">Pengadilan</a></div>
      <div class="small"><i class="icon-time color"></i><strong>Register :</strong> 13-05-2025 &#8212;
        <strong>Putus :</strong> 24-06-2025 &#8212; <strong>Upload :</strong> 31-08-2025</div>
      <strong><a href="{base}/direktori/putusan/{did}.html">{title}</a></strong><br/>
      <div>Tanggal 24 Juni 2025</div></div></div>"""


def list_page(base, page):
    main = "".join(spost(base, doc_id(page, i), f"Putusan PN X Nomor {page}{i}/Pid/2025")
                   for i in range(PER_PAGE))
    # tab "Penting"/"Terpopuler" berisi putusan lain yang sama di setiap halaman
    side = "".join(spost(base, f"sidebar{i}", f"Putusan populer {i}") for i in range(5))
    links = "".join(f'<li><a href="{base}/direktori/index/tahunjenis/putus/tahun/2025/page/{n}.html" '
                    f'class="page-link" data-ci-pagination-page="{n}">{n}</a></li>' for n in range(2, PAGES + 1))
    return f"""<html><body><div class="tabs"><ul><li><a href="#tabs-1">Putusan</a></li></ul>
      <div id="tabs-1"><div id="popular-post-list-sidebar">{main}</div></div>
      <div id="tabs-2"><div id="popular-post-list-sidebar">{side}</div></div>
      <div id="tabs-3"><div id="popular-post-list-sidebar">{side}</div></div></div>
      <ul class="pagination">{links}</ul></body></html>"""


def detail_page(base, did, with_pdf=True):
    pdf = (f'<li><a href="{base}/direktori/download_file/ff{did}/pdf/{did}">1_Pid/2025.pdf</a></li>'
           if with_pdf else "")
    return f"""<html><body><table class="table">
      <tr><td colspan="2"><h2>Putusan PN X</h2></td></tr>
      <tr><td><b>Nomor</b></td><td>{did[-4:]}/Pid/2025</td></tr>
      <tr><td><b>Lembaga Peradilan</b></td><td>PN JAKARTA PUSAT</td></tr>
      <tr><td><b>Tanggal Dibacakan</b></td><td>24 Juni 2025</td></tr></table>
      <ul><li>Download Zip</li><li><a href="{base}/direktori/download_file/ff{did}/zip/{did}">zip</a></li>
      {pdf}</ul></body></html>"""


NO_PDF_ID = doc_id(2, 1)
NOT_PDF_ID = doc_id(3, 2)


class Stub(BaseHTTPRequestHandler):
    """Gabungan server bypass + situs MA. Cookie 'basi' ditolak (challenge);
    hanya cookie baru dari /html?bypassCookieCache=true yang diterima."""

    state = {"fresh": False, "browser_runs": 0}

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="text/html; charset=utf-8"):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
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
        m = re.search(r"/tahun/2025(?:/page/(\d+))?\.html$", path)
        if m:
            page = int(m.group(1) or 1)
            return self.send(200, list_page(base, page)) if page <= PAGES else self.send(404, "")
        m = re.search(r"/direktori/putusan/(\w+)\.html$", path)
        if m:
            return self.send(200, detail_page(base, m.group(1), with_pdf=m.group(1) != NO_PDF_ID))
        m = re.search(r"/download_file/\w+/pdf/(\w+)$", path)
        if m:
            if m.group(1) == NOT_PDF_ID:
                return self.send(200, "<html>Maaf, file tidak tersedia</html>")
            return self.send(200, b"%PDF-1.4 " + m.group(1).encode(), "application/pdf")
        self.send(404, "")


@pytest.fixture
def stub():
    Stub.state.update(fresh=False, browser_runs=0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def run_bot(base, tmp_path, *args):
    env = dict(os.environ, MA_BASE_URL=base, BYPASS_URL=base, MA_YEAR="2025",
               DOWNLOAD_DIR=str(tmp_path), REQ_PER_SEC="50", WORKERS="3", MAX_ATTEMPTS="1",
               MIN_FREE_GB="0", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    return subprocess.run([sys.executable, os.path.join(HERE, "bot_mahkamah.py"), *args],
                          env=env, capture_output=True, text=True, timeout=120)


def test_parse_list_ignores_sidebar_tabs():
    items, last = bot.parse_list(list_page("https://x", 1))
    assert [i["id"] for i in items] == [doc_id(1, i) for i in range(PER_PAGE)]
    assert last == PAGES
    assert items[0]["tgl_putus"] == "24-06-2025" and items[0]["tgl_upload"] == "31-08-2025"
    assert items[0]["judul"] == "Putusan PN X Nomor 10/Pid/2025"


def test_parse_detail_picks_pdf_not_zip():
    meta, pdf_url, name = bot.parse_detail(detail_page(bot.BASE_URL, "abc123"))
    assert meta["Nomor"] == "c123/Pid/2025" and meta["Lembaga Peradilan"] == "PN JAKARTA PUSAT"
    assert pdf_url.endswith("/pdf/abc123") and name == "1_Pid/2025.pdf"
    assert bot.parse_detail(detail_page(bot.BASE_URL, "abc123", with_pdf=False))[1] is None


def test_end_to_end(stub, tmp_path):
    result = run_bot(stub, tmp_path)
    assert result.returncode == 0, result.stderr

    db = sqlite3.connect(tmp_path / "bot_state.db")
    status = dict(db.execute("SELECT id, status FROM docs"))
    assert len(status) == PAGES * PER_PAGE  # tanpa putusan dari tab samping
    assert status[NO_PDF_ID] == "NO_PDF"
    assert status[NOT_PDF_ID] == "FAILED"
    assert sum(s == "DONE" for s in status.values()) == PAGES * PER_PAGE - 2
    for (path,) in db.execute("SELECT file FROM docs WHERE status='DONE'"):
        assert open(path, "rb").read(4) == b"%PDF"
    assert not list(tmp_path.rglob("*.part"))
    assert db.execute("SELECT v FROM kv WHERE k='crawl_finished'").fetchone()
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
    assert f"Terunduh: {PAGES * PER_PAGE - 2} / ±{PAGES * PER_PAGE}" in stats
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
    assert len(rows) == PAGES * PER_PAGE
    assert {r["Lembaga Peradilan"] for r in rows if r["status"] == "DONE"} == {"PN JAKARTA PUSAT"}
