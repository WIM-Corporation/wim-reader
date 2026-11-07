# /// script
# requires-python = ">=3.10"
# ///
"""완성된 HTML의 그림·글자 지표를 잰다. 고쳐 쓰기 전후 보고에 쓴다.

usage: uv run <skill>/scripts/audit.py <a.html> [b.html ...]
열: 크기, 그림 밖 글자(script·style·svg·<details> 제외), SVG, 사진, 표, 접기, 그림당 글자, 판정
판정 기준(references/design_lessons.md):
  250 이하 통과 / 600 이하 주의 / 그 위 다시 쓰기
"""
import re
import sys
from pathlib import Path


def audit(p: Path) -> str:
    s = p.read_text(encoding="utf-8", errors="ignore")
    svg, img = len(re.findall(r"<svg\b", s)), len(re.findall(r"<img\b", s))
    svg -= len(re.findall(r'<svg width="0" height="0"', s))
    tbl, det = len(re.findall(r"<table\b", s)), len(re.findall(r"<details\b", s))
    t = re.sub(r"<(script|style|svg)\b.*?</\1>|<details\b.*?</details>|<[^>]+>", "", s, flags=re.S | re.I)
    n = len(re.sub(r"\s+", "", t))
    per = n // max(1, svg + img)
    v = "통과" if per <= 250 else "주의" if per <= 600 else "다시 쓰기"
    return f"{len(s) // 1024:>6}KB 글자{n:>7} svg{svg:>4} img{img:>4} 표{tbl:>4} 접기{det:>3} 그림당{per:>6} {v:<5} {p.name}"


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)
    for a in sys.argv[1:]:
        print(audit(Path(a)))
