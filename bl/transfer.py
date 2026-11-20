"""Downloading an asset: size estimate, the download operator and cancelling."""

import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bpy
import bpy.utils.previews
from bpy.props import StringProperty

from ..core import api, download
from . import jobs
from .library import have_resolution, measure_library, refresh_asset_browsers
from .store import HOME, Result, client, library, prefs, state

BUILD_SCRIPT = str(Path(__file__).with_name("build.py"))


_pool = ThreadPoolExecutor(4)


def download_size(asset_id, fetch=True):
    """Bytes the Download button would fetch at the chosen resolution; None until the asset listing arrives."""
    if fetch and prefs().fetch_sizes and asset_id not in state.assets:
        state.assets[asset_id] = None

        def fetch_asset():
            try:
                state.assets[asset_id] = client.asset(asset_id)
            except Exception:
                return
            jobs.dirty.set()

        _pool.submit(fetch_asset)
    asset = state.assets.get(asset_id)
    if not asset:
        return None
    key = (asset_id, int(prefs().resolution))
    if key not in state.sizes:
        state.sizes[key] = api.payload_size(asset, api.build_payload(asset, key[1]))
    return state.sizes[key]


class QUIXLEND_OT_download(bpy.types.Operator):
    bl_idname = "quixlend.download"
    bl_label = "Download"
    bl_description = "Download this asset and add it to the Asset Browser"
    asset_id: StringProperty()  # pyright: ignore[reportInvalidTypeForm]

    def execute(self, context: bpy.types.Context) -> Result:
        asset_id, root = self.asset_id, library()
        if asset_id in state.progress or asset_id in state.restart:
            return {"FINISHED"}  # already queued, downloading or building
        if asset_id in state.cancelling:
            state.restart.add(asset_id)  # the old worker still owns the files: start once it is gone
            jobs.redraw()
            return {"FINISHED"}
        resolution, blender = int(prefs().resolution), bpy.app.binary_path
        anti_tiling = prefs().anti_tiling
        state.progress[asset_id] = 0.0
        state.errors.pop(asset_id, None)
        stop = state.cancel[asset_id] = threading.Event()

        def progress(done_bytes, total):
            if asset_id in state.cancelling:
                return  # cancelled in the UI; the dying workers must not bring the bar back
            state.transferred[asset_id] = (done_bytes, total)
            state.progress[asset_id] = done_bytes / max(
                total, 1
            )  # the network part only; building is its own state
            jobs.dirty.set()

        def fetch():
            asset = client.asset(asset_id)
            payload = api.build_payload(asset, resolution)
            dest = download.asset_dir(root, asset)
            plan = lambda: download.plan(client.request_download(payload), dest)
            files = download.fetch_all(
                plan(),
                progress,
                refresh=plan,
                cancel=stop,
                speed=lambda bps, eta: (
                    asset_id in state.cancelling or state.speed.__setitem__(asset_id, (bps, eta))
                ),
            )
            return dest, download.resolution_of(files)

        def settle(error):
            state.progress.pop(asset_id, None)
            state.speed.pop(asset_id, None)
            state.transferred.pop(asset_id, None)
            state.cancelling.discard(asset_id)
            try:
                if isinstance(error, download.Cancelled):
                    return  # not a failure: no error on the card
                if error:
                    state.errors[asset_id] = download.reason(error)
                    return
                state.local.add(asset_id)
                measure_library()
                refresh_asset_browsers()
            finally:
                if asset_id in state.restart:  # clicked Download again while this was stopping
                    state.restart.discard(asset_id)
                    # one tick later: the old job releases its scheduler slot right after settle()
                    bpy.app.timers.register(
                        lambda: bpy.ops.quixlend.download(asset_id=asset_id) and None,  # pyright: ignore[reportAttributeAccessIssue]
                        first_interval=0,
                    )

        def run(finished):
            got = []  # resolution of the files just fetched

            def fetched(result, error):
                try:
                    state.cancel.pop(asset_id, None)  # nothing left to cancel
                    if not error and stop.is_set():
                        error = download.Cancelled()  # cancelled after the last byte arrived
                    if error:
                        settle(error)
                        return
                    state.building.add(asset_id)
                    dest, res = result
                    got.append(res)
                    jobs.run(lambda: build_asset(asset_id, dest, res, root, blender, anti_tiling), built)
                finally:
                    finished()  # network part over: the next queued download starts while this one builds

            def built(_, error):
                state.building.discard(asset_id)
                if not error and got and got[0]:
                    have_resolution(asset_id, got[0])
                settle(error)

            try:
                jobs.run(fetch, fetched)
            except BaseException as e:  # never leave the card stuck, never break the job that started us
                state.cancel.pop(asset_id, None)
                settle(e)
                finished()

        state.queue.limit = prefs().simultaneous_downloads
        state.queue.add(asset_id, run)
        jobs.redraw()  # show Queued/progress now, not on the next mouse move
        return {"FINISHED"}


def build_asset(asset_id, dest, res, root, blender, anti_tiling):
    """Build the asset's .blend from the files in `dest`, using the textures of size `res`. Runs on a worker thread."""
    thumb = HOME / "thumbs" / f"{asset_id}.jpg"  # Quixel's own catalog thumbnail
    if thumb.exists():
        shutil.copyfile(thumb, dest / "_Thumb.jpg")
    proc = subprocess.run(
        [blender, "-b", "--factory-startup", "--python", BUILD_SCRIPT, "--", str(dest), str(root)]
        + [str(res or 0)]
        + ([] if anti_tiling else ["--no-anti-tiling"]),
        capture_output=True,
        text=True,
    )
    if "QUIXLEND_BUILT" not in proc.stdout:
        raise RuntimeError((proc.stderr or proc.stdout).strip().splitlines()[-1])


def build_resolution(asset_id, res, then):
    """Build the .blend for texture size `res`, whose textures are already on disk; then() runs when done."""
    dest = next((library() / "Quixlend").glob(f"*/{asset_id}"), None)
    if dest is None:
        state.errors[asset_id] = "Asset folder is missing; download it again"
        return
    state.building.add(asset_id)
    root, blender, anti_tiling = library(), bpy.app.binary_path, prefs().anti_tiling
    jobs.redraw()

    def done(_, error):
        state.building.discard(asset_id)
        if error:
            state.errors[asset_id] = download.reason(error)
            jobs.redraw()
            return
        have_resolution(asset_id, res)
        refresh_asset_browsers()
        then()

    jobs.run(lambda: build_asset(asset_id, dest, res, root, blender, anti_tiling), done)


def cancel_download(asset_id):
    """Cancel at once as far as the UI goes; a running download stops and cleans up in the background."""
    event = state.cancel.get(asset_id)
    if event:
        event.set()
    if state.queue.cancel(asset_id):  # it had not started: nothing will report back, so clean up here
        state.cancel.pop(asset_id, None)
    elif event:
        state.cancelling.add(asset_id)  # settle() clears it when the workers are gone
        state.cancel.pop(asset_id, None)
        state.speed.pop(asset_id, None)
    state.transferred.pop(asset_id, None)
    state.progress.pop(asset_id, None)
    jobs.redraw()
