"""Tiny JSON-over-HTTPS helper on urllib. No bpy."""

import json
import urllib.error
import urllib.request
from typing import Any

UA = "quixlend/0.1"


class HttpError(Exception):
    def __init__(self, status, url, body):
        super().__init__(f"HTTP {status} {url}: {body[:500]}")
        self.status, self.url, self.body = status, url, body


def request(url, body=None, headers=None, method=None, timeout=30) -> Any:
    hdrs = {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, url, e.read().decode("utf-8", "replace")) from None
    return json.loads(raw) if raw else None


def get(url, headers=None) -> Any:
    return request(url, headers=headers)


def post(url, body, headers=None) -> Any:
    return request(url, body=body, headers=headers, method="POST")
