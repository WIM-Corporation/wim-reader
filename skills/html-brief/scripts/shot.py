# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow"]
# ///
"""완성된 HTML을 headless Chrome으로 찍어 세로 조각 PNG로 자른다(겹침·넘침 눈검사용).

usage: uv run <skill>/scripts/shot.py <a_local.html> <out_dir> [light|dark]
출력: <out_dir>/<이름>_<theme>_00.png ... (조각당 높이 1500px). 원본 HTML은 건드리지 않는다.
찍은 PNG를 열어 글자 겹침·잘림·다크모드 대비를 본다.
Chrome/Chromium 경로: 환경변수 CHROME, 없으면 PATH와 macOS 기본 위치에서 찾는다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops

MAC_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def find_chrome() -> str | None:
    env = os.environ.get("CHROME")
    if env:
        return env
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        if shutil.which(name):
            return shutil.which(name)
    return next((p for p in MAC_PATHS if Path(p).exists()), None)


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)
    src = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2]).resolve()
    theme = sys.argv[3] if len(sys.argv) > 3 else "light"
    chrome = find_chrome()
    if not chrome:
        sys.exit("Chrome/Chromium을 찾지 못했다. CHROME 환경변수에 실행 파일 경로를 넣는다.")
    out.mkdir(parents=True, exist_ok=True)
    page = src.read_text(encoding="utf-8")
    page = page.replace('<html lang="ko">', f'<html lang="ko" data-theme="{theme}">', 1)
    page = page.replace("<head>", f'<head><base href="file://{src.parent}/">', 1)
    tmp = src.parent / f".shot_{src.stem}_{theme}.html"
    tmp.write_text(page, encoding="utf-8")
    full = out / f"{src.stem}_{theme}_full.png"
    try:
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=1100,16000",
                        f"--screenshot={full}", f"file://{tmp}"], check=True, capture_output=True)
    finally:
        tmp.unlink(missing_ok=True)
    im = Image.open(full).convert("RGB")
    bg = Image.new("RGB", im.size, im.getpixel((2, im.size[1] - 2)))
    box = ImageChops.difference(im, bg).getbbox()
    bottom = min(im.size[1], (box[3] if box else im.size[1]) + 40)
    for n, top in enumerate(range(0, bottom, 1500)):
        p = out / f"{src.stem}_{theme}_{n:02d}.png"
        im.crop((0, top, 1100, min(top + 1500, bottom))).save(p)
        print(p)
    full.unlink()


if __name__ == "__main__":
    main()
