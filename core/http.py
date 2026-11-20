"""Tiny JSON-over-HTTPS helper on urllib. No bpy."""

import json
import time
import tomllib
import urllib.error
import urllib.request
from http.client import HTTPException
from pathlib import Path
from typing import Any

MANIFEST = Path(__file__).parents[1] / "blender_manifest.toml"


def _version() -> str:
    """The extension's version, from its manifest, so a release changes it in one place."""
    try:
        return str(tomllib.loads(MANIFEST.read_text())["version"])
    except (OSError, ValueError, KeyError):
        return "unknown"  # a request must never fail because of its User-Agent


UA = f"quixlend/{_version()}"


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


def transient(e: BaseException) -> bool:
    """Worth another try: dropped or stalled connections, 429 and 5xx. Not 4xx: the same request fails the same way."""
    if isinstance(e, (HttpError, urllib.error.HTTPError)):  # urllib's is an OSError too, so check it first
        status = e.status if isinstance(e, HttpError) else e.code
        return status == 429 or status >= 500
    return isinstance(e, (OSError, HTTPException))  # URLError, timeouts and resets are all OSErrors


def retry(fn, delays=(0.5, 1.5), on_retry=None, give_up=None) -> Any:
    """fn(), retried after each delay on a transient failure. on_retry(n) is called before retry n;
    give_up() returning True (the request was superseded) ends the retrying."""
    for attempt in range(len(delays) + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == len(delays) or not transient(e) or (give_up and give_up()):
                raise
            if on_retry:
                on_retry(attempt + 1)
            time.sleep(delays[attempt])
