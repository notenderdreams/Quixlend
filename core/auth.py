"""Quixel account session: Epic browser login, token store, refresh. No bpy."""
import base64
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

from . import http

EPIC_CLIENT_ID = "6befbbcfe1f8406698661b68bb01091a"
SSO = "https://accounts.quixel.se/api/v1/"
REALM = "megascans-bridge"

# Epic shows the authorization code as JSON on this page once logged in.
_REDIRECT = (
    "https://www.epicgames.com/id/api/redirect?"
    + urllib.parse.urlencode({"clientId": EPIC_CLIENT_ID, "responseType": "code"})
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
    body = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


class Session:
    def __init__(self, path):
        self.path = Path(path)
        self.token = self.refresh_token = ""
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.token, self.refresh_token = data.get("token", ""), data.get("refreshToken", "")

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
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"token": self.token, "refreshToken": self.refresh_token}, f)

    def login_with_code(self, pasted: str):
        self._store(http.post(SSO + "login/epic", {"code": parse_code(pasted), "realm": REALM}))

    def refresh(self):
        if not self.token:
            raise AuthError("Not logged in")
        self._store(http.post(SSO + "login", {
            "email": self.email, "token": self.token,
            "refreshToken": self.refresh_token, "realm": REALM,
        }))

    def bearer(self) -> str:
        """Valid access token, refreshed if within 60s of expiry."""
        if not self.token:
            raise AuthError("Not logged in")
        if claims(self.token).get("exp", 0) < time.time() + 60:
            self.refresh()
        return self.token

    def logout(self):
        self.token = self.refresh_token = ""
        self.path.unlink(missing_ok=True)
