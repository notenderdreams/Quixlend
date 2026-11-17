"""Run blocking work on a thread; deliver the result on Blender's main thread."""

import queue
import threading
import traceback

import bpy

_done = queue.Queue()
dirty = threading.Event()  # set by workers to request a UI redraw (progress updates)


def run(work, on_done):
    """work() runs on a thread; on_done(result, error) runs later on the main thread."""

    def target():
        try:
            _done.put((on_done, work(), None))
        except Exception as e:  # reported to the user by on_done
            traceback.print_exc()
            _done.put((on_done, None, e))

    threading.Thread(target=target, daemon=True).start()


def redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _pump():
    changed = dirty.is_set()
    dirty.clear()
    while not _done.empty():
        on_done, result, error = _done.get()
        try:
            on_done(result, error)
        except Exception:  # a failing callback must not kill the timer for every later job
            traceback.print_exc()
        changed = True
    if changed:
        redraw()
    return 0.05


def register():
    bpy.app.timers.register(_pump, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)
