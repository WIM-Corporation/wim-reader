---
name: html-brief
description: "Builds research briefs, reports and learning packs as figure-first local HTML (SVG diagrams + web photos, minimal text, light/dark mode, a one-page '한장' edition), checks them, builds them and opens them. Also merges a set of existing HTML pages into one lossless page per part (synthesis layer + full originals). Use when the user says \"make this an HTML\", \"HTML report\", \"brief\", \"one-pager\", \"merge these HTMLs into one\", \"one page per part\", \"learning pack\", \"more diagrams / architecture figures\", \"less text\", \"hard to read\", \"like this one\" with an HTML link, or in Korean \"html로 만들어\", \"html로 정리\", \"html 작성\", \"보고서 html\", \"브리프\", \"한장으로 합쳐\", \"html 하나로 뭉쳐\", \"병합해서 하나로\", \"파트별로 1장\", \"학습 팩\", \"그림/다이어그램/아키텍쳐 많이 넣어\", \"글자 줄여\", \"읽기 힘들다\", \"이거만큼\", or when rewriting an existing *_local.html. Rule of thumb from real use: accepted pages have ≤250 characters of prose per figure; rejected ones had 1,000+."
---

# 그림 위주 HTML 브리프·팩 (Figure-first HTML briefs)

읽기 쉬운 HTML과 다시 쓰라는 말을 듣는 HTML의 차이는 그림당 글자 수였다. 이 스킬은 그 차이를 규칙과 스크립트로 묶는다. 배경과 근거는 `references/design_lessons.md`.

The difference between HTML pages readers accept and pages they send back is prose per figure. This skill turns that into rules and scripts. Background: `references/design_lessons.md`.

## 규칙 (Rules)

1. 그림이 본문이다. 절마다 SVG나 사진 1개 이상, 그림당 글자 250 이하. 세부는 `<details>`로 접는다.
2. 사물·현장·제품·장소가 나오면 웹 사진을 넣는다. 결정용 문서(출장·운송·구매)는 시나리오마다 총액 띠, 리스크, 사진, 지도 핀을 나란히 둔다.
3. 고등학생이 따라올 수준으로 쓴다. 개념 → 구조 그림 → 실제 사례 순서. 학습 팩의 용어는 칩이 아니라 절 하나씩.
4. 팩은 언제나 한장(one-page) 판을 함께 만들고, 한장 판을 정본으로 연다.
5. 다 만들면 묻지 않고 연다(macOS `open`, Linux `xdg-open`). 고친 뒤에도 다시 연다. 경로만 알려 주고 끝내지 않는다.
6. 참고 페이지·양식(제품 페이지, 테크 블로그, 기존 HTML)을 받으면 그 구성을 따른다. 주인공 주제를 흐리는 비교군은 뺀다. 실측 그림은 PDF에서 오리지 말고 원본 데이터로 다시 그린다.
7. 내부 판단(점수, 원가, 내부 평가)은 내부용에만 둔다. 공유용은 따로 빌드한다.
8. 로컬 단독 HTML이 기본이다. 호스팅·아티팩트는 유저가 요청할 때만.
9. 탭 금지. 내용을 탭·토글로 숨기지 않고 한 페이지 아래로 이어 붙인다. 리더가 스크롤 깊이로 읽음을 판정하면 탭이 진행도를 망친다. 부 구분은 왼쪽 목차와 제목으로 한다. `"tabs": true`를 넣어도 build.py가 무시한다.
10. 숫자는 `_raw`·`_data`에서만 가져온다. 미확인 값은 ★, 가격은 출처 URL과 등급을 붙인다. 글 속 "|" 구분자·">" 인용 금지, 수식은 유니코드(≤, ×, ²).

## 크기 고르기 (Brief or pack)

- 브리프(기본): 한 주제, 절 10개 이하. 조각 1개 → `<폴더>/<이름>_<YYMMDD>_local.html` 1개.
- 팩: 전수조사·학습 팩·한장을 말했거나 목차가 장 5개를 넘을 때. 장마다 조각 → 장 HTML + `00_index_local.html` + `<이름>_한장_<YYMMDD>_local.html`.
- 이미 있는 HTML 여러 개를 합치는 경우(묶음 통합): 아래 "묶음 통합" 절. 파트(주제)마다 한 장이다. 수십 개를 한 파일에 통째로 이어붙이는 것은 정리가 아니다.
- 기존 HTML을 고쳐 쓰는 경우: 먼저 `audit.py`로 재고, 원래 빌드 스크립트가 있으면 그것을 고쳐 다시 빌드한다. 없으면 새 폴더 구조로 옮긴다. 원본은 `_v1`로 남긴다.

## 순서 (Steps)

스크립트 위치: 스킬을 설치한 곳. 예: `S=~/.claude/skills/html-brief/scripts` (프로젝트 설치면 `S=.claude/skills/html-brief/scripts`). 작업 폴더는 현재 작업 디렉터리 아래 아무 곳이나 된다. 스크립트는 Python 3.10+ 표준 라이브러리만 쓰고(사진·스크린샷·묶음 통합은 pillow·beautifulsoup4), `uv run`이면 의존성을 알아서 설치한다. `uv`가 없으면 `python3`로 돌리고 필요한 패키지만 pip로 깐다.

1. 뼈대: `uv run $S/new_pack.py <폴더> --name <파일이름> --title "<제목>" --lead "<한 문장>" [--pack]`
   폴더는 보통 `<주제>_<YYMMDD>/`. `_build/AUTHORING.md`에 독자, 요청 원문, 결론 한 줄, 장 목록을 채운다. 팩은 `_build/pack.json`의 parts(부 이름, 시작 장, 끝 장)를 고친다.
2. 조사: 장(또는 갈래)마다 에이전트를 병렬로 띄워 `_raw/<갈래>.md`에 근거·수치·출처 URL을 쓰게 한다. 사진이 필요하면 `## 이미지 후보` 절에 `- 이미지URL — 페이지URL — 설명` 줄을 받고 `uv run $S/fetch_images.py <폴더>`로 `_img/`에 내려받는다. 결과는 생기는 대로 파일에 붙인다.
3. 작성: 장마다 작성 에이전트에게 `references/authoring.md`, `_build/AUTHORING.md`, 해당 `_raw` 파일을 읽히고 `_build/pages/NN_slug.html` 조각을 쓰게 한다. 브리프면 직접 쓴다. 조사가 길면 뼈대 판을 먼저 빌드·오픈하고 장이 채워질 때마다 다시 빌드한다.
4. 검사: `uv run $S/check.py <폴더>`. 오류 0이 될 때까지 고친다. 경고 중 "근거 없는 숫자"와 "글자가 그림 밖"은 반드시 본다.
5. 빌드: `uv run $S/build.py <폴더>`. 마지막 줄 `정본 …`이 열 파일이다.
6. 눈검사(선택): 그림이 많은 장 1~2개를 `uv run $S/shot.py <장 HTML> <임시폴더>/shots [dark]`로 찍어 PNG를 본다(겹침, 잘림, 다크 대비). Chrome/Chromium이 필요하다(`CHROME` 환경변수로 경로 지정 가능). 한장 판은 너무 길어 잘리므로 장 HTML을 찍는다.
7. 열기: 정본 하나만 연다. `open "<정본>"` (macOS) 또는 `xdg-open "<정본>"` (Linux).
8. 보관(선택): 작업이 끝나면 정본 HTML(과 `_img/`)을 평소 보고서를 두는 곳으로 복사한다. 밑줄 폴더(`_raw`, `_build`, `_data`)는 작업 폴더에 남긴다.
9. 보고: `uv run $S/audit.py <정본>` 결과로 글자 수, SVG·사진 수, 그림당 글자를 알린다. 고쳐 쓴 경우 전후를 함께 쓴다.

## WIM Reader와 함께 쓰기 (optional)

같은 저장소의 WIM Reader 서버(기본 `http://127.0.0.1:8770`)를 띄워 두면 로컬 HTML을 그 주소로 열어 읽음 진행도·북마크·메모를 남길 수 있고, 내용이 바뀐 HTML은 내용 해시로 알아채 읽음 표시를 지운다. 없어도 모든 산출물은 `file://`로 단독으로 열린다. 묶음 통합의 리더 URL은 `HTML_BRIEF_READER` 환경변수로 바꿀 수 있다.

Pages work best when served by the WIM Reader in this repo (`http://127.0.0.1:8770`), which tracks scroll depth, bookmarks and notes. It is not required: every output opens standalone from disk.

## 파일 (Files)

- `assets/style.css`: 공용 CSS(색 변수 라이트·다크, SVG 클래스 팔레트, 목차·카드·칩·사진 줄). 작업 고유 색은 `<폴더>/_build/extra.css`.
- `references/authoring.md`: 조각 작성 규격(절 구조, SVG 클래스, 글 규칙). 작성 에이전트에게 그대로 넘긴다.
- `references/design_lessons.md`: 규칙의 근거(일반화한 설계 교훈과 판정 기준).
- `references/bundle_brief.md`: 묶음 통합 작성 에이전트 지시서.
- `scripts/`: new_pack.py, build.py, check.py, audit.py, shot.py, fetch_images.py. `scripts/bundle/`: 묶음 통합(bundle_init, build_parts, check_parts, merge_lib).

## 묶음 통합 (Merge many HTMLs into one page per part)

한 장의 구성은 둘이다. 위는 정리 층으로, 문서 순서가 아니라 주제 순서로 다시 엮은 html-brief 조각이다. 주장마다 `[NN]` 태그를 달고, 원문 숫자·★·날짜·미결은 `<details>`에 빠짐없이 넣는다. 아래는 원문 전문으로, 원본 HTML을 iframe srcdoc으로 그대로 붙인다(이미지 중복 제거, 150KB 넘는 그림만 WebP 재압축, vh 고정, 문서 사이 링크는 같은 장이나 다른 파트 장으로 이동).

입력은 묶음 폴더 안의 목록 페이지다(기본 이름 `00_list_local.html`, `HTML_BRIEF_INDEX`로 바꿀 수 있음). 형식:

```html
<section class="t1"><h2><span class="tag">T1</span>파트 이름<small>한 줄 설명</small></h2>
  <a class="card" href="docs/first.html"><b>01</b><div><h3>문서 제목</h3><p>한 줄 설명</p></div></a>
  <div class="gap">없는 문서 메모(선택)</div>
</section>
```

카드 `href`는 묶음 폴더 기준 상대 경로이거나, 리더 URL(`http://127.0.0.1:8770/<루트 기준 경로>`)이다. 리더 URL의 루트는 `HTML_BRIEF_ROOT`(기본: 현재 작업 디렉터리)다. 스크립트 위치는 `B=$S/bundle`.

1. `uv run $B/bundle_init.py <묶음폴더>`. 원문 텍스트를 `_build/text/`에 뽑고, 파트마다 `_build/parts/TN/` 뼈대를 만들고 `_raw/`를 채운다.
2. 파트마다 작성 에이전트를 병렬로 띄운다. 지시서는 `references/bundle_brief.md`이고, 프롬프트에는 파트의 문서 목록(번호·제목·한 줄)과 목록 페이지의 '없는 문서'만 더한다. 100KB 넘는 원문은 `grep '^#'`으로 목차를 먼저 본다.
3. `uv run $B/build_parts.py <묶음폴더> [TN…]`로 조립한다. 결과는 `<묶음폴더>/TN_<이름>_통합_<날짜>_local.html`.
4. `uv run $B/check_parts.py <묶음폴더> [TN]`로 손실을 검사한다. 원문 전문은 모든 원문이 "일치"여야 하고, 정리 층의 ★ 문장 숫자 반영률을 파트별로 보고한다. TN을 주면 빠진 항목이 `_build/parts/TN/_data/missing.md`에 나온다. 반영률이 90% 아래인 파트는 그 에이전트에게 missing.md를 반영시킨다. 큰 원장형 문서는 원문에서 고른 ★ 문장을 json에 모으고 생성 스크립트가 해당 절이나 `p01-star` "그 밖의 ★ 수치" 절에 붙이는 방식이 낫다.
5. 헤드리스 렌더 검사, 목록 페이지에 파트 링크 추가 뒤 첫 파트를 연다.
6. 보고에는 에이전트들이 찾은 문서 간 충돌과, 목록 페이지 설명 중 원문에 근거가 없는 것을 옮긴다. 이것이 '정리'의 값이다. 보고는 "손실 없음" 한 줄이 아니라 원문 전문 일치 수와 정리 층 반영률로 쓴다.

함정
- 한글 경로는 인자로 오면 NFC, 디스크(macOS)에서는 NFD일 수 있다. merge_lib가 디스크 표기로 맞추므로 glob을 새로 짤 때도 그 BUNDLE을 쓴다.
- `p01-star` 덤프는 다시 엮은 것이 아니라 손실을 막는 접힌 모음이다. 보고에 그렇다고 쓴다.

## 함정 (Pitfalls)

- 조각에 `style=`·`fill=`·hex 색을 쓰면 다크모드에서 글자가 사라진다. 클래스로만 칠한다.
- `<h2>` id에 `pNN-` 접두사가 없으면 한장 판에서 목차 링크가 겹친다(build.py가 `! 한 장 판 id 중복`으로 알린다).
- SVG 글자 폭은 한글 14px, 영문 7.5px로 잡는다. check.py 경고를 무시하면 상자 밖으로 삐져나온다.
- `_build/`·`_raw/`·`_img/`처럼 밑줄 폴더는 작업용이다. 산출물 HTML은 작업폴더 바로 아래에 둔다. 사진은 `_img/`에 두고 HTML과 함께 옮긴다(상대 경로).
