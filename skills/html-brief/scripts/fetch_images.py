# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow"]
# ///
"""_raw/*.md의 "## 이미지 후보" 줄(이미지URL — 페이지URL — 설명)을 내려받아 _img/ 와 _img/manifest.json 을 만든다.

usage: uv run <skill>/scripts/fetch_images.py <작업폴더>
_raw/*.md 안 "## 이미지 후보" 절의 줄 형식: `- 이미지URL — 페이지URL — 설명`
이미 받은 파일은 건너뛴다. 20KB 미만·가로 400px 미만·이미지 아닌 응답은 버린다.
"""
from __future__ import annotations

import io
import sys
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
    sys.exit(__doc__)
ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
IMG = ROOT / "_img"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
LINE = re.compile(r"^\s*-?\s*(https?://\S+?)\s+[—-]+\s+(https?://\S+?)\s+[—-]+\s+(.+)$")


def candidates() -> list[dict]:
    out = []
    for p in sorted((ROOT / "_raw").glob("*.md")):
        src = p.read_text(encoding="utf-8", errors="ignore")
        i = src.find("## 이미지 후보")
        if i < 0:
            continue
        j = src.find("\n## ", i + 5)
        for ln in src[i:j if j > 0 else None].splitlines():
            m = LINE.match(ln)
            if m:
                out.append({"lane": re.sub(r"\W", "", p.stem)[:12], "img": m.group(1).strip("<>()"), "page": m.group(2).strip("<>()"), "desc": m.group(3).strip()})
    return out


def fetch(c: dict, n: int) -> dict | None:
    ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
    for f in IMG.glob(f"L{c['lane']}_{n:03d}.*"):
        return {**c, "file": f"_img/{f.name}", "bytes": f.stat().st_size}
    try:
        req = urllib.request.Request(c["img"], headers={"User-Agent": UA, "Referer": c["page"]})
        data = urllib.request.urlopen(req, timeout=25).read()
        im = Image.open(io.BytesIO(data))
        if len(data) < 20_000 or im.width < 400 or im.format not in ext:
            return None
    except Exception:
        return None
    name = f"L{c['lane']}_{n:03d}.{ext[im.format]}"
    (IMG / name).write_bytes(data)
    return {**c, "file": f"_img/{name}", "bytes": len(data)}


def main() -> None:
    IMG.mkdir(exist_ok=True)
    cs = candidates()
    with ThreadPoolExecutor(12) as ex:
        got = [r for r in ex.map(fetch, cs, range(1, len(cs) + 1)) if r]
    (IMG / "manifest.json").write_text(json.dumps(got, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["# 이미지 출처", ""] + [f"- {r['file']} — {r['desc']} — 페이지 {r['page']} — 원본 {r['img']}" for r in got]
    (IMG / "SOURCES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"후보 {len(cs)}  받음 {len(got)}")


if __name__ == "__main__":
    main()
