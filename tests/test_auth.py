"""core.auth.Session: a damaged session file must not stop the add-on, and a saved one is written whole."""

import base64
import json
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
from core import auth


def jwt(**claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


class Session(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "q" / "auth.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text)

    def test_missing_file_is_signed_out(self):
        self.assertFalse(auth.Session(self.path).logged_in)

    def test_damaged_file_is_signed_out_not_a_crash(self):
        for text in ("", "{", "null", "[]", '{"token": '):
            self.write(text)
            self.assertFalse(auth.Session(self.path).logged_in, text)

    def test_a_token_that_is_not_a_jwt_reads_as_expired(self):
        self.write(json.dumps({"token": "garbage", "refreshToken": "r"}))
        session = auth.Session(self.path)
        self.assertTrue(session.logged_in)
        self.assertEqual(session.email, "")  # the settings panel asks for this on every draw
        self.assertEqual(auth.claims("garbage"), {})

    def test_stored_session_is_private_and_reloads(self):
        session = auth.Session(self.path)
        session._store({"token": jwt(email="a@b.c"), "refreshToken": "r"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])  # no half-written file left behind
        again = auth.Session(self.path)
        self.assertEqual((again.email, again.refresh_token), ("a@b.c", "r"))

    def test_concurrent_callers_refresh_once(self):
        session = auth.Session(self.path)
        session.token = jwt(exp=time.time() - 10)
        calls = []

        def post(url, body):
            calls.append(url)
            time.sleep(0.05)
            return {"token": jwt(exp=time.time() + 3600), "refreshToken": "r2"}

        with mock.patch.object(auth.http, "post", post):
            import threading

            threads = [threading.Thread(target=session.bearer) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(calls), 1)
