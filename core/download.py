"""Fetch the files of a /v1/downloads response into an asset folder. No bpy."""
import os
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


def _fetch(uri, path, size, on_bytes):
    if path.exists() and size and path.stat().st_size == size:
        on_bytes(size)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    req = urllib.request.Request(uri, headers={"User-Agent": http.UA})
    with urllib.request.urlopen(req, timeout=60) as res, open(part, "wb") as f:
        while chunk := res.read(1 << 16):
            f.write(chunk)
            on_bytes(len(chunk))
    got = part.stat().st_size
    if size and got != size:
        part.unlink()
        raise DownloadError(f"{path.name}: got {got} bytes, expected {size}")
    part.replace(path)


def fetch_all(files, progress=None, workers=4):
    """Download planned files. progress(done_bytes, total_bytes) is called from worker threads."""
    total, done = sum(size or 0 for _, _, size in files), [0]

    def on_bytes(n):
        done[0] += n
        if progress:
            progress(done[0], total)

    with ThreadPoolExecutor(workers) as pool:
        for future in [pool.submit(_fetch, uri, path, size, on_bytes) for uri, path, size in files]:
            future.result()
    return [path for _, path, _ in files]
