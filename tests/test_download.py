"""Characterization tests for core.download.fetch_all against a local HTTP server."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from core import download  # noqa: E402
from tests.server import TestServer  # noqa: E402

FILES = {"a/big.bin": os.urandom(300_000), "a/small.bin": os.urandom(1_000)}


def plan(server, dest, size=True):
    return [(server.url(p), Path(dest) / p, len(d) if size else None) for p, d in FILES.items()]


class FetchAll(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_files_are_byte_identical(self):
        with TestServer(FILES) as server:
            download.fetch_all(plan(server, self.dest))
        for p, data in FILES.items():
            self.assertEqual((self.dest / p).read_bytes(), data)

    def test_progress_ends_at_total_and_never_goes_backwards(self):
        seen = []
        with TestServer(FILES) as server:
            download.fetch_all(plan(server, self.dest), lambda done, total: seen.append((done, total)))
        self.assertEqual(seen[-1], (sum(map(len, FILES.values())),) * 2)
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))

    def test_size_mismatch_fails_and_leaves_no_finished_file(self):
        with TestServer(FILES) as server:
            bad = [(url, path, size + 1) for url, path, size in plan(server, self.dest)]
            with self.assertRaises(download.DownloadError):
                download.fetch_all(bad)
        self.assertFalse(any(p.exists() for _, p, _ in bad))

    def test_complete_files_are_skipped(self):
        with TestServer(FILES) as server:
            download.fetch_all(plan(server, self.dest))
            before = len(server.requests())
            download.fetch_all(plan(server, self.dest))
            self.assertEqual(len(server.requests()), before)

    def test_http_error_is_raised(self):
        with TestServer(FILES) as server:
            server.fail = {1: 500, 2: 500}
            with self.assertRaises(Exception):
                download.fetch_all(plan(server, self.dest), workers=1)


class Chunked(unittest.TestCase):
    """Big files are fetched as parallel byte-range chunks (CHUNK shrunk so the test files count as big)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name)
        self.chunk, download.CHUNK = download.CHUNK, 64_000

    def tearDown(self):
        download.CHUNK = self.chunk
        self.tmp.cleanup()

    def test_ranged_download_is_byte_identical_and_spread_over_connections(self):
        with TestServer(FILES) as server:
            server.delay = 0.05
            download.fetch_all(plan(server, self.dest))
            big = server.requests("a/big.bin")
        for p, data in FILES.items():
            self.assertEqual((self.dest / p).read_bytes(), data)
        self.assertEqual(len(big), 5)  # 300_000 bytes in 64_000-byte chunks
        self.assertTrue(all(r["range"] for r in big))
        self.assertGreater(len({r["conn"] for r in big}), 1)

    def test_biggest_file_is_requested_first(self):
        with TestServer(FILES) as server:
            download.fetch_all(plan(server, self.dest), workers=1)
            self.assertEqual(server.requests()[0]["path"], "a/big.bin")

    def test_small_files_are_not_split(self):
        with TestServer(FILES) as server:
            download.fetch_all(plan(server, self.dest))
            small = server.requests("a/small.bin")
        self.assertEqual([r["range"] for r in small], [None])

    def test_server_without_ranges_still_produces_correct_files(self):
        with TestServer(FILES, ranges=False) as server:
            download.fetch_all(plan(server, self.dest))
            self.assertEqual(len(server.requests("a/big.bin")), 1)
        for p, data in FILES.items():
            self.assertEqual((self.dest / p).read_bytes(), data)

    def test_progress_is_monotonic_and_ends_at_total(self):
        seen = []
        with TestServer(FILES) as server:
            download.fetch_all(plan(server, self.dest), lambda done, total: seen.append(done))
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], sum(map(len, FILES.values())))

    def test_wrong_size_fails_without_a_finished_file(self):
        with TestServer(FILES) as server:
            bad = [(url, path, size + 1) for url, path, size in plan(server, self.dest)]
            with self.assertRaises(download.DownloadError):
                download.fetch_all(bad)
        self.assertFalse((self.dest / "a/big.bin").exists())


if __name__ == "__main__":
    unittest.main()
