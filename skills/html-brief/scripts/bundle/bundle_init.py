# /// script
# dependencies = ["beautifulsoup4"]
# ///
"""묶음 통합 1단계: 목록 페이지가 가리키는 원문에서 본문 텍스트를 뽑고, 파트마다 html-brief 작업 폴더를 만든다.
usage: uv run <skill>/scripts/bundle/bundle_init.py <묶음폴더>
만드는 것: <묶음폴더>/_build/text/NN_tK.txt, <묶음폴더>/_build/parts/TN/(new_pack 뼈대, _raw/NN_tK.md = 원문 텍스트)
목록 페이지 형식(기본 00_list_local.html): <section class="tK"> 안에 <span class="tag">TN</span>이름<small>설명</small>,
카드 <a class="card" href="상대경로 또는 리더URL"><b>NN</b><div><h3>제목</h3><p>설명</p></div></a>
자세한 형식과 환경변수는 SKILL.md "묶음 통합"과 merge_lib.py 머리말.
"""
import re, os, sys, shutil, subprocess
if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
    sys.exit(__doc__)
os.environ["BUNDLE_DIR"] = os.path.abspath(sys.argv[1])
sys.path.insert(0, os.path.dirname(__file__))
from bs4 import BeautifulSoup
from merge_lib import themes, ROOT, BUNDLE, DATE

NEW_PACK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "new_pack.py")
os.makedirs(f"{BUNDLE}/_build/text", exist_ok=True)
for th in themes:
    pdir = f"{BUNDLE}/_build/parts/{th['tag']}"
    name = re.sub(r"[^\w가-힣]+", "_", re.sub(r"<[^>]+>", "", th["name"])).strip("_")
    subprocess.run([sys.executable, NEW_PACK, pdir, "--name", f"{th['tag']}_{name}_정리",
                    "--title", f"{th['tag']} {re.sub(r'<[^>]+>', '', th['name'])} 통합 정리",
                    "--lead", "(작성 에이전트가 채움)", "--date", DATE], check=True, capture_output=True, cwd=ROOT)
    for c in th["cards"]:
        p = f"{ROOT}/{c['rel']}"
        s = BeautifulSoup(open(p, encoding="utf-8", errors="replace").read(), "html.parser")
        for x in s(["script", "style", "noscript"]): x.decompose()
        for tr in s.find_all("tr"):
            tr.replace_with(" | ".join(td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])) + "\n")
        for h in s.find_all(re.compile("^h[1-6]$")):
            h.insert_before("\n" + "#" * int(h.name[1]) + " "); h.insert_after("\n")
        for img in s.find_all("img"):
            if img.get("alt"): img.replace_with(f"[그림: {img['alt']}]")
        t = re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", s.get_text("\n"))).strip()
        out = f"{BUNDLE}/_build/text/{c['num']}_{th['cls']}.txt"
        open(out, "w").write(f"# [{c['num']}] {c['title']}\n원본: {p}\n\n{t}\n")
        shutil.copy(out, f"{pdir}/_raw/{c['num']}_{th['cls']}.md")  # check.py가 _raw/*.md 숫자를 근거로 본다
    print(th["tag"], len(th["cards"]), "문서 →", pdir)
