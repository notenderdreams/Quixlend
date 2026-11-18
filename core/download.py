"""Fetch the files of a /v1/downloads response into an asset folder. No bpy."""

import collections
import contextlib
import itertools
import json
import os
import queue
import random
import re
import shutil
import socket
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


class DiskFull(DownloadError):
    pass


class Cancelled(Exception):
    """The caller asked to stop; not a failure."""


class ShortRead(DownloadError):
    """A response ended early; worth retrying."""


RETRY_DELAYS = (0.5, 1.0, 2.0)  # seconds before each retry (jittered); one entry per retry allowed


def retryable(e):
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500
    return isinstance(e, (ShortRead, urllib.error.URLError, HTTPException, OSError))


def reason(e):
    """Short user-facing explanation of why a download failed."""
    if isinstance(e, DiskFull):
        return "Not enough disk space"
    if isinstance(e, Cancelled):
        return "Cancelled"
    if isinstance(e, urllib.error.HTTPError):
        return "Link expired or denied" if e.code in (401, 403) else f"Server error {e.code}"
    if isinstance(e, (urllib.error.URLError, HTTPException, OSError)):
        return "Network error"
    return str(e) or type(e).__name__


_RES = re.compile(r"_(\d+)K_", re.I)


def resolution_of(paths) -> int | None:
    """Texture size in pixels (the largest `_<n>K_` file-name token), or None if there are no textures."""
    sizes = [int(m.group(1)) * 1024 for p in paths if (m := _RES.search(Path(p).name))]
    return max(sizes, default=None)


def resolutions_of(paths) -> set[int]:
    """Texture sizes in pixels found in file names; previews and billboards are not the asset's own maps."""
    return {
        int(m.group(1)) * 1024
        for p in paths
        if (m := _RES.search(Path(p).name))
        and not {"preview", "billboards"} & {s.lower() for s in Path(p).parts}
        and "preview" not in Path(p).name.lower()
    }


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
        files.append(
            (previews[0]["uri"], dest / f"{asset_id}_Preview{ext}", previews[0].get("contentLength"))
        )
    for section in SECTIONS:
        items = res.get(section) or []
        for item in [items] if isinstance(items, dict) else items:
            path = dest.joinpath(*rel(item["uri"]))
            if dest not in path.resolve().parents and dest.resolve() not in path.resolve().parents:
                raise DownloadError(f"Refusing path outside asset folder: {item['uri'].split('?')[0]}")
            files.append((item["uri"], path, item.get("contentLength")))
    return files


CHUNK = 8 << 20  # bytes per ranged request
READ = 1 << 20  # streaming read size


def _get(uri, start=None, end=None):
    """Open `uri`, optionally for the byte range start..end (inclusive)."""
    headers = {"User-Agent": http.UA}
    if start is not None:
        headers["Range"] = f"bytes={start}-{end}"
    return urllib.request.urlopen(urllib.request.Request(uri, headers=headers), timeout=60)


def _abort(res):
    """Wake a thread blocked reading `res`: shut its socket down so the read returns at once."""
    with contextlib.suppress(Exception):  # internals of http.client; if they move, cancel is just slower
        res.fp.raw._sock.shutdown(socket.SHUT_RDWR)


def _copy(res, f, on_bytes):
    n = 0
    while chunk := res.read1(READ):
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
        self.sidecar = self.part.with_name(self.part.name + ".json")  # which chunks are complete
        self.left, self.done = 1, set()
        self.lock = threading.Lock()

    def chunks(self):
        return -(-self.size // CHUNK)

    def span(self, i: int) -> tuple[int, int]:
        """(start, end) inclusive byte range of chunk i."""
        return i * CHUNK, min((i + 1) * CHUNK, self.size) - 1

    def save(self):
        tmp = self.sidecar.with_name(self.sidecar.name + ".tmp")
        tmp.write_text(json.dumps({"size": self.size, "chunk": CHUNK, "done": sorted(self.done)}))
        tmp.replace(self.sidecar)

    def mark(self, i):
        """Record chunk i as completely written, so an interrupted download can pick up after it."""
        with self.lock:
            self.done.add(i)
            self.save()

    def resume(self):
        """Chunks already on disk from an earlier attempt, or None (and a clean slate) if that state is unusable."""
        try:
            meta = json.loads(self.sidecar.read_text())
            done = set(meta["done"])
            if (
                meta["size"] == self.size
                and meta["chunk"] == CHUNK
                and self.part.stat().st_size == self.size
                and all(isinstance(i, int) and 0 <= i < self.chunks() for i in done)
            ):
                self.done = done
                return done
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.part.unlink(missing_ok=True)
        self.sidecar.unlink(missing_ok=True)
        return None

    @property
    def uri(self):
        return self.links[self.path]  # swapped in place when the links are refreshed

    def finish(self):
        """Called when the last chunk lands: verify the size and move the file into place."""
        got = self.part.stat().st_size
        if self.size and got != self.size:
            self.part.unlink()
            self.sidecar.unlink(missing_ok=True)
            raise DownloadError(f"{self.path.name}: got {got} bytes, expected {self.size}")
        self.part.replace(self.path)
        self.sidecar.unlink(missing_ok=True)

    def chunk_done(self):
        with self.lock:
            self.left -= 1
            last = self.left == 0
        if last:
            self.finish()


_now = time.monotonic  # patchable clock


class _Rate:
    """Transfer speed over the last few seconds, and the time left at that speed."""

    WINDOW = 3.0  # seconds of history that count
    SETTLE = 1.0  # no estimate sooner than this after the start

    def start(self, done):
        self.t0 = _now()
        self.samples = collections.deque([(self.t0, done)])

    def update(self, done, total):
        """-> (bytes per second, seconds left); each None until there is enough data."""
        now = _now()
        self.samples.append((now, done))
        while len(self.samples) > 1 and self.samples[0][0] < now - self.WINDOW:
            self.samples.popleft()
        then, before = self.samples[0]
        if now - then < 0.25 or now - self.t0 < self.SETTLE:
            return None, None
        bps = max(done - before, 0) / (now - then)
        return bps, ((total - done) / bps if bps > 0 else None)


MARGIN = 64 << 20  # bytes that must stay free on top of what the download needs


def _free(path):
    return shutil.disk_usage(path).free


MAX_REFRESHES = 2  # fresh sets of links requested per download before giving up


def fetch_all(files, progress=None, workers=8, refresh=None, cancel=None, speed=None, free_space=_free):
    """Download planned files. Big files are split into parallel byte-range chunks, biggest first;
    a server that ignores ranges is streamed whole. progress(done_bytes, total_bytes) is called from
    worker threads. `refresh()`, if given, returns a new plan for the same files with fresh links; it is
    called when a link has expired (HTTP 403) and the interrupted work is retried with the new links.
    `speed(bytes_per_second, seconds_left)`, if given, is called alongside progress with a smoothed
    estimate (both None until there is enough data). Setting the `cancel` threading.Event stops the workers promptly, deletes the partial files and raises
    Cancelled. Before any request, the bytes still to fetch (files and chunks already on disk don't count)
    must fit in `free_space(folder)` plus MARGIN, or DiskFull is raised."""
    cancel = cancel or threading.Event()
    links, renewed = {}, [0, threading.Lock()]  # path -> current uri; [refresh count, lock]
    total, done, lock = sum(size or 0 for _, _, size in files), [0], threading.Lock()
    todo, failure, seq = queue.PriorityQueue(), [], itertools.count()

    rate = _Rate()
    rate.start(0)
    live, live_lock, over = set(), threading.Lock(), threading.Event()  # open responses; fetch_all finished

    @contextlib.contextmanager
    def opened(uri, start=None, end=None):
        """_get, remembered so a cancel can cut the connection instead of waiting for the next byte."""
        res = _get(uri, start, end)
        with live_lock:
            live.add(res)
        if cancel.is_set():
            _abort(res)
        try:
            with res:
                yield res
        finally:
            with live_lock:
                live.discard(res)

    def watch():
        while not over.wait(0.1):
            if cancel.is_set():
                with live_lock:
                    for res in list(live):
                        _abort(res)
                return

    def on_bytes(n):
        with lock:
            done[0] += n
            if progress:
                progress(done[0], total)
            if speed:
                speed(*rate.update(done[0], total))

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
                if cancel.is_set():
                    raise Cancelled
                counted[0] += n  # noqa: B023  (called within the same iteration)
                on_bytes(n)

            try:
                return job(count)
            except BaseException as e:
                if isinstance(e, urllib.error.HTTPError):
                    e.close()
                on_bytes(-counted[0])
                if cancel.is_set() and not isinstance(e, Cancelled):
                    raise Cancelled from e  # a cut connection looks like a failure; it is the cancel
                if isinstance(e, urllib.error.HTTPError) and e.code == 403 and renew(seen):
                    continue
                if tries >= len(RETRY_DELAYS) or not retryable(e):
                    raise
                time.sleep(RETRY_DELAYS[tries] * random.uniform(0.5, 1.0))
                tries += 1

    def put(job, priority):
        todo.put((-priority, next(seq), job))

    def stream(f, on_bytes):
        with opened(f.uri) as res, open(f.part, "wb") as out:
            got = _copy(res, out, on_bytes)
        if f.size and got < f.size:
            raise ShortRead(f"{f.path.name}: got {got} bytes, expected {f.size}")
        f.finish()

    def chunk(f, start, end, on_bytes):
        with opened(f.uri, start, end) as res:
            if res.status != 206:
                raise DownloadError(f"{f.path.name}: server ignored the byte range")
            with open(f.part, "r+b") as out:
                out.seek(start)
                got = _copy(res, out, on_bytes)
        if got != end - start + 1:
            raise ShortRead(f"{f.path.name}: got {got} bytes of range {start}-{end}")
        f.mark(start // CHUNK)
        f.chunk_done()

    def first(f, on_bytes):
        """First chunk doubles as the range probe: a 206 means the rest can be fetched in parallel."""
        end = min(CHUNK, f.size) - 1
        with opened(f.uri, 0, end) as res:
            if res.status != 206:  # no range support: this response is the whole file
                with open(f.part, "wb") as out:
                    got = _copy(res, out, on_bytes)
                if got < f.size:
                    raise ShortRead(f"{f.path.name}: got {got} bytes, expected {f.size}")
                f.finish()
                return
            with open(f.part, "wb") as out:
                out.truncate(f.size)
                f.done = set()
                f.save()
                got = _copy(res, out, on_bytes)
        if got != end + 1:
            raise ShortRead(f"{f.path.name}: got {got} bytes of range 0-{end}")
        f.mark(0)
        starts = range(CHUNK, f.size, CHUNK)
        f.left = 1 + len(starts)
        for start in starts:
            put(
                lambda start=start: attempt(lambda on: chunk(f, start, min(start + CHUNK, f.size) - 1, on)),
                f.size,
            )
        f.chunk_done()

    def worker():
        while True:
            item = todo.get()
            try:
                if item[2] is None:
                    return
                if not failure:
                    if cancel.is_set():
                        raise Cancelled
                    item[2]()
            except BaseException as e:
                failure.append(e)
            finally:
                todo.task_done()

    started, needed = [], 0
    for uri, path, size in sorted(files, key=lambda f: -(f[2] or 0)):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and size and path.stat().st_size == size:
            on_bytes(size)
            continue
        f = _File(uri, path, size, links)
        started.append(f)
        if not size or size <= CHUNK:
            needed += size or 0
            put(lambda f=f: attempt(lambda on: stream(f, on)), size or 0)
        elif (have := f.resume()) is None:
            needed += size
            put(lambda f=f: attempt(lambda on: first(f, on)), size)
        else:  # an earlier attempt left chunks on disk: fetch only the missing ones
            for i in have:
                on_bytes(f.span(i)[1] - f.span(i)[0] + 1)
            missing = [i for i in range(f.chunks()) if i not in have]
            needed += sum(f.span(i)[1] - f.span(i)[0] + 1 for i in missing)
            f.left = len(missing)
            if not missing:
                put(f.finish, size)
            for i in missing:
                start, end = f.span(i)
                put(lambda f=f, start=start, end=end: attempt(lambda on: chunk(f, start, end, on)), size)

    if needed and files and needed + MARGIN > free_space(files[0][1].parent):
        raise DiskFull("Not enough disk space")
    rate.start(done[0])  # bytes already on disk from an earlier attempt are not transfer speed
    threading.Thread(target=watch, daemon=True).start()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    todo.join()
    for _ in threads:
        todo.put((1, 0, None))  # sentinels sort after every real job
    for t in threads:
        t.join()
    over.set()
    if cancel.is_set():
        for f in started:  # unfinished files leave nothing behind
            if not f.path.exists():
                f.part.unlink(missing_ok=True)
                f.sidecar.unlink(missing_ok=True)
        for _, path, _ in files:
            for folder in path.parents:
                try:
                    folder.rmdir()  # only empty ones go
                except OSError:
                    break
        raise Cancelled
    if failure:
        raise failure[0]
    return [path for _, path, _ in files]
