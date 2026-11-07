# /// script
# dependencies = ["beautifulsoup4"]
# ///
"""파트 통합본 손실 검사.
1) 원문 전문 층: 각 원문의 본문 텍스트가 통합본 페이로드 안에 글자 그대로 있는지.
2) 정리 층: 원문의 날짜·★ 줄 숫자 중 정리 층(접힌 details 포함)에 다시 나온 비율과 빠진 날짜.
usage: uv run <skill>/scripts/bundle/check_parts.py <묶음폴더> [TN]
"""
import re, os, sys, glob
if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
    sys.exit(__doc__)
from bs4 import BeautifulSoup
os.environ["BUNDLE_DIR"] = os.path.abspath(sys.argv[1])
sys.path.insert(0, os.path.dirname(__file__))
from merge_lib import themes, ROOT, ESC, BUNDLE, DATE

B = BUNDLE
DETAIL = sys.argv[2] if len(sys.argv) > 2 else None  # 파트 태그를 주면 빠진 ★ 문장·날짜 문맥을 _build/parts/TN/_data/missing.md에 쓴다

def text(h):
    s = BeautifulSoup(h, "html.parser")
    for x in s(["script", "style", "noscript"]): x.decompose()
    return re.sub(r"\s+", " ", s.get_text(" ")).strip()

DATE_RE = re.compile(r"(?<![\d.])((?:1[0-2]|0?[1-9])/(?:3[01]|[12]\d|0?[1-9])(?![\d/])(?! ?(?:명|건|개|점|곳|대|회|장))|\d{2}-\d{2}(?:-\d{2})?|\d{1,2}월 ?\d{1,2}일)(?![\d/])")
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")

for th in themes:
    if DETAIL and th["tag"] != DETAIL:
        continue
    md = []
    f = glob.glob(f"{B}/{th['tag']}_*_통합_{DATE}_local.html")
    if not f:
        print(th["tag"], "통합본 없음"); continue
    page = open(f[0], encoding="utf-8").read()
    summ = page.split('id="p01-orig"')[0]
    st = text(summ)
    print(f"\n== {th['tag']} {os.path.basename(f[0])}  정리 층 {len(st):,}자")
    for c in th["cards"]:
        orig = open(f"{ROOT}/{c['rel']}", encoding="utf-8", errors="replace").read()
        m = re.search(rf'<script type="text/plain" id="doc{c["num"]}">(.*?)</script>', page, re.S)
        pay = m.group(1).replace(ESC, "</script") if m else ""
        same = text(re.sub(r"<base[^>]*>", "", pay)) == text(orig)
        ot = text(orig)
        dates = set(DATE_RE.findall(ot)); dmiss = sorted(d for d in dates if d not in st)
        star = [l for l in re.split(r"(?<=[.。!?])\s", ot) if "★" in l]
        snums = {n for l in star for n in NUM.findall(l) if len(n.replace(",", "")) >= 2}
        shit = sum(1 for n in snums if n in st)
        print(f"[{c['num']}] 원문전문 {'일치' if same else '불일치!'}  날짜 {len(dates)-len(dmiss)}/{len(dates)}  ★줄 숫자 {shit}/{len(snums)}"
              + (f"  빠진 날짜: {', '.join(dmiss[:12])}{' …' if len(dmiss) > 12 else ''}" if dmiss else ""))
        if DETAIL:
            md.append(f"\n## [{c['num']}] {c['title']}\n### 정리 층에 숫자가 빠진 ★ 문장")
            md += [f"- {l[:400]}" for l in star if any(n in snums and n not in st for n in NUM.findall(l))]
            md.append("### 정리 층에 없는 날짜(문맥)")
            for d in dmiss:
                i = ot.find(d); md.append(f"- {d}: …{ot[max(0, i-120):i+120]}…")
    if DETAIL and md:
        out = f"{B}/_build/parts/{th['tag']}/_data/missing.md"
        open(out, "w").write("# 정리 층 누락 후보(check_parts.py)\n" + "\n".join(md)); print("→", out)
