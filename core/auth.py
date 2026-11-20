"""Quixel account session: Epic browser login, token store, refresh. No bpy."""

import base64
import json
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

from . import http

EPIC_CLIENT_ID = "6befbbcfe1f8406698661b68bb01091a"
SSO = "https://accounts.quixel.se/api/v1/"
REALM = "megascans-bridge"

# Epic shows the authorization code as JSON on this page once logged in.
_REDIRECT = "https://www.epicgames.com/id/api/redirect?" + urllib.parse.urlencode(
    {"clientId": EPIC_CLIENT_ID, "responseType": "code"}
)
LOGIN_URL = "https://www.epicgames.com/id/login?" + urllib.parse.urlencode({"redirectUrl": _REDIRECT})

_CODE_RE = re.compile(r'(?:authorizationCode"\s*:\s*"|[?&]code=)([0-9a-fA-F]{32})|^\s*([0-9a-fA-F]{32})\s*$')


class AuthError(Exception):
    pass


def parse_code(pasted: str) -> str:
    """Accepts the bare code, the JSON Epic shows, or a redirect URL containing ?code=."""
    m = _CODE_RE.search(pasted.strip())
    if not m:
        raise AuthError("No Epic authorization code found in pasted text")
    return m.group(1) or m.group(2)


def claims(token: str) -> dict:
    """The token's payload; empty for anything that is not a JWT, so a damaged token reads as expired, not as a crash."""
    try:
        body = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (IndexError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class Session:
    def __init__(self, path):
        self.path = Path(path)
        self.token = self.refresh_token = ""
        self._lock = threading.Lock()  # searches and downloads refresh from different threads
        try:
            data = json.loads(self.path.read_text())
            self.token, self.refresh_token = str(data.get("token", "")), str(data.get("refreshToken", ""))
        except (OSError, ValueError, AttributeError):
            pass  # no session file, or one damaged by a crash: signed out, never a reason to fail to start

    @property
    def logged_in(self) -> bool:
        return bool(self.token)

    @property
    def email(self) -> str:
        return claims(self.token).get("email", "") if self.token else ""

    def _store(self, res: dict):
        if not res.get("token"):
            raise AuthError(f"Login response had no token: {res}")
        self.token, self.refresh_token = res["token"], res.get("refreshToken", "")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(
            self.path.name + ".tmp"
        )  # replaced whole, so a crash never leaves half a file
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"token": self.token, "refreshToken": self.refresh_token}, f)
        tmp.replace(self.path)

    def login_with_code(self, pasted: str):
        self._store(http.post(SSO + "login/epic", {"code": parse_code(pasted), "realm": REALM}))

    def refresh(self):
        with self._lock:
            self._refresh()

    def _refresh(self):
        if not self.token:
            raise AuthError("Not logged in")
        self._store(
            http.post(
                SSO + "login",
                {
                    "email": self.email,
                    "token": self.token,
                    "refreshToken": self.refresh_token,
                    "realm": REALM,
                },
            )
        )

    def bearer(self) -> str:
        """Valid access token, refreshed if within 60s of expiry."""
        if not self.token:
            raise AuthError("Not logged in")
        if self._expiring():
            with self._lock:
                if self._expiring():  # another thread may have refreshed while this one waited
                    self._refresh()
        return self.token

    def _expiring(self) -> bool:
        return claims(self.token).get("exp", 0) < time.time() + 60

    def logout(self):
        self.token = self.refresh_token = ""
        self.path.unlink(missing_ok=True)
