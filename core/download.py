"""Fetch the files of a /v1/downloads response into an asset folder. No bpy."""
import itertools
import os
import queue
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import http

SECTIONS = ("json", "brushes", "billboards", "components", "meshes")


class DownloadError(Exception):
    pass


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

    def __init__(self, uri, path, size):
        self.uri, self.path, self.size = uri, path, size
        self.part = path.with_name(path.name + ".part")
        self.left = 1
        self.lock = threading.Lock()

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


def fetch_all(files, progress=None, workers=8):
    """Download planned files. Big files are split into parallel byte-range chunks, biggest first;
    a server that ignores ranges is streamed whole. progress(done_bytes, total_bytes) is called from
    worker threads."""
    total, done, lock = sum(size or 0 for _, _, size in files), [0], threading.Lock()
    todo, failure, seq = queue.PriorityQueue(), [], itertools.count()

    def on_bytes(n):
        with lock:
            done[0] += n
            if progress:
                progress(done[0], total)

    def put(job, priority):
        todo.put((-priority, next(seq), job))

    def stream(f):
        with _get(f.uri) as res, open(f.part, "wb") as out:
            _copy(res, out, on_bytes)
        f.finish()

    def chunk(f, start, end):
        with _get(f.uri, start, end) as res:
            if res.status != 206:
                raise DownloadError(f"{f.path.name}: server ignored the byte range")
            with open(f.part, "r+b") as out:
                out.seek(start)
                got = _copy(res, out, on_bytes)
        if got != end - start + 1:
            raise DownloadError(f"{f.path.name}: got {got} bytes of range {start}-{end}")
        f.chunk_done()

    def first(f):
        """First chunk doubles as the range probe: a 206 means the rest can be fetched in parallel."""
        end = min(CHUNK, f.size) - 1
        with _get(f.uri, 0, end) as res:
            if res.status != 206:  # no range support: this response is the whole file
                with open(f.part, "wb") as out:
                    _copy(res, out, on_bytes)
                f.finish()
                return
            with open(f.part, "wb") as out:
                out.truncate(f.size)
                got = _copy(res, out, on_bytes)
        if got != end + 1:
            raise DownloadError(f"{f.path.name}: got {got} bytes of range 0-{end}")
        starts = range(CHUNK, f.size, CHUNK)
        f.left = 1 + len(starts)
        for start in starts:
            put(lambda start=start: chunk(f, start, min(start + CHUNK, f.size) - 1), f.size)
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
        f = _File(uri, path, size)
        put((lambda f=f: first(f)) if size and size > CHUNK else (lambda f=f: stream(f)), size or 0)

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
