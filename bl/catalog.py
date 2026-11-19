"""Offline index of the downloaded assets, built from the meta JSON Megascans ships in each asset folder."""

import json
from typing import Any

import bpy

from . import jobs
from .store import HOME, library, state, wm_of

CACHE = HOME / "catalog.json"  # parsing every 50 KB meta file at startup would be slow; this is the slim copy


def load():
    """Instant start: the cached index from the last session."""
    try:
        state.catalog = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        state.catalog = {}


def _words(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [w for v in value for w in _words(v)]
    if isinstance(value, dict):
        return [w for v in value.values() for w in _words(v)]
    return []


def _record(asset_id, folder_type, meta, mtime):
    cats = [str(c).lower() for c in meta.get("categories") or []]
    words = [
        asset_id,
        meta.get("name") or asset_id,
        *cats,
        *_words(meta.get("tags")),
        *_words(meta.get("semanticTags")),
    ]
    return {
        "id": asset_id,
        "type": folder_type,
        "name": (meta.get("name") or asset_id).strip(),
        "cats": cats[1:],
        "h": " ".join(words).lower(),
        "mtime": mtime,
    }


def refresh():
    """Re-read the meta files that changed since the cached index, off the main thread."""
    root = library() / "Quixlend"
    old = dict(state.catalog)

    def work():
        found = {}
        for folder in root.glob("*/*"):
            meta_file = folder / f"{folder.name}.json"
            try:
                mtime = meta_file.stat().st_mtime_ns
                prev = old.get(folder.name)
                if prev and prev["mtime"] == mtime and prev["type"] == folder.parent.name:
                    found[folder.name] = prev
                    continue
                found[folder.name] = _record(
                    folder.name, folder.parent.name, json.loads(meta_file.read_text()), mtime
                )
            except (OSError, ValueError):
                continue  # no meta file yet (still downloading) or unreadable: not indexable
        if found != old:
            HOME.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(found, separators=(",", ":")))
        return found

    def done(found, error):
        if error or found == state.catalog:
            return
        state.catalog = found
        if wm_of(bpy.context).quixlend_downloaded:
            bpy.ops.quixlend.search()  # pyright: ignore[reportAttributeAccessIssue]  # the list was built from a stale index

    jobs.run(work, done)


def find(query, asset_type, category) -> list[dict[str, Any]]:
    """Downloaded assets matching every word of `query`, by name. `category` is one name or several (any of them)."""
    words = query.lower().split()
    wanted = {category.lower()} if isinstance(category, str) else {c.lower() for c in category or ()}
    return sorted(
        (
            r
            for r in state.catalog.values()
            if r["id"] in state.local
            and (not asset_type or r["type"] == asset_type)
            and (not wanted or wanted & set(r["cats"]))
            and all(w in r["h"] for w in words)
        ),
        key=lambda r: r["name"].lower(),
    )


def hit(rec) -> dict[str, Any]:
    """A search-result entry for an indexed asset, pointing at the images already on disk."""
    asset_id, folder = rec["id"], library() / "Quixlend" / rec["type"] / rec["id"]
    thumb = next(
        (p for p in (folder / "_Thumb.jpg", HOME / "thumbs" / f"{asset_id}.jpg") if p.exists()), None
    )
    big = folder / f"{asset_id}_Preview.png"
    return {
        "id": asset_id,
        "name": rec["name"],
        "type": rec["type"],
        "decal": bool({"decal", "decals"} & set(rec["cats"])),
        "big": "",
        "big_path": str(big) if big.exists() else "",
        "path": str(thumb or ""),
    }


def meta(asset_id) -> dict[str, Any] | None:
    """The asset's meta JSON from its folder, or None if it is not downloaded."""
    for f in (library() / "Quixlend").glob(f"*/{asset_id}/{asset_id}.json"):
        try:
            return json.loads(f.read_text())
        except (OSError, ValueError):
            return None
    return None
