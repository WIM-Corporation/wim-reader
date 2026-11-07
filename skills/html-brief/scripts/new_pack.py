# /// script
# requires-python = ">=3.10"
# ///
"""html-brief 작업 폴더 뼈대를 만든다.

usage:
  uv run <skill>/scripts/new_pack.py <작업폴더> --name 파일이름 --title "제목" --lead "한 문장" [--date YYMMDD] [--pack]
기본은 브리프(조각 1개 → 단독 HTML 1개). --pack 이면 장 여러 개 + 목록 + 한장.
만드는 것: _build/pack.json, _build/AUTHORING.md(이 팩 고유 규칙), _build/pages/01_main.html(예시 조각), _raw/, _img/, _data/
이미 있는 파일은 덮어쓰지 않는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

SAMPLE = """<!-- title: {title} | lead: {lead} -->
<h2 id="p01-s1">절 제목</h2>
<p>한 문장.</p>
<figure class="fig"><svg viewBox="0 0 880 240" role="img" aria-label="그림 설명">
<rect class="s-box" x="20" y="80" width="220" height="80" rx="8"/><text class="s-tb" x="130" y="126" text-anchor="middle">입력</text>
<line class="s-acc-line" x1="240" y1="120" x2="320" y2="120" marker-end="url(#ar-acc)"/>
<rect class="s-acc" x="330" y="80" width="220" height="80" rx="8"/><text class="s-tb" x="440" y="126" text-anchor="middle">처리</text>
<line class="s-acc-line" x1="550" y1="120" x2="630" y2="120" marker-end="url(#ar-acc)"/>
<rect class="s-ok" x="640" y="80" width="220" height="80" rx="8"/><text class="s-tb" x="750" y="126" text-anchor="middle">결과</text>
</svg><figcaption>캡션 한 줄.</figcaption></figure>
<h2 id="p01-src">출처</h2>
<ul class="src"><li><a href="https://example.com">제목</a> (등급 A)</li></ul>
"""

AUTH = """# 이 {kind}의 고유 규칙

공통 규격은 html-brief 스킬의 `references/authoring.md`. 여기에는 이 작업만의 것을 적는다.

- 독자: (누가, 무엇을 모르는가. 예: 제품 기획 담당자와 의사결정자, 현장을 깊이 모른다)
- 요청 원문: (유저가 한 말 그대로)
- 결론 한 줄: (이 문서가 답하는 질문과 답)
- 범위: 리서치면 "~해야 한다" 권고를 쓰지 않는다 / 판정이면 조건 → 판단 + 킬 게이트
- 장 목록: NN_slug — 제목 — 담당 에이전트
- 근거 파일: _raw/*.md (숫자는 여기 있는 것만), _data/*.json
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--name", required=True, help="정본 파일 이름 앞부분(한글 가능), 예: 태양광_설치비용")
    ap.add_argument("--title", required=True)
    ap.add_argument("--lead", default="")
    ap.add_argument("--date", default=dt.date.today().strftime("%y%m%d"))
    ap.add_argument("--pack", action="store_true")
    a = ap.parse_args()
    root = Path(a.folder).resolve()
    b = root / "_build"
    for d in (b / "pages", root / "_raw", root / "_img", root / "_data"):
        d.mkdir(parents=True, exist_ok=True)
    cfg = {"title": a.title, "lead": a.lead, "date": a.date, "name": a.name}
    if a.pack:
        cfg["parts"] = [["A. 전체 지도", 1, 4], ["B. 세부", 5, 12], ["C. 판단·남은 질문", 13, 20]]
        cfg["min_svg"] = 12
    else:
        cfg["min_svg"] = 3
    for p, text in [
        (b / "pack.json", json.dumps(cfg, ensure_ascii=False, indent=1) + "\n"),
        (b / "AUTHORING.md", AUTH.format(kind="팩" if a.pack else "브리프")),
        (b / "pages" / "01_main.html", SAMPLE.format(title=a.title, lead=a.lead or "한 문장")),
    ]:
        if not p.exists():
            p.write_text(text, encoding="utf-8")
            print("만듦", p)
    print(f"다음: 조각 작성 → check.py {a.folder} → build.py {a.folder}")


if __name__ == "__main__":
    main()
