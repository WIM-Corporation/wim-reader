# /// script
# requires-python = ">=3.10"
# ///
"""그림 위주 HTML 브리프·팩 빌드 (html-brief 스킬 공용).

usage: uv run <skill>/scripts/build.py <작업폴더>

입력: <작업폴더>/_build/pack.json, _build/pages/NN_slug.html (뼈대 없는 본문 조각), 선택 _build/extra.css, _build/pages/index_top.html
조각 첫 줄: <!-- title: 장 제목 | lead: 한 문장 -->
출력:
  조각 1개(브리프) -> <작업폴더>/<name>_<date>_local.html 한 개
  조각 2개 이상(팩) -> NN_slug_<date>_local.html + 00_index_local.html + <name>_한장_<date>_local.html
장마다 SVG·사진 수와 그림 밖 글자 수를 출력한다.
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
HEAD_RE = re.compile(r"<!--\s*title:\s*(.*?)\s*\|\s*lead:\s*(.*?)\s*-->", re.S)
DEFS = (
    '<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>'
    + "".join(
        f'<marker id="{i}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        f'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="{c}"/></marker>'
        for i, c in [("ar", "s-arrow"), ("ar-acc", "s-arrow-acc"), ("ar-warn", "s-arrow-warn"), ("ar-ok", "s-arrow-ok")]
    )
    + "</defs></svg>"
)
ONE_CSS = (
    "\nbody.one main{max-width:none;margin:0;padding:0}"
    ".one-wrap{display:grid;grid-template-columns:300px minmax(0,1fr);max-width:1360px;margin:0 auto}"
    "nav.side{position:sticky;top:0;height:100vh;overflow:auto;padding:20px 12px;border-right:1px solid var(--line);font-size:.84rem}"
    "nav.side a{color:var(--ink);display:block;padding:2px 6px;border-radius:6px}nav.side a:hover{background:var(--acc-soft);text-decoration:none}"
    "nav.side a.home{font-weight:700;margin-bottom:8px}nav.side .sp{color:var(--acc);font-weight:700;margin:12px 0 4px 6px}"
    "nav.side summary{list-style:none;cursor:pointer}nav.side summary::-webkit-details-marker{display:none}"
    'nav.side summary::before{content:"▸";color:var(--muted);margin-right:2px}nav.side details[open]>summary::before{content:"▾"}'
    "nav.side summary a{display:inline;padding:2px 2px}nav.side .sub a{color:var(--muted);font-size:.8rem;padding-left:18px}"
    ".one-main{padding:28px 32px 96px;min-width:0;max-width:1040px}"
    "section.chap{margin-top:72px;padding-top:28px;border-top:3px solid var(--acc)}p.up{font-size:.8rem;text-align:right}"
    "@media (max-width:900px){.one-wrap{display:block}nav.side{position:static;height:auto;max-height:50vh;border-right:0;"
    "border-bottom:1px solid var(--line)}.one-main{padding:16px}}\n"
)


def theme_js(key: str) -> str:
    return (
        f'<script>(function(){{var k="{key}-theme",r=document.documentElement;'
        'try{var s=localStorage.getItem(k);if(s)r.setAttribute("data-theme",s)}catch(e){}'
        'document.getElementById("tt").onclick=function(){var d=r.getAttribute("data-theme");'
        'if(!d)d=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";'
        'd=d==="dark"?"light":"dark";r.setAttribute("data-theme",d);try{localStorage.setItem(k,d)}catch(e){}}})();</script>'
    )


def shell(title: str, body: str, css: str, key: str, cls: str = "") -> str:
    b = f'<body class="{cls}">' if cls else "<body>"
    return (
        '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title>\n<style>\n{css}</style>\n</head>\n"
        f'{b}<button class="theme" id="tt" type="button">라이트/다크</button>{DEFS}<main>\n'
        f"{body}\n</main>{theme_js(key)}</body></html>\n"
    )


def outside_chars(body: str) -> int:
    t = re.sub(r"<svg\b.*?</svg>|<details\b.*?</details>|<[^>]+>", "", body, flags=re.S)
    return len(re.sub(r"\s+", "", t))


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)
    out = Path(sys.argv[1]).resolve()
    build = out / "_build"
    cfg = json.loads((build / "pack.json").read_text(encoding="utf-8"))
    title, lead, date, name = cfg["title"], cfg.get("lead", ""), cfg["date"], cfg["name"]
    parts = [(p[0], range(p[1], p[2] + 1)) for p in cfg.get("parts", [])]
    key = re.sub(r"\W", "", name) or "brief"
    css = (SKILL / "assets" / "style.css").read_text(encoding="utf-8")
    if (build / "extra.css").exists():
        css += "\n" + (build / "extra.css").read_text(encoding="utf-8")

    pages = []
    for p in sorted((build / "pages").glob("[0-9][0-9]_*.html")):
        src = p.read_text(encoding="utf-8")
        m = HEAD_RE.search(src)
        if not m:
            sys.exit(f"{p.name}: 첫 줄 title/lead 주석이 없다")
        pages.append({"n": int(p.name[:2]), "stem": p.stem, "title": m.group(1), "lead": m.group(2),
                      "body": HEAD_RE.sub("", src, 1), "out": f"{p.stem}_{date}_local.html"})
    if not pages:
        sys.exit("_build/pages/ 에 NN_slug.html 조각이 없다")
    top = build / "pages" / "index_top.html"
    top_html = top.read_text(encoding="utf-8") if top.exists() else ""

    def stat(pg: dict) -> str:
        b = pg["body"]
        nsvg, nimg = len(re.findall(r"<svg\b", b)), len(re.findall(r"<img\b", b))
        ch = outside_chars(b)
        return f'{pg["n"]:02d} {pg["stem"][3:]:<28} svg {nsvg:>3} img {nimg:>2}  그림 밖 글자 {ch:>6}  그림당 {ch // max(1, nsvg + nimg):>5}'

    # 브리프: 조각 하나 -> 단독 HTML 하나
    if len(pages) == 1:
        pg = pages[0]
        heads = re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', pg["body"], re.S)
        toc = ('<nav class="toc">' + "".join(f'<a href="#{a}">{re.sub("<.*?>", "", t)}</a>' for a, t in heads) + "</nav>") if len(heads) > 3 else ""
        body = f'<h1>{pg["title"]}</h1><p class="lead">{pg["lead"]}</p>{top_html}{toc}{pg["body"]}'
        dest = out / f"{name}_{date}_local.html"
        dest.write_text(shell(pg["title"], body, css, key), encoding="utf-8")
        print(stat(pg))
        print(f"정본 {dest}")
        return

    def part_of(n: int) -> str:
        return next((nm for nm, rng in parts if n in rng), "")

    for old in out.glob("[0-9][0-9]_*_local.html"):
        if not old.name.startswith("00_"):
            old.unlink()

    def docnav(cur: int) -> str:
        s = ['<nav class="docnav"><a href="00_index_local.html"' + (' class="cur"' if cur == 0 else "") + ">목록</a>"]
        last = ""
        for pg in pages:
            pt = part_of(pg["n"])
            if pt and pt != last:
                s.append(f'<span class="part">{pt[:1]}</span>')
                last = pt
            c = ' class="cur"' if pg["n"] == cur else ""
            s.append(f'<a{c} href="{pg["out"]}" title="{html.escape(pg["title"])}">{pg["n"]:02d}</a>')
        return "".join(s) + "</nav>"

    total = 0
    for i, pg in enumerate(pages):
        body = pg["body"]
        total += len(re.findall(r"<svg\b", body))
        heads = re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', body, re.S)
        toc = '<nav class="toc">' + "".join(f'<a href="#{a}">{re.sub("<.*?>", "", t)}</a>' for a, t in heads) + "</nav>"
        prev = f'<a href="{pages[i-1]["out"]}">← {pages[i-1]["n"]:02d} {pages[i-1]["title"]}</a>' if i else "<span></span>"
        nxt = (f'<a href="{pages[i+1]["out"]}">{pages[i+1]["n"]:02d} {pages[i+1]["title"]} →</a>'
               if i + 1 < len(pages) else '<a href="00_index_local.html">목록 →</a>')
        full = (docnav(pg["n"]) + f'<h1><span class="num">{pg["n"]:02d}</span>{pg["title"]}</h1><p class="lead">{pg["lead"]}</p>'
                + toc + body + f'<div class="pn">{prev}{nxt}</div>')
        (out / pg["out"]).write_text(shell(f'{pg["n"]:02d} {pg["title"]}', full, css, key), encoding="utf-8")
        print(stat(pg))

    groups = [(nm, [pg for pg in pages if pg["n"] in rng]) for nm, rng in parts] or [("", pages)]
    covered = {pg["n"] for _, items in groups for pg in items}
    if len(covered) < len(pages):
        groups.append(("기타", [pg for pg in pages if pg["n"] not in covered]))
    one_name = f"{name}_한장_{date}_local.html"

    idx = [docnav(0), f"<h1>{title}</h1>", f'<p class="lead">{lead}</p>',
           f'<p><a href="{one_name}">전 장을 한 페이지로 보기 →</a></p>', top_html]
    for nm, items in groups:
        if not items:
            continue
        idx.append((f'<h2 class="part">{nm}</h2>' if nm else "") + '<div class="grid">')
        idx += [f'<a href="{pg["out"]}"><b>{pg["n"]:02d}</b>{pg["title"]}<small>{pg["lead"]}</small></a>' for pg in items]
        idx.append("</div>")
    (out / "00_index_local.html").write_text(shell(title, "\n".join(idx), css, key), encoding="utf-8")

    # 한 장: 전 장을 한 HTML에 모으고 왼쪽에 접이식 목차를 둔다.
    # 탭 금지: 리더가 스크롤 깊이로 읽음을 판정하므로 내용을 숨기면 진행도가 틀어진다.
    if cfg.get("tabs"):
        print('  ! pack.json "tabs"는 지원하지 않는다. 무시하고 한 줄로 이어 붙인다.')
    side = [f'<nav class="side"><a class="home" href="#top">{title}</a>']
    main_ = [f'<div id="top"></div><h1>{title}</h1>', f'<p class="lead">{lead} 전 장을 한 페이지에 모은 판이다.</p>', top_html]
    seen: dict[str, int] = {}
    for gi, (nm, items) in enumerate(groups):
        if not items:
            continue
        if nm:
            side.append(f'<div class="sp">{nm}</div>')
        for pg in items:
            body = pg["body"]
            for hid in re.findall(r'\bid="([^"]+)"', body):
                seen[hid] = seen.get(hid, 0) + 1
                if seen[hid] == 2:
                    print(f"  ! 한 장 판 id 중복 {hid} ({pg['stem']}) — id에 pNN- 접두사를 붙인다")
            heads = re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', body, re.S)
            subs = "".join(f'<a href="#{a}">{re.sub("<.*?>", "", t)}</a>' for a, t in heads)
            side.append(f'<details><summary><a href="#ch{pg["n"]:02d}">{pg["n"]:02d} {pg["title"]}</a></summary>'
                        f'<div class="sub">{subs}</div></details>')
            main_.append(f'<section class="chap" id="ch{pg["n"]:02d}"><h1><span class="num">{pg["n"]:02d}</span>{pg["title"]}</h1>'
                         f'<p class="lead">{pg["lead"]}</p>{body}<p class="up"><a href="#top">맨 위로</a></p></section>')
    side.append("</nav>")
    one = shell(f"{title} (한 장)", '<div class="one-wrap">' + "".join(side) + '<div class="one-main">' + "\n".join(main_) + "</div></div>", css + ONE_CSS, key, cls="one")
    (out / one_name).write_text(one, encoding="utf-8")
    print(f"pages {len(pages)}  svg 합계 {total}")
    print(f"정본 {out / one_name}")


if __name__ == "__main__":
    main()
