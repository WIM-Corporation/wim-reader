# /// script
# requires-python = ">=3.10"
# ///
"""완성된 HTML의 그림·글자 지표를 잰다. 고쳐 쓰기 전후 보고와, 공용 build.py를 안 쓰는 팩(자체 빌드)의 마지막 검사에 쓴다.

usage: uv run <skill>/scripts/audit.py <a.html> [b.html ...] [--all]
첫 줄: 크기, 그림 밖 글자(script·style·svg·<details> 제외), SVG, 사진, 표, 접기, 그림당 글자, 판정
판정 기준(references/design_lessons.md):
  250 이하 통과 / 600 이하 주의 / 그 위 다시 쓰기
그 아래: 절(h2)마다 그림 안 글자를 잰다. 한도를 넘은 절만 찍는다(--all이면 전부).
  절 글자 합계(그림 밖+그림 안, 공백 제외) 700 이하, 그림 안 글자 그림당 300·라벨 25개 이하,
  viewBox 폭 960 이하(넓으면 화면에서 글자가 작아진다), 접힌 글 절당 800 이하. 기준은 독자가 받아들인 장(절 합계 300~600)에서 잡았다. 그림 밖 글자만 세면 글이 그림 라벨로 옮겨 가도 통과해 버린다
  (실제 사례: 그림 밖 87~117자로 통과했지만 그림 안 절당 700~1,037자·라벨 40~76개, 화면 글자 10~11.6px).
"""
import re
import sys
from pathlib import Path

LIM = {"svgtxt": 300, "labels": 25, "sec": 700, "vbw": 960, "det": 800}


def plain(x: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", x))


def sections(s: str):
    mcol = re.search(r"main\s*\{[^}]*max-width:\s*(\d+)px", s)
    col = int(mcol.group(1)) - 60 if mcol else 880  # 본문 칸 폭(패딩 뺀 근사). 칸이 viewBox보다 넓으면 글자가 줄지 않는다
    body = re.sub(r"<(script|style)\b.*?</\1>", "", s, flags=re.S | re.I)
    parts = re.split(r"(?=<h2\b)", body)[1:]
    for p in parts:
        hd = plain(re.search(r"<h2\b.*?</h2>", p, re.S).group(0))[:28]
        svgs = [x for x in re.findall(r"<svg\b.*?</svg>", p, re.S) if 'width="0"' not in x[:80]]
        nimg = len(re.findall(r"<img\b", p))
        labels = [plain(t) for sv in svgs for t in re.findall(r"<text\b[^>]*>(.*?)</text>", sv, re.S)]
        labels = [t for t in labels if t]
        vbw = [float(m) for sv in svgs for m in re.findall(r'viewBox="[\d.\-]+ [\d.\-]+ ([\d.]+) [\d.]+"', sv[:400])]
        det = sum(len(plain(d)) for d in re.findall(r"<details\b.*?</details>", p, re.S))
        prose = len(plain(re.sub(r"<svg\b.*?</svg>|<details\b.*?</details>", "", p, flags=re.S)))
        nf = max(1, len(svgs))
        per_txt, per_lab = sum(map(len, labels)) // nf, len(labels) // nf
        flags = []
        if svgs and per_txt > LIM["svgtxt"]:
            flags.append(f"그림 안 글자 그림당 {per_txt}")
        if svgs and per_lab > LIM["labels"]:
            flags.append(f"라벨 그림당 {per_lab}개")
        if vbw and max(vbw) > LIM["vbw"] and max(vbw) > col:
            flags.append(f"viewBox 폭 {max(vbw):.0f}(글자 작아짐)")
        tot = prose + sum(map(len, labels))
        if tot > LIM["sec"]:
            flags.append(f"절 글자 합계 {tot}(그림 밖+안)")
        if det > LIM["det"]:
            flags.append(f"접힌 글 {det}")
        yield hd, prose, len(svgs) + nimg, per_txt, per_lab, det, flags


def audit(p: Path, show_all: bool) -> str:
    s = p.read_text(encoding="utf-8", errors="ignore")
    svg, img = len(re.findall(r"<svg\b", s)), len(re.findall(r"<img\b", s))
    svg -= len(re.findall(r'<svg width="0" height="0"', s))
    tbl, det = len(re.findall(r"<table\b", s)), len(re.findall(r"<details\b", s))
    t = re.sub(r"<(script|style|svg)\b.*?</\1>|<details\b.*?</details>|<[^>]+>", "", s, flags=re.S | re.I)
    n = len(re.sub(r"\s+", "", t))
    per = n // max(1, svg + img)
    v = "통과" if per <= 250 else "주의" if per <= 600 else "다시 쓰기"
    out = [f"{len(s) // 1024:>6}KB 글자{n:>7} svg{svg:>4} img{img:>4} 표{tbl:>4} 접기{det:>3} 그림당{per:>6} {v:<5} {p.name}"]
    rows = list(sections(s))
    bad = [r for r in rows if r[6]]
    out.append(f"  절 {len(rows)}개 중 그림 안 글자 한도 넘은 절 {len(bad)}개"
               f" (한도: 절 합계 {LIM['sec']}, 그림당 글자 {LIM['svgtxt']}·라벨 {LIM['labels']}, viewBox 폭 {LIM['vbw']}, 접힌 글 {LIM['det']})")
    for hd, prose, nfig, pt, pl, d, fl in (rows if show_all else bad):
        out.append(f"  {'!' if fl else ' '} {hd:<28} 그림 {nfig} 그림밖 {prose} 그림안/그림 {pt} 라벨/그림 {pl} 접힌 {d}  {' · '.join(fl)}")
    return "\n".join(out)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    for a in args:
        print(audit(Path(a), "--all" in sys.argv))
