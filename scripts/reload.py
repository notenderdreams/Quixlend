"""Reload the extension in a running Blender, through the Blender MCP add-on's socket (localhost:9876)."""

import json
import socket
import sys

CODE = """
import sys, addon_utils
name = next(m.__name__ for m in addon_utils.modules() if m.__name__.endswith(".quixlend"))
addon_utils.disable(name, default_set=False)
for k in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
    del sys.modules[k]
addon_utils.enable(name, default_set=True, persistent=True)
print("reloaded", name)
"""


def main():
    try:
        sock = socket.create_connection(("localhost", 9876), timeout=30)
    except OSError:
        sys.exit("Blender MCP not reachable on localhost:9876 (is Blender open with the MCP add-on on?)")
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
