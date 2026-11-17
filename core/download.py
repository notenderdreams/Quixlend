"""Fetch the files of a /v1/downloads response into an asset folder. No bpy."""
import itertools
import os
import queue
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPException
from pathlib import Path

from . import http

SECTIONS = ("json", "brushes", "billboards", "components", "meshes")


class DownloadError(Exception):
    pass


class ShortRead(DownloadError):
    """A response ended early; worth retrying."""


RETRY_DELAYS = (0.5, 1.0, 2.0)  # seconds before each retry (jittered); one entry per retry allowed


def retryable(e):
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500
    return isinstance(e, (ShortRead, urllib.error.URLError, HTTPException, OSError))


def reason(e):
    """Short user-facing explanation of why a download failed."""
    if isinstance(e, urllib.error.HTTPError):
        return "Link expired or denied" if e.code in (401, 403) else f"Server error {e.code}"
    if isinstance(e, (urllib.error.URLError, HTTPException, OSError)):
        return "Network error"
    return str(e) or type(e).__name__


def asset_dir(library, asset) -> Path:
    """<library>/Quixlend/<type>/<id> — kept apart from Bridge's own Downloaded/ tree."""
    return Path(library) / "Quixlend" / asset["categories"][0].lower() / asset["id"]


def plan(res, dest) -> list:
    """[(uri, path, size)] for a download response (port of Bridge's getFilesToDownload)."""
    dest, asset_id, files = Path(dest), res["asset"], []

    def rel(uri):
        parts = [p for p in urllib.parse.urlparse(uri).path.split("/") if p]
        return parts if len(parts) <= 1 else parts[1:]  # drop the leading <id>/ segment

    previews = res.get("previews") or []
    if previews:
        ext = os.path.splitext(urllib.parse.urlparse(previews[0]["uri"]).path)[1]
        files.append((previews[0]["uri"], dest / f"{asset_id}_Preview{ext}", previews[0].get("contentLength")))
    for section in SECTIONS:
        items = res.get(section) or []
        for item in [items] if isinstance(items, dict) else items:
            path = dest.joinpath(*rel(item["uri"]))
            if dest not in path.resolve().parents and dest.resolve() not in path.resolve().parents:
                raise DownloadError(f"Refusing path outside asset folder: {item['uri'].split('?')[0]}")
            files.append((item["uri"], path, item.get("contentLength")))
    return files


CHUNK = 8 << 20   # bytes per ranged request
READ = 1 << 20    # streaming read size


def _get(uri, start=None, end=None):
    """Open `uri`, optionally for the byte range start..end (inclusive)."""
    headers = {"User-Agent": http.UA}
    if start is not None:
        headers["Range"] = f"bytes={start}-{end}"
    return urllib.request.urlopen(urllib.request.Request(uri, headers=headers), timeout=60)


def _copy(res, f, on_bytes):
    n = 0
    while chunk := res.read(READ):
        f.write(chunk)
        n += len(chunk)
        on_bytes(len(chunk))
    return n


class _File:
    """One file being downloaded: its partial file, and how many chunks are still outstanding."""

    def __init__(self, uri, path, size, links):
        self.links, self.path, self.size = links, path, size
        self.links[path] = uri
        self.part = path.with_name(path.name + ".part")
        self.left = 1
        self.lock = threading.Lock()

    @property
    def uri(self):
        return self.links[self.path]  # swapped in place when the links are refreshed

    def finish(self):
        """Called when the last chunk lands: verify the size and move the file into place."""
        got = self.part.stat().st_size
        if self.size and got != self.size:
            self.part.unlink()
            raise DownloadError(f"{self.path.name}: got {got} bytes, expected {self.size}")
        self.part.replace(self.path)

    def chunk_done(self):
        with self.lock:
            self.left -= 1
            last = self.left == 0
        if last:
            self.finish()


MAX_REFRESHES = 2  # fresh sets of links requested per download before giving up


def fetch_all(files, progress=None, workers=8, refresh=None):
    """Download planned files. Big files are split into parallel byte-range chunks, biggest first;
    a server that ignores ranges is streamed whole. progress(done_bytes, total_bytes) is called from
    worker threads. `refresh()`, if given, returns a new plan for the same files with fresh links; it is
    called when a link has expired (HTTP 403) and the interrupted work is retried with the new links."""
    links, renewed = {}, [0, threading.Lock()]  # path -> current uri; [refresh count, lock]
    total, done, lock = sum(size or 0 for _, _, size in files), [0], threading.Lock()
    todo, failure, seq = queue.PriorityQueue(), [], itertools.count()

    def on_bytes(n):
        with lock:
            done[0] += n
            if progress:
                progress(done[0], total)

    def renew(seen):
        """New links after a 403; False once the refresh budget is spent. `seen` = refresh count the caller ran with."""
        with renewed[1]:
            if renewed[0] != seen:  # another worker already refreshed
                return True
            if not refresh or renewed[0] >= MAX_REFRESHES:
                return False
            for uri, path, _ in refresh():
                if path in links:
                    links[path] = uri
            renewed[0] += 1
            return True

    def attempt(job):
        """Run job(on_bytes), retrying transient failures and renewing expired links; bytes of a failed try are taken back."""
        tries = 0
        while True:
            seen, counted = renewed[0], [0]

            def count(n):
                counted[0] += n
                on_bytes(n)
            try:
                return job(count)
            except BaseException as e:
                if isinstance(e, urllib.error.HTTPError):
                    e.close()
                on_bytes(-counted[0])
                if isinstance(e, urllib.error.HTTPError) and e.code == 403 and renew(seen):
                    continue
                if tries >= len(RETRY_DELAYS) or not retryable(e):
                    raise
                time.sleep(RETRY_DELAYS[tries] * random.uniform(0.5, 1.0))
                tries += 1

    def put(job, priority):
        todo.put((-priority, next(seq), job))

    def stream(f, on_bytes):
        with _get(f.uri) as res, open(f.part, "wb") as out:
            got = _copy(res, out, on_bytes)
        if f.size and got < f.size:
            raise ShortRead(f"{f.path.name}: got {got} bytes, expected {f.size}")
        f.finish()

    def chunk(f, start, end, on_bytes):
        with _get(f.uri, start, end) as res:
            if res.status != 206:
                raise DownloadError(f"{f.path.name}: server ignored the byte range")
            with open(f.part, "r+b") as out:
                out.seek(start)
                got = _copy(res, out, on_bytes)
        if got != end - start + 1:
            raise ShortRead(f"{f.path.name}: got {got} bytes of range {start}-{end}")
        f.chunk_done()

    def first(f, on_bytes):
        """First chunk doubles as the range probe: a 206 means the rest can be fetched in parallel."""
        end = min(CHUNK, f.size) - 1
        with _get(f.uri, 0, end) as res:
            if res.status != 206:  # no range support: this response is the whole file
                with open(f.part, "wb") as out:
                    got = _copy(res, out, on_bytes)
                if got < f.size:
                    raise ShortRead(f"{f.path.name}: got {got} bytes, expected {f.size}")
                f.finish()
                return
            with open(f.part, "wb") as out:
                out.truncate(f.size)
                got = _copy(res, out, on_bytes)
        if got != end + 1:
            raise ShortRead(f"{f.path.name}: got {got} bytes of range 0-{end}")
        starts = range(CHUNK, f.size, CHUNK)
        f.left = 1 + len(starts)
        for start in starts:
            put(lambda start=start: attempt(lambda on: chunk(f, start, min(start + CHUNK, f.size) - 1, on)), f.size)
        f.chunk_done()

    def worker():
        while True:
            item = todo.get()
            try:
                if item[2] is None:
                    return
                if not failure:
                    item[2]()
            except BaseException as e:
                failure.append(e)
            finally:
                todo.task_done()

    for uri, path, size in sorted(files, key=lambda f: -(f[2] or 0)):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and size and path.stat().st_size == size:
            on_bytes(size)
            continue
        f = _File(uri, path, size, links)
        put((lambda f=f: attempt(lambda on: first(f, on))) if size and size > CHUNK
            else (lambda f=f: attempt(lambda on: stream(f, on))), size or 0)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    todo.join()
    for _ in threads:
        todo.put((1, 0, None))  # sentinels sort after every real job
    for t in threads:
        t.join()
    if failure:
        raise failure[0]
    return [path for _, path, _ in files]
