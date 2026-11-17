"""Characterization tests for core.download.fetch_all against a local HTTP server."""
import os
import sys
import tempfile
import urllib.error
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


class Retries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name)
        self.chunk, download.CHUNK = download.CHUNK, 64_000
        self.delays, download.RETRY_DELAYS = download.RETRY_DELAYS, (0, 0, 0)

    def tearDown(self):
        download.CHUNK, download.RETRY_DELAYS = self.chunk, self.delays
        self.tmp.cleanup()

    def run_fetch(self, server, **kw):
        seen = []
        download.fetch_all(plan(server, self.dest), lambda done, total: seen.append(done), **kw)
        return seen

    def test_transient_server_error_on_one_chunk_is_retried_alone(self):
        with TestServer(FILES) as server:
            server.fail = {3: 503}
            seen = self.run_fetch(server)
            ranges = [r["range"] for r in server.requests("a/big.bin")]
        for p, data in FILES.items():
            self.assertEqual((self.dest / p).read_bytes(), data)
        self.assertEqual(len(ranges), 6)       # 5 chunks + the one retried
        self.assertEqual(len(set(ranges)), 5)  # no chunk was fetched twice successfully
        self.assertEqual(seen[-1], sum(map(len, FILES.values())))

    def test_truncated_response_is_retried_and_progress_is_not_double_counted(self):
        with TestServer(FILES) as server:
            server.truncate = {2}
            seen = self.run_fetch(server)
        for p, data in FILES.items():
            self.assertEqual((self.dest / p).read_bytes(), data)
        self.assertEqual(max(seen), sum(map(len, FILES.values())))

    def test_gives_up_after_the_retry_budget_with_the_server_error(self):
        with TestServer(FILES) as server:
            server.fail = {n: 500 for n in range(1, 20)}
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                download.fetch_all(plan(server, self.dest), workers=1)
            attempts = len(server.requests())
        self.assertEqual(ctx.exception.code, 500)
        self.assertEqual(attempts, 4)  # first try + 3 retries
        self.assertEqual(download.reason(ctx.exception), "Server error 500")

    def test_client_errors_are_not_retried(self):
        with TestServer(FILES) as server:
            server.fail = {1: 404}
            with self.assertRaises(urllib.error.HTTPError):
                download.fetch_all(plan(server, self.dest), workers=1)
            self.assertEqual(len(server.requests()), 1)

    def test_denied_link_has_a_clear_reason(self):
        with TestServer(FILES) as server:
            server.fail = {1: 403}
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                download.fetch_all(plan(server, self.dest), workers=1)
        self.assertEqual(download.reason(ctx.exception), "Link expired or denied")

    def test_unreachable_server_is_a_network_error(self):
        with TestServer(FILES) as server:
            files = plan(server, self.dest)
        with self.assertRaises(Exception) as ctx:
            download.fetch_all(files)
        self.assertEqual(download.reason(ctx.exception), "Network error")


class LinkRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name)
        self.chunk, download.CHUNK = download.CHUNK, 64_000
        self.delays, download.RETRY_DELAYS = download.RETRY_DELAYS, (0, 0, 0)

    def tearDown(self):
        download.CHUNK, download.RETRY_DELAYS = self.chunk, self.delays
        self.tmp.cleanup()

    def links(self, server, token):
        return [(f"{url}?t={token}", path, size) for url, path, size in plan(server, self.dest)]

    def test_expired_links_are_refreshed_and_the_download_completes(self):
        calls = []
        with TestServer(FILES) as server:
            server.token = "new"

            def refresh():
                calls.append(1)
                return self.links(server, "new")
            seen = []
            download.fetch_all(self.links(server, "old"), lambda done, total: seen.append(done), refresh=refresh)
        for p, data in FILES.items():
            self.assertEqual((self.dest / p).read_bytes(), data)
        self.assertEqual(len(calls), 1)  # concurrent 403s share one refresh
        self.assertEqual(seen[-1], sum(map(len, FILES.values())))

    def test_links_that_stay_denied_fail_after_the_bounded_number_of_refreshes(self):
        calls = []
        with TestServer(FILES) as server:
            server.token = "never"

            def refresh():
                calls.append(1)
                return self.links(server, "still-old")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                download.fetch_all(self.links(server, "old"), refresh=refresh)
            forbidden = [r for r in server.requests() if r["status"] == 403]
        self.assertEqual(len(calls), download.MAX_REFRESHES)
        self.assertEqual(download.reason(ctx.exception), "Link expired or denied")
        self.assertTrue(forbidden)

    def test_403_is_not_retried_before_the_refresh(self):
        with TestServer(FILES) as server:
            server.token = "new"
            download.fetch_all(self.links(server, "old"), workers=1, refresh=lambda: self.links(server, "new"))
            first = server.requests()[0]
            self.assertEqual(first["status"], 403)
            self.assertNotEqual(server.requests()[1]["status"], 403)

    def test_without_a_refresh_callback_a_403_fails_immediately(self):
        with TestServer(FILES) as server:
            server.token = "new"
            with self.assertRaises(urllib.error.HTTPError):
                download.fetch_all(self.links(server, "old"), workers=1)
            self.assertEqual(len(server.requests()), 1)


class Resume(unittest.TestCase):
    """An interrupted download continues from the chunks already on disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name)
        self.chunk, download.CHUNK = download.CHUNK, 64_000
        self.delays, download.RETRY_DELAYS = download.RETRY_DELAYS, ()
        self.big = self.dest / "a/big.bin"
        self.sidecar = self.dest / "a/big.bin.part.json"

    def tearDown(self):
        download.CHUNK, download.RETRY_DELAYS = self.chunk, self.delays
        self.tmp.cleanup()

    def interrupt(self, server, after=3):
        """Download with the server dying for good after `after` requests; one worker keeps the order fixed."""
        server.fail = {n: 500 for n in range(after + 1, 100)}
        with self.assertRaises(Exception):
            download.fetch_all(plan(server, self.dest), workers=1)
        server.fail, server.log[:] = {}, []

    def finish(self, server):
        seen = []
        download.fetch_all(plan(server, self.dest), lambda done, total: seen.append(done), workers=1)
        return seen

    def test_second_run_fetches_only_the_missing_chunks(self):
        with TestServer(FILES) as server:
            self.interrupt(server)
            self.assertTrue(self.sidecar.exists())
            seen = self.finish(server)
            ranges = [r["range"] for r in server.requests("a/big.bin")]
        self.assertEqual(ranges, ["bytes=192000-255999", "bytes=256000-299999"])  # chunks 3 and 4
        self.assertEqual(self.big.read_bytes(), FILES["a/big.bin"])
        self.assertEqual(seen[-1], sum(map(len, FILES.values())))
        self.assertEqual(seen, sorted(seen))
        self.assertFalse(self.sidecar.exists())

    def test_a_corrupt_record_restarts_cleanly(self):
        with TestServer(FILES) as server:
            self.interrupt(server)
            self.sidecar.write_text("not json")
            self.finish(server)
            self.assertEqual(len(server.requests("a/big.bin")), 5)
        self.assertEqual(self.big.read_bytes(), FILES["a/big.bin"])

    def test_a_record_without_its_partial_file_restarts_cleanly(self):
        with TestServer(FILES) as server:
            self.interrupt(server)
            (self.dest / "a/big.bin.part").unlink()
            self.finish(server)
            self.assertEqual(len(server.requests("a/big.bin")), 5)
        self.assertEqual(self.big.read_bytes(), FILES["a/big.bin"])

    def test_a_record_for_a_different_chunk_size_restarts_cleanly(self):
        with TestServer(FILES) as server:
            self.interrupt(server)
            download.CHUNK = 100_000
            self.finish(server)
            self.assertEqual(len(server.requests("a/big.bin")), 3)
        self.assertEqual(self.big.read_bytes(), FILES["a/big.bin"])

    def test_wrong_final_size_after_resume_leaves_no_finished_file(self):
        with TestServer(FILES) as server:
            self.interrupt(server)
            bad = [(url, path, size + 1) for url, path, size in plan(server, self.dest)]
            with self.assertRaises(download.DownloadError):
                download.fetch_all(bad, workers=1)
        self.assertFalse(self.big.exists())

    def test_complete_files_cost_no_requests_on_a_later_run(self):
        with TestServer(FILES) as server:
            download.fetch_all(plan(server, self.dest)[:1], workers=1)  # only the big file
            server.log[:] = []
            download.fetch_all(plan(server, self.dest), workers=1)
            self.assertEqual([r["path"] for r in server.requests()], ["a/small.bin"])


if __name__ == "__main__":
    unittest.main()
