"""Megascans catalog search + Quixel REST API + download payload builder. No bpy.

Payload logic is a port of Bridge's `initiateDownload` (app/actions/myscans.js)
and its helpers in app/utils/index.js.
"""
from . import http

API = "https://quixel.com/v1/"
ALGOLIA_URL = "https://6uj1i5a072-dsn.algolia.net/1/indexes/assets/query"
ALGOLIA_HEADERS = {
    "X-Algolia-Application-Id": "6UJ1I5A072",
    "X-Algolia-API-Key": "8f0c9e3c8c5bff863c6c56966c152853",
}
FBX = "application/x-fbx"

# Bridge's default-selected texture components (reducers/settings.js).
COMPONENTS = [
    ("albedo", "image/jpeg"), ("metalness", "image/jpeg"), ("roughness", "image/jpeg"),
    ("displacement", "jpeg+exr"), ("normal", "image/jpeg"), ("ao", "image/jpeg"),
    ("opacity", "image/jpeg"), ("brush", "image/jpeg"), ("fuzz", "image/jpeg"),
    ("translucency", "image/jpeg"), ("transmission", "image/jpeg"),
]


def search(query="", page=0, per_page=48, asset_type=None, ids=None):
    """Catalog search. `ids` restricts to those asset ids (owned-only view)."""
    body = {"query": query, "page": page, "hitsPerPage": per_page}
    filters = []
    if asset_type:
        filters.append(f'type:"{asset_type}"')
    if ids:
        filters.append("(" + " OR ".join(f"objectID:{i}" for i in ids) + ")")
    if filters:
        body["filters"] = " AND ".join(filters)
    return http.post(ALGOLIA_URL, body, ALGOLIA_HEADERS)


class Client:
    def __init__(self, session):
        self.session = session

    def _call(self, path, body=None):
        def go():
            headers = {"Authorization": f"Bearer {self.session.bearer()}"}
            return http.request(API + path, body=body, headers=headers,
                                method="POST" if body is not None else None, timeout=60)
        try:
            return go()
        except http.HttpError as e:
            if e.status != 401:
                raise
            self.session.refresh()
            return go()

    def profile(self):
        return self._call("users/self")

    def acquired(self):
        return self._call("assets/acquired")

    def asset(self, asset_id):
        return self._call(f"assets/{asset_id}/extended")

    def request_download(self, payload):
        """-> {asset, previews[], json[], components[], meshes[], billboards[], brushes[]},
        every item {uri (signed, ready to GET), contentLength}."""
        return self._call("downloads", payload)


# ---- payload builder -------------------------------------------------------

def _parse_res(res):
    return [int(x) for x in res.split("x")]


def _resolutions(asset):
    comps = asset.get("components") or []
    if comps and comps[0].get("uris"):
        found = [r["resolution"] for r in comps[0]["uris"][0]["resolutions"]]
        if found:
            return found
    return list(dict.fromkeys(m["resolution"] for m in asset.get("maps") or []))


def _res_x(resolution, asset):
    square = f"{resolution}x{resolution}"
    if asset["categories"][0] in ("3d", "3dplant"):
        return square
    return next((r for r in _resolutions(asset) if max(_parse_res(r)) == resolution), square)


def _process(components, exr_access):
    """Expand jpeg+exr selections by the account's EXR access (processComponents)."""
    out = []
    for c in components:
        jpeg, exr = {**c, "mimeType": "image/jpeg"}, {**c, "mimeType": "image/x-exr"}
        disp_exr = c["type"] == "displacement" and exr_access in ("displacement", "full")
        if c["mimeType"] == "image/jpeg":
            out += [jpeg, exr] if disp_exr else [c]
        elif c["mimeType"] == "image/x-exr":
            out.append(c if disp_exr or exr_access == "full" else jpeg)
        elif c["mimeType"] == "jpeg+exr":
            out += [jpeg, exr] if disp_exr or exr_access == "full" else [jpeg]
        else:
            out.append(c)
    return out


def build_payload(asset, resolution=4096, lods=(0,)):
    """Body for POST /v1/downloads. `lods` = LOD indices to fetch (0 = highest detail)."""
    asset_id = asset["id"]
    cat0 = asset["categories"][0].lower()
    v2 = asset.get("version") == 2
    is_free = asset.get("points") == 0
    acl = asset.get("acl") or {}
    exr_access = "full" if is_free else acl.get("exrAccess")

    max_res = max((max(_parse_res(r)) for r in _resolutions(asset)), default=resolution)
    if acl.get("resolution") and not is_free:
        max_res = min(acl["resolution"], max_res)
    resolution = min(resolution, max_res)
    res_x = _res_x(resolution, asset)

    def selected(prop):
        types = {c["type"] for c in asset.get(prop) or []}
        return [{"type": t, "mimeType": m, "resolution": res_x} for t, m in COMPONENTS if t in types]

    prop = "maps" if v2 else "components" if asset.get("components") else "billboards"

    if cat0.startswith("3d") and v2:
        max_lod = max(m["lod"] for m in asset["models"] if m["type"] == "lod")
        chosen = [i for i in range(max_lod + 1) if i in lods] or [0]
        meshes = [{"mimeType": FBX, "lod": i} for i in chosen]
        if "3d-mod-window" in (asset.get("tags") or []):
            for m in meshes:
                m["variations"] = [1]
        payload = {"asset": asset_id}
        # Plants: the last LOD is a billboard card with its own texture set.
        billboard = cat0 == "3dplant" and max_lod in chosen
        maps = [] if billboard and len(chosen) == 1 else _process(selected(prop), exr_access)
        if maps:
            payload["components"] = maps
        if cat0 == "3dplant":
            payload["billboards"] = _process(selected("billboards"), exr_access) if billboard else False
        payload["meshes"] = meshes
        return payload

    is_3d = cat0 == "3d"
    lod_normals = len(lods) > 1
    components = _process([c for c in selected(prop) if c["type"] != "normal" or not is_3d or lod_normals],
                          exr_access)
    config = {"highpoly": False, "ztool": False, "lowerlod_normals": lod_normals,
              "albedo_lods": False, "meshMimeType": FBX, "brushes": False}
    if is_3d:
        by_tris = sorted((m for m in asset.get("meshes") or [] if m["type"] == "lod"),
                         key=lambda m: -m["tris"])
        tris = [m["tris"] for i, m in enumerate(by_tris) if i in lods]
        config["lods"] = tris
        if not lod_normals and tris and any(c["type"] == "normal" for c in asset.get(prop) or []):
            components.append({"type": "normal", "resolution": res_x,
                               "mimeType": "image/jpeg", "tris": tris[0]})
    return {"asset": asset_id, "components": components, "config": config}


def payload_size(asset, payload):
    """Bytes a `build_payload` result will download, from the asset's file listing."""
    files = []  # (type, resolution, mimeType, tris, bytes)
    for c in asset.get("components") or []:
        for u in c.get("uris") or []:
            for r in u["resolutions"]:
                files += [(c["type"], r["resolution"], f["mimeType"], f.get("tris"), f["contentLength"])
                          for f in r["formats"]]
    pool = {"components": files}
    for key in ("maps", "billboards"):
        pool[key] = [(m["type"], m["resolution"], m["mimeType"], None, m["contentLength"])
                     for m in asset.get(key) or []]

    def pick(want, files):
        t, res, mime, tris = want["type"], want["resolution"], want["mimeType"], want.get("tris")
        mimes = ("image/jpeg", "image/x-exr") if mime == "jpeg+exr" else (mime,)
        return sum(f[4] for f in files if f[:3] in {(t, res, m) for m in mimes} and tris in (None, f[3]))

    comps = pool["maps"] if asset.get("version") == 2 else pool["components"]
    total = sum(pick(c, comps) for c in payload.get("components") or [])
    if isinstance(payload.get("billboards"), list):
        total += sum(pick(c, pool["billboards"]) for c in payload["billboards"])
    meshes = payload.get("meshes")
    if meshes:  # v2 plants/3D: models are listed per lod
        total += sum(m["contentLength"] for m in asset.get("models") or []
                     if m["type"] == "lod" and any(m["lod"] == w["lod"] and m["mimeType"] == w["mimeType"]
                                                   for w in meshes))
    elif payload.get("config", {}).get("lods"):
        for tris in payload["config"]["lods"]:
            total += sum(u["contentLength"] for m in asset.get("meshes") or []
                         if m["type"] == "lod" and m["tris"] == tris
                         for u in m["uris"] if u["mimeType"] == FBX)
    return total


def asset_info(asset):
    """[(label, value)] describing an extended asset, empty values dropped."""
    sem = asset.get("semanticTags") or {}
    cats = asset.get("categories") or []
    comps = asset.get("maps") or asset.get("components") or []
    tris = [m["tris"] for m in (asset.get("meshes") or []) + (asset.get("models") or []) if m.get("tris")]
    res = sorted({max(_parse_res(r)) for r in _resolutions(asset)})
    join = lambda v: ", ".join(v) if isinstance(v, list) else v
    rows = [
        ("Type", cats[0] if cats else ""),
        ("Category", " / ".join(cats[1:])),
        ("Points", "Free" if asset.get("points") == 0 else asset.get("points")),
        ("Physical size", asset.get("physicalSize")),
        ("Object size", f"{sem['minSize']} - {sem['maxSize']} m" if "minSize" in sem and "maxSize" in sem else ""),
        ("Resolutions", ", ".join(f"{r // 1024}K" if r >= 1024 else str(r) for r in res)),
        ("Max triangles", f"{max(tris):,}" if tris else ""),
        ("Maps", ", ".join(dict.fromkeys(c["type"] for c in comps))),
        ("Theme", join(sem.get("theme"))),
        ("Environment", join(sem.get("environment"))),
        ("Region", join([x for x in (sem.get("country"), sem.get("region")) if x])),
        ("Color", join(sem.get("color"))),
        ("Contains", join(sem.get("contains"))),
        ("Descriptive", join(sem.get("descriptive"))),
        ("Tags", join(asset.get("tags"))),
        ("ID", asset.get("id")),
    ]
    return [(k, str(v)) for k, v in rows if v not in ("", None, [])]
