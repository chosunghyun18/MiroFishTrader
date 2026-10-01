"""bitmex_public — 로컬 HTTP 서버로 S3 listing(v1)·Range 응답을 흉내 내 검증."""

import gzip
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from src.ingest import bitmex_public as bp

HEADER = "timestamp,symbol,side,size,price\n"


def make_day(rows: int) -> bytes:
    body = "".join(
        f"2020-03-12D00:00:{i % 60:02d}.000000000,{'XBTUSD' if i % 3 else 'ETHUSD'},Buy,{i},5000\n"
        for i in range(rows))
    return gzip.compress((HEADER + body).encode())


class FakeS3:
    """키 → 바이트. page_size 로 IsTruncated 페이지네이션을 강제한다."""

    def __init__(self, objects: dict[str, bytes], page_size: int = 2, honor_range: bool = True):
        self.objects = dict(sorted(objects.items()))
        self.page_size = page_size
        self.honor_range = honor_range
        self.requests: list[tuple[str, str | None]] = []
        self.fail_once_after: int | None = None  # 첫 GET 을 N 바이트에서 끊는다

    def listing(self, prefix: str, marker: str) -> bytes:
        keys = [k for k in self.objects if k.startswith(prefix) and k > marker]
        page, truncated = keys[: self.page_size], len(keys) > self.page_size
        items = "".join(
            f"<Contents><Key>{k}</Key><Size>{len(self.objects[k])}</Size>"
            f"<ETag>&quot;e{i}&quot;</ETag></Contents>" for i, k in enumerate(page))
        return (f'<?xml version="1.0"?><ListBucketResult '
                f'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>{items}"
                f"</ListBucketResult>").encode()


@pytest.fixture
def s3():
    fake = FakeS3({
        "data/trade/": b"",
        "data/trade/README.txt": b"not a day file",
        "data/trade/20200310.csv.gz": make_day(30),
        "data/trade/20200311.csv.gz": make_day(300),
        "data/trade/20200312.csv.gz": make_day(3000),
        "data/trade/20200313.csv.gz": make_day(10),
        "data/quote/20200312.csv.gz": make_day(5),
    })

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urlparse(self.path)
            rng = self.headers.get("Range")
            fake.requests.append((u.path, rng))
            if u.path == "/":
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                body = fake.listing(q.get("prefix", ""), q.get("marker", ""))
                return self._send(200, body)
            data = fake.objects.get(u.path.lstrip("/"))
            if data is None:
                return self._send(404, b"")
            start = 0
            if rng and fake.honor_range:
                start = int(rng.split("=")[1].split("-")[0])
                end = rng.split("-")[1]
                stop = int(end) + 1 if end else len(data)
                if start >= len(data):
                    return self._send(416, b"")
                return self._send(206, data[start:stop],
                                  {"Content-Range": f"bytes {start}-{stop - 1}/{len(data)}"})
            if fake.fail_once_after is not None:
                cut, fake.fail_once_after = fake.fail_once_after, None
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data[:cut])
                self.wfile.flush()
                self.close_connection = True
                return
            self._send(200, data)

        def _send(self, code, body, headers=None):
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fake.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield fake
    srv.shutdown()


def test_list_paginates_and_filters_dates(s3):
    files = bp.list_files("trade", date(2020, 3, 11), date(2020, 3, 12), base_url=s3.url)
    assert [f.name for f in files] == ["20200311.csv.gz", "20200312.csv.gz"]
    assert all(f.size == len(s3.objects[f.key]) for f in files)
    # 다른 dataset 은 섞이지 않는다
    assert [f.name for f in bp.list_files("quote", base_url=s3.url)] == ["20200312.csv.gz"]


def test_peek_header_reads_only_a_range(s3):
    f = bp.list_files("trade", date(2020, 3, 12), date(2020, 3, 12), base_url=s3.url)[0]
    lines = bp.peek_header(f, base_url=s3.url, nbytes=200)
    assert lines[0] == HEADER.strip()
    assert s3.requests[-1][1] == "bytes=0-199"


def test_download_resumes_from_part(s3, tmp_path):
    f = bp.list_files("trade", date(2020, 3, 12), date(2020, 3, 12), base_url=s3.url)[0]
    data = s3.objects[f.key]
    (tmp_path / (f.name + ".part")).write_bytes(data[:1000])
    (tmp_path / (f.name + ".part.etag")).write_text(f.etag)
    out = bp.download_file(f, tmp_path, base_url=s3.url)
    assert out.read_bytes() == data
    assert s3.requests[-1][1] == "bytes=1000-"
    assert not (tmp_path / (f.name + ".part")).exists()


def test_stale_part_is_discarded_when_etag_changes(s3, tmp_path):
    f = bp.list_files("trade", date(2020, 3, 12), date(2020, 3, 12), base_url=s3.url)[0]
    part = tmp_path / (f.name + ".part")
    part.write_bytes(b"old-object-bytes")
    (tmp_path / (f.name + ".part.etag")).write_text("old-etag")
    out = bp.download_file(f, tmp_path, base_url=s3.url)
    assert out.read_bytes() == s3.objects[f.key]
    assert s3.requests[-1][1] is None  # 처음부터 받음
    assert not list(tmp_path.glob("*.etag"))


def test_download_restarts_when_range_ignored(s3, tmp_path):
    s3.honor_range = False
    f = bp.list_files("trade", date(2020, 3, 12), date(2020, 3, 12), base_url=s3.url)[0]
    (tmp_path / (f.name + ".part")).write_bytes(b"garbage")
    (tmp_path / (f.name + ".part.etag")).write_text(f.etag)
    out = bp.download_file(f, tmp_path, base_url=s3.url)
    assert out.read_bytes() == s3.objects[f.key]


def test_download_retries_after_dropped_connection(s3, tmp_path, monkeypatch):
    monkeypatch.setattr(bp.time, "sleep", lambda s: None)
    f = bp.list_files("trade", date(2020, 3, 12), date(2020, 3, 12), base_url=s3.url)[0]
    s3.fail_once_after = 500
    out = bp.download_file(f, tmp_path, base_url=s3.url)
    assert out.read_bytes() == s3.objects[f.key]


def test_size_mismatch_is_rejected(s3, tmp_path):
    f = bp.list_files("trade", date(2020, 3, 12), date(2020, 3, 12), base_url=s3.url)[0]
    wrong = bp.RemoteFile(f.key, f.size + 5, f.etag)
    with pytest.raises(bp.IntegrityError):
        bp.download_file(wrong, tmp_path, base_url=s3.url)
    assert not any(tmp_path.iterdir())


def test_corrupt_gzip_is_rejected(s3, tmp_path):
    s3.objects["data/trade/20200314.csv.gz"] = make_day(100)[:-20] + b"x" * 20
    f = bp.list_files("trade", date(2020, 3, 14), date(2020, 3, 14), base_url=s3.url)[0]
    with pytest.raises(bp.IntegrityError):
        bp.download_file(f, tmp_path, base_url=s3.url)


def test_symbol_filter_keeps_only_requested_and_drops_raw(s3, tmp_path):
    files = bp.list_files("trade", date(2020, 3, 12), date(2020, 3, 12), base_url=s3.url)
    [out] = bp.run_download(files, tmp_path, symbols=["XBTUSD"], base_url=s3.url)
    assert out == tmp_path / "XBTUSD" / "20200312.csv.gz"
    with gzip.open(out, "rt") as g:
        lines = g.read().splitlines()
    assert lines[0] == HEADER.strip()
    assert len(lines) - 1 == 2000 and all(",XBTUSD," in l for l in lines[1:])
    assert not list((tmp_path / "_raw").iterdir())


def test_plan_skips_completed_and_counts_partial(s3, tmp_path):
    files = bp.list_files("trade", date(2020, 3, 10), date(2020, 3, 12), base_url=s3.url)
    bp.download_file(files[0], tmp_path, base_url=s3.url)
    (tmp_path / (files[1].name + ".part")).write_bytes(b"x" * 100)
    plan = bp.make_plan(files, tmp_path)
    assert [f.name for f in plan.done] == ["20200310.csv.gz"]
    assert plan.need_bytes == files[1].size + files[2].size - 100


def test_guard_rejects_over_cap_and_low_disk(tmp_path):
    f = bp.RemoteFile("data/trade/20200312.csv.gz", 3 * bp.GB, "e")
    plan = bp.Plan([f], [], f.size, 0, free_bytes=100 * bp.GB)
    with pytest.raises(bp.DiskGuardError, match="상한"):
        bp.check_guard(plan, max_bytes=2 * bp.GB, min_free_bytes=0)
    with pytest.raises(bp.DiskGuardError, match="여유"):
        bp.check_guard(plan, max_bytes=5 * bp.GB, min_free_bytes=98 * bp.GB)
    bp.check_guard(plan, max_bytes=5 * bp.GB, min_free_bytes=20 * bp.GB)


def test_cli_dry_run_downloads_nothing(s3, tmp_path, capsys):
    rc = bp.main(["download", "--start", "2020-03-10", "--end", "2020-03-13",
                  "--out", str(tmp_path), "--min-free-gb", "0", "--dry-run",
                  "--base-url", s3.url])
    assert rc == 0
    assert capsys.readouterr().out.count("data/trade/") == 4
    assert not list((tmp_path / "trade").glob("*.gz"))


def test_cli_refuses_over_cap(s3, tmp_path):
    rc = bp.main(["download", "--out", str(tmp_path), "--max-gb", "0.000001",
                  "--min-free-gb", "0", "--base-url", s3.url])
    assert rc == 2
