"""Catalog search: thumbnails, result pages, categories and the search properties on the window manager."""

import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bpy
import bpy.utils.previews
from bpy.props import BoolProperty, EnumProperty, StringProperty

from ..core import api, http
from . import catalog, jobs
from .store import HOME, PER_PAGE, TYPES, Result, state, wm_of


def big_preview(hit):
    """Path of the large preview image: the copy in the asset folder, else downloaded once."""
    if hit.get("big_path") and Path(hit["big_path"]).exists():
        return Path(hit["big_path"])
    path = HOME / "thumbs" / f"{hit['id']}_big.jpg"
    if not path.exists():
        req = urllib.request.Request(hit["big"], headers={"User-Agent": http.UA})
        with urllib.request.urlopen(req, timeout=30) as res:
            path.write_bytes(res.read())
    return path


def _thumb(hit):
    path = HOME / "thumbs" / f"{hit['id']}.jpg"
    if not path.exists():
        images = hit["previews"]["images"]
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        req = urllib.request.Request(
            images.get("thumbJpg") or images["thumb"], headers={"User-Agent": http.UA}
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            part.write_bytes(res.read())
        part.replace(path)
    return path


def thumb_image(hit):
    """Thumbnail datablock; undo/redo invalidates references, so look up by name and reload if gone."""
    name = f".quixlend_{hit['id']}"  # leading dot hides it from image lists
    image = bpy.data.images.get(name)
    if image is None and hit.get("path") and Path(hit["path"]).exists():
        image = bpy.data.images.load(hit["path"])
        image.name = name
        image.use_fake_user = True
    return image


def _show(hits, page, pages, append):
    """Make `hits` the result cards: replace the current ones, or add to them when `append`."""
    if not append:
        for old in state.hits:
            if img := bpy.data.images.get(f".quixlend_{old['id']}"):
                bpy.data.images.remove(img)
        state.hits = []
    state.hits.extend(hits)
    state.page, state.pages = page, pages


DECAL_CATEGORIES = ("decals", "decal")  # what Megascans calls a decal's category


def scope(asset_type, category):
    """(catalog type or None, category or None) to search for the sidebar's Type and Category choices."""
    if asset_type == "decal":  # not a catalog type: decals are atlases in the decals categories
        return "atlas", DECAL_CATEGORIES
    return (None if asset_type == "ALL" else asset_type), (
        category if asset_type != "ALL" and category != "ALL" else None
    )


def start_search(page=0, append=False):
    """Fetch a result page. Results of a search superseded by a newer one are dropped."""
    wm = wm_of(bpy.context)
    state.seq += 1
    seq = state.seq
    query, asset_type = wm.quixlend_query, wm.quixlend_type
    api_type, category = scope(asset_type, wm.quixlend_category)
    if wm.quixlend_downloaded:  # the library on disk: no network, no thread
        if not state.local:
            state.hits, state.page, state.pages = [], 0, 1
            state.status, state.loading = "Nothing downloaded yet", False
            return
        found = catalog.find(query, api_type, category)
        chunk = found[page * PER_PAGE : (page + 1) * PER_PAGE]
        state.loading = False
        _show([catalog.hit(r) for r in chunk], page, max(1, math.ceil(len(found) / PER_PAGE)), append)
        state.status = f"{len(found)} downloaded" if found else "No results"
        return
    state.status = "Searching…"
    state.loading = True

    def work():
        def attempt():
            res = api.search(query, page, PER_PAGE, api_type, None, category)
            with ThreadPoolExecutor(8) as pool:
                thumbs = list(pool.map(_thumb, res["hits"]))
            return res, thumbs

        def retrying(n):
            state.status = f"Connection dropped, retrying ({n}/2)…"
            jobs.dirty.set()

        return http.retry(attempt, on_retry=retrying, give_up=lambda: seq != state.seq)

    def done(result, error):
        if seq != state.seq:
            return
        state.loading = False
        if error:
            state.status = f"Search failed: {error}"[:120]
            if http.transient(error) and not append and state.local:  # offline: show what is on disk
                wm.quixlend_downloaded = True  # runs the search again, now local
                state.status = f"Offline · {state.status}"
            return
        res, thumbs = result
        hits = [
            {
                "id": hit["id"],
                "name": hit["name"],
                "type": hit.get("type"),
                "decal": hit.get("category") in ("decal", "decals"),
                "big": hit["previews"]["images"].get("preview") or hit["previews"]["images"]["thumb"],
                "path": str(path),
            }
            for hit, path in zip(res["hits"], thumbs)
        ]
        _show(hits, page, res["nbPages"], append)
        state.status = f"{res['nbHits']} assets" if state.hits else "No results"

    jobs.run(work, done)


class QUIXLEND_OT_search(bpy.types.Operator):
    bl_idname = "quixlend.search"
    bl_label = "Search Megascans"
    bl_description = "Search the Megascans catalog"
    more: BoolProperty(default=False, description="Append the next page instead of starting over")  # pyright: ignore[reportInvalidTypeForm]

    def execute(self, context: bpy.types.Context) -> Result:
        if self.more:
            start_search(state.page + 1, append=True)
        else:
            start_search()
        return {"FINISHED"}


def _search_update(self, context):
    start_search()


_category_items: list[tuple[str, str, str]] = []  # Blender keeps references into a dynamic enum's list


def _category_items_for(self, context):
    found = state.categories.get(self.quixlend_type, [])
    _category_items[:] = [("ALL", "All Categories", "")] + [
        (name, f"{name.title()} ({count})", "") for name, count in found
    ]
    return _category_items


def load_categories(asset_type):
    """Fetch the categories of a type once; the dropdown fills in when they arrive."""
    if (
        asset_type in ("ALL", "decal") or asset_type in state.categories
    ):  # decals have no sub-categories to list
        return
    state.categories[asset_type] = []  # marks it as requested

    def done(result, error):
        if error:
            state.categories.pop(asset_type, None)  # try again next time the type is picked
        else:
            state.categories[asset_type] = result
        jobs.redraw()

    jobs.run(lambda: http.retry(lambda: api.categories(asset_type)), done)


def _type_update(self, context):
    self["quixlend_category"] = 0  # back to "All Categories" without triggering a second search
    load_categories(self.quixlend_type)
    start_search()


def register_properties():
    wm = bpy.types.WindowManager
    wm.quixlend_query = StringProperty(name="Search", update=_search_update, options={"SKIP_SAVE"})  # pyright: ignore[reportAttributeAccessIssue]
    wm.quixlend_type = EnumProperty(name="Type", items=TYPES, update=_type_update)  # pyright: ignore[reportAttributeAccessIssue]
    wm.quixlend_category = EnumProperty(name="Category", items=_category_items_for, update=_search_update)  # pyright: ignore[reportAttributeAccessIssue]
    wm.quixlend_downloaded = BoolProperty(  # pyright: ignore[reportAttributeAccessIssue]
        name="Downloaded Only",
        description="Show only assets you have already downloaded",
        update=_search_update,
        options={"SKIP_SAVE"},
    )
    wm.quixlend_code = StringProperty(name="Authorization Code", options={"SKIP_SAVE"})  # pyright: ignore[reportAttributeAccessIssue]


def unregister_properties():
    wm = bpy.types.WindowManager
    del wm.quixlend_query, wm.quixlend_type, wm.quixlend_category, wm.quixlend_downloaded, wm.quixlend_code  # pyright: ignore[reportAttributeAccessIssue]
