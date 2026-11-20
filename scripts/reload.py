"""Reload the extension in a running Blender, through the Blender MCP add-on's socket."""

import json
import socket
import sys

NAME = "bl_ext.user_default.quixlend"
MCP_PORT = 9876

CODE = f"""
import sys, addon_utils
addon_utils.disable({NAME!r}, default_set=False)
for k in [k for k in sys.modules if k == {NAME!r} or k.startswith({NAME!r} + ".")]:
    del sys.modules[k]  # enable alone keeps the stale sub-modules
addon_utils.enable({NAME!r}, default_set=True, persistent=True)
print("reloaded", {NAME!r})
"""


def main():
    try:
        sock = socket.create_connection(("localhost", MCP_PORT), timeout=30)
    except OSError:
        sys.exit(
            f"Blender MCP not reachable on localhost:{MCP_PORT} (is Blender open with the MCP add-on on?)"
        )
    with sock:
        sock.sendall(json.dumps({"type": "execute_code", "params": {"code": CODE}}).encode())
        data = b""
        while True:
            data += sock.recv(65536)
            try:
                reply = json.loads(data)
                break
            except ValueError:
                pass  # reply not complete yet
    if reply.get("status") == "error":
        sys.exit(reply.get("message"))
    print(reply["result"]["result"].strip())


main()
