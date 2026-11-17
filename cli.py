#!/usr/bin/env python3
"""Headless harness for the Quixel protocol — no Blender needed.

python3 cli.py login                 open Epic login page
python3 cli.py login <code>          finish login with the code Epic shows
python3 cli.py whoami
python3 cli.py owned                 ids of acquired assets
python3 cli.py search rock [--type 3d] [--owned]
python3 cli.py asset <id>            dump extended asset json
python3 cli.py download <id> [--res 2048] [--lods 0] [--out DIR] [--dry]
"""

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from core import api, auth, download

AUTH_FILE = Path.home() / ".quixlend" / "auth.json"
LIBRARY = Path.home() / "Documents" / "Megascans Library"


def owned_ids(client):
    res = client.acquired()
    items = res if isinstance(res, list) else res.get("assets") or res.get("data") or []
    return [i if isinstance(i, str) else i.get("assetID") or i.get("id") for i in items]


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login").add_argument("code", nargs="?")
    sub.add_parser("whoami")
    sub.add_parser("owned")
    s = sub.add_parser("search")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--type")
    s.add_argument("--owned", action="store_true")
    a = sub.add_parser("asset")
    a.add_argument("id")
    d = sub.add_parser("download")
    d.add_argument("id")
    d.add_argument("--res", type=int, default=2048)
    d.add_argument("--lods", default="0")
    d.add_argument("--out")
    d.add_argument("--dry", action="store_true", help="print payload and response, fetch nothing")
    args = p.parse_args()

    session = auth.Session(AUTH_FILE)
    client = api.Client(session)

    if args.cmd == "login":
        if not args.code:
            webbrowser.open(auth.LOGIN_URL)
            print(
                "Sign in with Epic in the browser. The page then shows JSON containing\n"
                '"authorizationCode". Finish with:  python3 cli.py login <code>\n\n'
                f"If the browser did not open:\n  {auth.LOGIN_URL}"
            )
            return
        session.login_with_code(args.code)
        print("Logged in as", session.email)
    elif args.cmd == "whoami":
        print(json.dumps(client.profile(), indent=1)[:2000])
    elif args.cmd == "owned":
        ids = owned_ids(client)
        print(len(ids), "owned:", " ".join(map(str, ids[:60])))
    elif args.cmd == "search":
        ids = owned_ids(client) if args.owned else None
        res = api.search(args.query, asset_type=args.type, ids=ids)
        print(res["nbHits"], "hits")
        for h in res["hits"]:
            print(f"{h['id']:12} {h.get('type', ''):10} {h['name']}")
    elif args.cmd == "asset":
        json.dump(client.asset(args.id), sys.stdout, indent=1)
    elif args.cmd == "download":
        asset = client.asset(args.id)
        payload = api.build_payload(asset, args.res, tuple(int(x) for x in args.lods.split(",")))
        print("payload:", json.dumps(payload))
        res = client.request_download(payload)
        dest = Path(args.out) if args.out else download.asset_dir(LIBRARY, asset)
        files = download.plan(res, dest)
        for _uri, path, size in files:
            print(f"  {size or '?':>10}  {path.relative_to(dest)}")
        if args.dry:
            return
        download.fetch_all(files, lambda done, total: print(f"\r{done}/{total} bytes", end=""))
        print(f"\n{len(files)} files -> {dest}")


if __name__ == "__main__":
    main()
