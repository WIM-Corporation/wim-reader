# /// script
# requires-python = ">=3.10"
# ///
"""본문 조각을 html-brief 규격(references/authoring.md)에 맞는지 검사한다.

usage: uv run <skill>/scripts/check.py <작업폴더> [pages/NN_slug.html ...]
조각을 안 주면 _build/pages/ 전부. 오류가 하나라도 있으면 종료 코드 1.
- 금지 태그·인라인 색·블록인용·"|" 구분자 → 오류
- 그림당 글자(그림 밖 글자 ÷ SVG·사진 수): 250 이하 통과, 600 초과 경고, 1000 초과 오류
- 그림 없는 절(h2) → 오류. SVG 글자가 viewBox 밖으로 나가면 경고
- 그림 안 글자도 센다(그림 밖 글만 세면 라벨에 절당 700~1,000자가 몰려도 통과해 '글자가 작고 많다'는 말을 듣는다):
  절 글자 합계(그림 밖+그림 안) 700 초과 경고·1,000 초과 오류, 그림 하나에 라벨 글자 300·라벨 25개 초과 경고,
  viewBox 폭 960 초과 경고(화면에서 글자가 작아진다), 라벨 절반 넘게 s-ts(작은 글자)면 경고, 접힌 글 절당 800 초과 경고
- 그림 라벨 숫자가 _raw·_data·_context 원문에 없으면 "근거 없는 숫자" 경고 (원문 폴더가 없으면 검사 생략)
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

BANNED = [
    (r"<(style|script|table|h1|html|body|head|defs|blockquote)\b", "금지 태그"),
    (r'\sstyle="', "style 속성"),
    (r'\s(fill|stroke)="(?!none")', "fill/stroke 속성(클래스로)"),
    (r"#[0-9a-fA-F]{3,6}\b(?![^<]*</a>)(?=[^<>]*>)", "hex 색"),
    (r"^\s*>", "블록인용"),
]
SCALE = {"s-ts": 0.89, "s-tm": 0.9, "s-mono": 0.95, "s-tb": 1.08}


def corpus(root: Path) -> str | None:
    files = [*sorted((root / "_raw").glob("*.md")), *sorted((root / "_context").glob("*.md"))]
    files += [p for p in sorted((root / "_data").glob("*")) if p.is_file()]
    if not files:
        return None
    return re.sub(r"[\s,]", "", "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in files))


def width(s: str) -> float:
    return sum(14 if ord(c) > 0x2E7F else 7.6 for c in s)


def check(root: Path, path: Path, corp: str | None, min_svg: int) -> int:
    src = path.read_text(encoding="utf-8")
    errs: list[str] = []
    warns: list[str] = []
    if not re.match(r"\s*<!--\s*title:.*?\|\s*lead:.*?-->", src, re.S):
        errs.append("첫 줄 title/lead 주석 없음")
    for pat, why in BANNED:
        for m in re.finditer(pat, src, re.M):
            errs.append(f"L{src.count(chr(10), 0, m.start()) + 1}: {why}: {m.group(0)[:40]}")
    svgs = re.findall(r"<svg\b.*?</svg>", src, re.S)
    small_total = [0, 0]
    for i, s in enumerate(svgs, 1):
        try:
            root_el = ET.fromstring(s)
        except ET.ParseError as e:
            errs.append(f"svg {i}: XML 오류 {e}")
            continue
        vb = root_el.get("viewBox", "").split()
        if len(vb) != 4:
            errs.append(f"svg {i}: viewBox 없음")
            continue
        if root_el.get("width") or root_el.get("height"):
            errs.append(f"svg {i}: width/height 속성 금지")
        w, h = float(vb[2]), float(vb[3])
        if w > 960:
            warns.append(f"svg {i}: viewBox 폭 {w:.0f} (880 기준, 넓으면 화면에서 글자가 작아진다)")
        labs = ["".join(el.itertext()).strip() for el in root_el.iter() if el.tag.endswith("text")]
        lc = sum(len(re.sub(r"\s+", "", t)) for t in labs)
        if lc > 300:
            warns.append(f"svg {i}: 그림 안 글자 {lc} (300 이하, 설명을 라벨에 몰지 말고 그림을 단순하게)")
        if len([t for t in labs if t]) > 25:
            warns.append(f"svg {i}: 라벨 {len(labs)}개 (25 이하)")
        nts = sum(1 for el in root_el.iter() if el.tag.endswith("text") and "s-ts" in (el.get("class") or "").split())
        small_total[0] += nts; small_total[1] += len([t for t in labs if t])
        for el in root_el.iter():
            if el.get("id"):
                errs.append(f"svg {i}: SVG 안 id {el.get('id')}")
            if el.tag.endswith("text"):
                t = "".join(el.itertext())
                try:
                    x, y = float(el.get("x", "0")), float(el.get("y", "0"))
                except ValueError:
                    continue
                cls = (el.get("class") or "").split()
                tw = width(t) * SCALE.get(cls[0] if cls else "", 1.0)
                anc = el.get("text-anchor", "start")
                left = x - tw / 2 if anc == "middle" else (x - tw if anc == "end" else x)
                if left < -2 or left + tw > w + 2 or y > h + 2:
                    warns.append(f"svg {i}: 글자가 그림 밖 '{t[:24]}' (x={x:.0f}, 폭≈{tw:.0f}, viewBox {w:.0f})")
    if corp is None:
        warns.append("_raw·_data·_context 없음: 근거 숫자 검사 생략")
    else:
        unsup = {n for s in svgs for t in re.findall(r"<text\b[^>]*>(.*?)</text>", s, re.S)
                 for n in re.findall(r"\d[\d,]*(?:\.\d+)?", re.sub(r"<[^>]+>", "", t))
                 if len(n.replace(",", "")) >= 3 and n.replace(",", "") not in corp}
        if unsup:
            warns.append(f"근거 없는 숫자 {len(unsup)}개: {' '.join(sorted(unsup)[:20])}")
    for m in re.finditer(r'<img\b[^>]*src="([^"]+)"', src):
        if not m.group(1).startswith(("http", "data:")) and not (root / m.group(1)).exists():
            errs.append(f"img 경로 없음 {m.group(1)}")
    vis = re.sub(r"<svg\b.*?</svg>|<[^>]+>", "", src, flags=re.S)
    if re.search(r"\s[|]\s", vis):
        errs.append("본문에 | 구분자")
    # 절마다 그림
    secs = re.split(r"(?=<h2\b)", src)[1:]
    for s in secs:
        hd = re.sub(r"<[^>]+>", "", re.search(r"<h2\b.*?</h2>", s, re.S).group(0))
        if hd.strip() in ("출처", "용어", "부록") or 'id="' in s[:80] and "-src" in s[:80]:
            continue
        if not re.search(r"<svg\b|<img\b", s):
            errs.append(f"그림 없는 절: {hd[:30]}")
        lab = sum(len(re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", t))) for t in re.findall(r"<text\b[^>]*>(.*?)</text>", s, re.S))
        out = len(re.sub(r"\s+", "", re.sub(r"<svg\b.*?</svg>|<details\b.*?</details>|<[^>]+>", "", s, flags=re.S)))
        dt = sum(len(re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", d))) for d in re.findall(r"<details\b.*?</details>", s, re.S))
        if out + lab > 1000:
            errs.append(f"절 글자 합계 {out + lab}(그림 밖 {out}+그림 안 {lab}) 1,000 초과: {hd[:24]}")
        elif out + lab > 700:
            warns.append(f"절 글자 합계 {out + lab}(그림 밖 {out}+그림 안 {lab}) 700 초과: {hd[:24]}")
        if dt > 800:
            warns.append(f"접힌 글 {dt} (절당 800 이하): {hd[:24]}")
    nimg = len(re.findall(r"<img\b", src))
    text = len(re.sub(r"\s+", "", re.sub(r"<svg\b.*?</svg>|<details\b.*?</details>|<[^>]+>", "", src, flags=re.S)))
    per = text // max(1, len(svgs) + nimg)
    print(f"{path.name}: h2 {len(secs)}, svg {len(svgs)}, img {nimg}, 그림 밖 글자 {text}, 그림당 {per}")
    if per > 1000:
        errs.append(f"그림당 글자 {per} (1000 초과: 글을 그림 라벨·<details>로 옮긴다)")
    elif per > 600:
        warns.append(f"그림당 글자 {per} (목표 250 이하)")
    elif per > 250:
        warns.append(f"그림당 글자 {per} (목표 250 이하, 600까지 허용)")
    if small_total[1] and small_total[0] / small_total[1] > 0.5:
        warns.append(f"그림 라벨 {small_total[1]}개 중 {small_total[0]}개가 작은 글자(s-ts): 본문 라벨은 s-t(14), s-ts는 단위·각주만")
    if len(svgs) < min_svg:
        warns.append(f"SVG {len(svgs)}개 (목표 {min_svg} 이상)")
    for w_ in warns[:30]:
        print("  경고", w_)
    for e in errs[:40]:
        print("  오류", e)
    return 1 if errs else 0


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)
    root = Path(sys.argv[1]).resolve()
    files = [Path(a).resolve() for a in sys.argv[2:]] or sorted((root / "_build" / "pages").glob("[0-9][0-9]_*.html"))
    cfg_p = root / "_build" / "pack.json"
    cfg = json.loads(cfg_p.read_text(encoding="utf-8")) if cfg_p.exists() else {}
    min_svg = cfg.get("min_svg", 12 if len(files) > 1 else 3)
    corp = corpus(root)
    sys.exit(max((check(root, f, corp, min_svg) for f in files), default=0))


if __name__ == "__main__":
    main()
