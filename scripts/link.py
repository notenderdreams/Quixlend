"""Symlink this repo into Blender's user_default extensions repo (through the Blender MCP socket), then enable it."""

from pathlib import Path

from reload import NAME, run

ROOT = Path(__file__).resolve().parent.parent

CODE = f"""
import os, bpy, addon_utils
repo = bpy.utils.user_resource("EXTENSIONS", path="user_default", create=True)
dest = os.path.join(repo, "quixlend")
if os.path.islink(dest):
    os.unlink(dest)
if os.path.exists(dest):
    raise SystemExit(f"{{dest}} exists and is not a symlink; remove it first")
os.symlink({str(ROOT)!r}, dest)
addon_utils.modules_refresh()
addon_utils.enable({NAME!r}, default_set=True, persistent=True)
print("linked", dest)
"""

print(run(CODE))
