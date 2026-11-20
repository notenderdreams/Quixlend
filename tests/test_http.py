"""core.http.retry: transient failures are retried, real ones are not."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
from core import http


def flaky(*errors, then="ok"):
    calls = []

    def fn():
        calls.append(1)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return then

    return fn, calls


class Retry(unittest.TestCase):
    def test_connection_reset_is_retried_and_reported(self):
        fn, calls = flaky(ConnectionResetError(54, "reset"))
        seen = []
        self.assertEqual(http.retry(fn, delays=(0, 0), on_retry=seen.append), "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual(seen, [1])  # attempt number about to be made, for "Retrying (1/2)…"

    def test_gives_up_after_the_delays_run_out(self):
        fn, calls = flaky(*[TimeoutError()] * 5)
        with self.assertRaises(TimeoutError):
            http.retry(fn, delays=(0, 0))
        self.assertEqual(len(calls), 3)

    def test_server_errors_and_rate_limits_are_retried(self):
        for status in (429, 503):
            fn, _ = flaky(http.HttpError(status, "u", ""))
            self.assertEqual(http.retry(fn, delays=(0,)), "ok")

    def test_client_errors_are_not_retried(self):
        fn, calls = flaky(http.HttpError(403, "u", ""))
        with self.assertRaises(http.HttpError):
            http.retry(fn, delays=(0, 0))
        self.assertEqual(len(calls), 1)

    def test_urllib_http_errors_follow_their_status(self):
        import urllib.error

        fn, calls = flaky(urllib.error.HTTPError("u", 404, "nf", None, None))  # type: ignore[arg-type]
        with self.assertRaises(urllib.error.HTTPError):
            http.retry(fn, delays=(0,))
        self.assertEqual(len(calls), 1)

    def test_a_superseded_request_stops_retrying(self):
        fn, calls = flaky(TimeoutError(), TimeoutError())
        with self.assertRaises(TimeoutError):
            http.retry(fn, delays=(0, 0), give_up=lambda: True)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()


class UserAgent(unittest.TestCase):
    def test_version_comes_from_the_manifest(self):
        import tomllib

        manifest = tomllib.loads(http.MANIFEST.read_text())
        self.assertEqual(http.UA, f"quixlend/{manifest['version']}")

    def test_unreadable_manifest_does_not_break_requests(self):
        with mock.patch.object(http, "MANIFEST", Path("/nonexistent/blender_manifest.toml")):
            self.assertEqual(http._version(), "unknown")
