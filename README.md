# WIM Reader

로컬 HTML 문서를 읽기 좋게 열어 주는 작은 서버와, 그림 위주 리서치 HTML을 만드는 Claude Code 스킬을 한 레포에 담았다.

> **Claude Code에게 맡기기:** 이 레포 링크를 주고 "셋업해줘"라고 하면 됩니다.
> 에이전트는 [`SETUP_FOR_CLAUDE.md`](SETUP_FOR_CLAUDE.md)를 읽고 설치부터 검증까지 마칩니다.
>
> ```
> https://github.com/WIM-Corporation/wim-reader 셋업해줘
> ```

## 무엇을 하나

**WIM Reader 서버** (`reader.py`, 파이썬 표준 라이브러리만 사용)

- 폴더 안의 HTML을 `http://127.0.0.1:8770`으로 열어 준다. 디스크의 파일은 고치지 않고, 응답할 때 리더 스크립트만 끼워 넣는다.
- **읽기 기록:** 어디까지 읽었는지, 책갈피와 메모를 SQLite에 남긴다. 다시 열면 읽던 절로 돌아가고, 폰에서 열어도 같은 위치로 간다.
- **단어 설명 카드:** 문서에서 단어를 고르고 `설명`(또는 `e`)을 누르면 `claude -p`가 카드를 만든다.
  - 순서: 4갈래 조사(원리·이 문서·웹 근거·실무)를 동시에 돌린다 → 카드를 쓴다 → 검증한다.
  - 카드 내용: 핵심 숫자 카드, mermaid 다이어그램 2개, 업계 사례와 출처, 확인할 질문.
  - 검증 단계에서 고친 문장과 근거가 약한 문장(★)은 카드 맨 끝에 표시된다.
- **하단 탭:** 카드를 `▾ 내리기`로 하단 탭에 내려 두고 계속 읽을 수 있다. 다 되면 알림이 뜨고, 탭을 누르면 다시 열린다.
- **카드 기록:** 왼쪽 아래 `카드` 버튼을 누르면 지금까지 만든 모든 카드 목록이 뜬다. 검색할 수 있고, 고르면 저장본이 바로 열린다.
- **색 맞춤:** 카드 색은 문서의 라이트·다크 색을 따른다. 대비가 모자라면 기본 팔레트로 바꾼다.
- 조회 CLI: `status`, `unread`, `recent`, `marks`, `explains`. 사람도 에이전트도 쓸 수 있다.

**html-brief 스킬** (`skills/html-brief`)

- "조사해서 html로 정리해줘", "브리프", "한 장으로 합쳐" 같은 요청에 쓰인다.
- SVG 도식과 그림을 중심으로, 그림 하나에 글은 250자 안쪽으로 쓴다.
- 다크 모드를 지원하는 한 장짜리 HTML을 만들고, 검사와 스크린샷까지 한다.

## 손으로 설치하기

필요한 것: [uv](https://docs.astral.sh/uv/), [Claude Code](https://docs.claude.com/claude-code)(`claude` 명령, 설명 카드용), macOS 또는 Linux.

```bash
git clone https://github.com/WIM-Corporation/wim-reader.git ~/Developer/wim-reader
cd ~/Developer/wim-reader
mkdir -p ~/wim-docs && cp examples/sample.html ~/wim-docs/
uv run reader.py --root ~/wim-docs install-agent           # macOS: 로그인 때 자동 실행
# Linux: uv run reader.py --root ~/wim-docs serve
ln -sfn "$PWD/skills/html-brief" ~/.claude/skills/html-brief
open http://127.0.0.1:8770/sample.html
```

자세한 단계(Linux systemd, Chrome 확장, 검증)는 [`SETUP_FOR_CLAUDE.md`](SETUP_FOR_CLAUDE.md)에 있다.

## 단축키

| 키 | 동작 |
|---|---|
| 단어 선택 후 `e` 또는 `설명` 칩 | 개념 카드 |
| `Esc` / `▾ 내리기` | 카드를 하단 탭으로 내리기 |
| `b` / `m` | 책갈피 / 메모 |
| `[` / `]` | 이전 / 다음 책갈피 |
| `d` | 읽음 표시 |
| 왼쪽 아래 `카드` | 지금까지 만든 카드 목록 |

## 설정

| 항목 | 방법 |
|---|---|
| 문서 폴더 | `--root` > `WIM_READER_ROOT` > `~/.config/wim-reader/root` > 현재 폴더 |
| 포트 | `serve --port 8771` |
| 카드가 가정하는 독자 | `WIM_READER_PERSONA="대학원생"` |
| 폰·태블릿 | `serve --lan` (토큰이 붙은 접속 URL을 출력한다) |

데이터는 `<문서 폴더>/.wim_reader/`(`reading.db`, `token`, `server.log`)에 저장된다. 이 폴더는 서버가 내보내지 않는다.

## 보안 메모

- 기본적으로 `127.0.0.1`에만 바인딩한다. Host 헤더를 검사해 DNS 리바인딩을 막는다.
- `--lan`을 쓰면 토큰 쿠키가 있어야 접속된다.
- 홈 폴더의 임의 HTML(`/__file/…`)은 이 컴퓨터(루프백)에서만 열린다.
- 설명 카드는 문서 일부(고른 곳 주변 최대 7,000자)를 `claude -p`에 보낸다. 웹 조사 갈래는 WebSearch와 WebFetch를 쓴다.
  민감한 문서라면 단어 설명 기능을 쓰지 않는다.

## 라이선스

MIT
