"""Print release notes markdown: install links for Blender's static repository."""

import glob
import os
import tomllib
from urllib.parse import quote

repo, tag = os.environ["REPO"], os.environ["TAG"]
with open("blender_manifest.toml", "rb") as f:
    m = tomllib.load(f)
zip_name = os.path.basename(glob.glob("dist/*.zip")[0])
base = f"https://github.com/{repo}/releases"
# `latest` so Blender keeps seeing new versions after the first install.
index = quote(f"{base}/latest/download/index.json", safe="")
dnd = f"{base}/download/{tag}/{zip_name}?repository={index}&blender_version_min={m['blender_version_min']}"

print(f"""## Install

**Drag this link into Blender** (adds the repository and installs {m["name"]}):

[{m["name"]} {m["version"]}]({dnd})

Or: Preferences → Get Extensions → Repositories → `+` → Add Remote Repository, paste:

```
{base}/latest/download/index.json
```

Requires Blender {m["blender_version_min"]}+. The zip below can also be installed manually via *Install from Disk*.
""")
