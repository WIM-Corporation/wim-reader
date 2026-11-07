# 본문 조각 작성 규격 (모든 작성 에이전트 공통)

독자가 "이만큼이면 된다"고 받아들인 팩들의 작성 규칙을 일반화했다. 작성 에이전트에게는 이 파일과 그 작업의 `_build/AUTHORING.md`를 함께 읽힌다.

## 파일

- 경로: `<작업폴더>/_build/pages/NN_slug.html` (slug는 영문 소문자·밑줄). 브리프는 `01_main.html` 하나.
- 뼈대 없는 조각이다. `<!doctype>`, `<html>`, `<head>`, `<body>`, `<style>`, `<script>`, `<h1>`, `<table>`, `<blockquote>`를 쓰지 않는다. 뼈대·CSS·다크모드·목차는 build.py가 씌운다.
- 첫 줄은 반드시 `<!-- title: 장 제목 | lead: 장 전체를 한 문장으로 -->`
- 다 쓰면 `uv run <스킬폴더>/scripts/check.py <작업폴더> <조각>` (스킬폴더 예: `~/.claude/skills/html-brief`)을 돌려 오류를 0으로, 경고를 줄인다.

## 근거

- 숫자는 `_raw/*.md`·`_data/*`에 있는 값만 쓴다. 판정이 REFUTED인 값은 쓰지 않는다(정정값은 쓴다).
- 그림 라벨 숫자는 원문 표기 그대로 쓴다. check.py가 원문에 없는 숫자를 경고한다. 환산·합산이 꼭 필요하면 계산 스크립트 결과(`_data/*.json`)에 넣고 거기서 인용한다.
- 같은 수치가 출처마다 다르면 둘 다 보이고 차이를 그림으로 보인다. 결과는 점 하나 대신 하한·기준·상한 띠로 보인다.
- 확인하지 못한 수치는 지어내지 않는다. `<span class="star">★</span>`, SVG에서는 `s-star`·`s-tstar`. 가격은 출처 URL과 신뢰도(A 공식 / B 협회·유통 / C 기사·추정)를 붙인다.

## 절 구조 (이 순서만 쓴다)

```html
<h2 id="pNN-s1">절 제목</h2>
<p>한 문장.</p>
<figure class="fig"><svg viewBox="0 0 880 H" role="img" aria-label="그림 설명">…</svg>
<figcaption>캡션 한 줄.</figcaption></figure>
```

- 절마다 그림 1개 이상(check.py가 그림 없는 절을 오류로 낸다). 팩은 장당 절 12~16개·SVG 12개 이상, 브리프는 절 5~10개.
- 절의 글은 한 문장(최대 두 문장). 설명은 그림 안 라벨로 옮긴다. 긴 문단·불릿 목록·표 금지.
- 그림당 글자(그림 밖 글자 ÷ 그림 수)는 250 이하. 버리기 아까운 세부는 `<details><summary>근거 더 보기</summary>…</details>`로 접는다(접힌 글은 세지 않는다).
- 웹 사진: 장당 2~4장. 사물·현장·제품이 나오는 주제(공장, 장비, 로봇, 숙소, 렌터카, 항공편)는 반드시 넣는다.
  `<figure class="fig img"><img src="_img/파일.jpg" alt="설명" loading="lazy"><figcaption>캡션. 출처: <a href="페이지URL">사이트명</a></figcaption></figure>`
  나란히: `<figure class="fig img"><div class="imgrow"><img …><img …></div><figcaption>…</figcaption></figure>`
  우리 실측 데이터가 있으면 PDF·PPT 속 그림을 오려 쓰지 말고 원본 데이터로 다시 그린다.
- 용어: 장 끝에 칩 `<div class="chips"><span><b>OEE</b> 설비종합효율</span>…</div>`(8~15개). 용어가 이해의 핵심인 학습 팩이면 칩 대신 용어마다 절 하나(정의 한 문장 + 그림 + 이 문서에서의 쓰임 + 비유)를 준다.
- 강조 카드: `card warn`(주의·반증), `card ask`(결정 전에 확인할 질문), `card kr`(지역·국가 관점), `card own`(우리 팀·작성자 관점). 장당 합쳐 3개 이하.
- 장 맨 끝: `<h2 id="pNN-src">출처</h2><ul class="src"><li><a href="URL">제목</a> (등급 A/B/C)</li>…</ul>`. 사진 출처도 여기.
- `id`는 `<h2>`에만, 반드시 `pNN-` 접두사(한장 판에서 겹치지 않게).

## SVG 규칙

- `viewBox="0 0 880 H"`, H는 220~520. `width`/`height` 속성 금지.
- 색은 클래스로만 준다. hex·rgb·`style=`·`fill=`·`stroke=` 속성·`<style>`·`<defs>` 금지(다크모드가 깨진다). 예외: `fill="none"`.
- 상자: `s-box`(중립) `s-box2` `s-paper` `s-acc`(강조) `s-warn`(위험·병목) `s-ok`(정상·기회) `s-star`(미확인) `s-dash`(점선 묶음). 등장 주체 색 셋(자사·경쟁사·파트너 등 원하는 대로): `s-lime` `s-blue` `s-violet` (선 `s-lime-line …`, 채움 `s-f-lime …`, 글자 `s-tlime s-tblue s-tviolet`)
- 국가 비교(순서 KR→JP→US→CN): 상자 `s-kr s-jp s-us s-cn`, 선 `s-kr-line …`, 채움 `s-f-kr …`, 글자 `s-tkr s-tjp s-tus s-tcn`
- 선: `s-line` `s-acc-line` `s-warn-line` `s-ok-line`. 채움(막대·점): `s-f-acc s-f-warn s-f-ok s-f-muted s-f-star`
- 글자: `s-tb`(굵은 제목 15) `s-t`(본문 14) `s-ts`(작게 11.5) `s-tm`(회색 12.5) `s-mono`(코드) `s-ta s-tw s-tok s-tstar`(색 강조)
- 화살표: 선에 `marker-end="url(#ar)"`(회색) `#ar-acc` `#ar-warn` `#ar-ok`. 마커는 셸에 이미 있다.
- 그 밖의 도메인 색이 필요하면 `_build/extra.css`에 변수·클래스를 같은 방식(라이트·다크 둘 다)으로 더한다.
- 상자는 `rx="8"`. 글자는 상자 안에 들어가게 폭을 잡는다(한글 14px 한 글자 약 14px, 영문 약 7.5px). 겹침 금지. 가운데 정렬은 `text-anchor="middle"`.
- 굵기·크기를 바꿀 때 font 단축 속성 클래스를 새로 만들지 않는다(font 단축 속성은 굵기를 덮어써 무시되기 쉽다). 위 글자 클래스를 쓴다.
- 그림 종류를 섞는다: 흐름·아키텍처 블록도, 계층(스택) 그림, 돈·물건 흐름도, 막대 비교, 지도형 배치, 파이프라인(단계별 시간 막대), 누적 막대, 2×2 포지셔닝, 타임라인·간트, 의사결정 트리, 손익분기 곡선, 시퀀스(누가 언제 무엇을).
- 막대 길이는 값에 비례하게 계산한다(눈대중 금지). 그림이 많으면 SVG를 손으로 쓰지 말고 `_build/figs.py` 같은 생성 스크립트로 만든다.

## 글 규칙

- 한국어 서술체(~한다/~이다). 독자가 모르는 개념은 고등학생도 따라올 수준으로 풀고, 처음 나오는 약어는 그림 라벨에 한글 뜻을 붙인다.
- "|" 구분자와 ">" 블록인용을 쓰지 않는다. 수식은 유니코드(≤, ×, ㎡, ²). HTML 특수문자는 이스케이프(`&lt;` `&amp;`).
- 문서의 주인공을 흐리지 않는다. 사례·비교는 핵심 주장을 받치는 만큼만 쓴다.
