"""Downloading an asset: size estimate, the download operator and cancelling."""

import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import bpy
import bpy.utils.previews
from bpy.props import BoolProperty, StringProperty

from ..core import api, download
from . import jobs
from .library import measure_library, refresh_asset_browsers
from .placement import reload_templates
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
    replace: BoolProperty(  # pyright: ignore[reportInvalidTypeForm]
        default=False, description="Download again at the chosen resolution, replacing the textures"
    )

    def execute(self, context: bpy.types.Context) -> Result:
        asset_id, root = self.asset_id, library()
        replacing = self.replace and asset_id in state.local
        if asset_id in state.progress or asset_id in state.restart:
            return {"FINISHED"}  # already queued, downloading or building
        if asset_id in state.cancelling:
            state.restart.add(asset_id)  # the old worker still owns the files: start once it is gone
            jobs.redraw()
            return {"FINISHED"}
        resolution, blender = int(prefs().resolution), bpy.app.binary_path
        state.progress[asset_id] = 0.0
        state.errors.pop(asset_id, None)
        if replacing:
            state.local.discard(
                asset_id
            )  # the card shows progress; the old build stays on disk until the new one works
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

        def build(dest, res):
            thumb = HOME / "thumbs" / f"{asset_id}.jpg"  # Quixel's own catalog thumbnail
            if thumb.exists():
                shutil.copyfile(thumb, dest / "_Thumb.jpg")
            proc = subprocess.run(
                [
                    blender,
                    "-b",
                    "--factory-startup",
                    "--python",
                    BUILD_SCRIPT,
                    "--",
                    str(dest),
                    str(root),
                    *([str(res)] if res else []),
                ],
                capture_output=True,
                text=True,
            )
            if "QUIXLEND_BUILT" not in proc.stdout:
                raise RuntimeError((proc.stderr or proc.stdout).strip().splitlines()[-1])
            if res:  # the new build no longer needs the textures of the resolution it replaces
                for old in download.stale_maps([p for p in dest.rglob("*") if p.is_file()], res):
                    old.unlink(missing_ok=True)

        def settle(error):
            state.progress.pop(asset_id, None)
            state.speed.pop(asset_id, None)
            state.transferred.pop(asset_id, None)
            state.cancelling.discard(asset_id)
            try:
                if error and replacing:
                    state.local.add(asset_id)  # the old build is still intact
                if isinstance(error, download.Cancelled):
                    return  # not a failure: no error on the card
                if error:
                    state.errors[asset_id] = download.reason(error)
                    return
                state.local.add(asset_id)
                if replacing:
                    reload_templates(asset_id)
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
            def fetched(result, error):
                try:
                    state.cancel.pop(asset_id, None)  # nothing left to cancel
                    if not error and stop.is_set():
                        error = download.Cancelled()  # cancelled after the last byte arrived
                    if error:
                        settle(error)
                        return
                    state.building.add(asset_id)
                    jobs.run(lambda: build(*result), built)
                finally:
                    finished()  # network part over: the next queued download starts while this one builds

            def built(_, error):
                state.building.discard(asset_id)
                settle(error)

            try:
                jobs.run(fetch, fetched)
            except BaseException as e:  # never leave the card stuck, never break the job that started us
                state.cancel.pop(asset_id, None)
                settle(e)
                finished()

        state.queue.add(asset_id, run)
        jobs.redraw()  # show Queued/progress now, not on the next mouse move
        return {"FINISHED"}


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
