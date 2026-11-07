# SETUP_FOR_CLAUDE.md — Claude 에이전트용 설치 지침

이 문서는 **Claude Code 에이전트가 처음부터 끝까지 그대로 따라 하라고** 쓴 것이다.
사용자가 "이 레포 셋업해줘"라고 GitHub 링크를 주면, 아래 순서대로 실행하고 마지막 '검증'까지 통과시킨 뒤 '사용자에게 알려 줄 것'을 전달한다.

설치되는 것:

1. **WIM Reader 서버** (`reader.py`): 로컬 HTML을 `http://127.0.0.1:8770`으로 열어 준다.
   - 읽은 위치·책갈피·메모를 기록한다.
   - 단어를 고르면 `claude -p`가 조사·검증한 개념 카드를 띄운다. 카드는 하단 탭에 쌓인다.
2. **html-brief 스킬** (`skills/html-brief`): "html로 정리해줘" 같은 요청에 그림 위주의 리서치 HTML을 만드는 Claude Code 스킬.
3. (선택) **Chrome 확장**: Finder에서 .html을 더블클릭하면 리더 주소로 열어 준다.

---

## 0. 원칙

- 명령은 위에서부터 순서대로 실행한다. 각 단계의 "확인"이 통과해야 다음으로 간다.
- 사용자에게 물을 것은 **딱 하나**다: HTML 문서를 모아 둘 폴더. 기본값은 `~/wim-docs`이고, 사용자가 상관없다고 하면 기본값을 쓴다.
- macOS가 아니면 6단계의 Linux 절을 따른다. Windows는 WSL에서 Linux 절차로 한다.
- 사용자 대신 Chrome 설정 화면을 조작하지 않는다. 확장 설치는 사용자가 직접 할 일로 안내만 한다.

## 1. 준비물 확인

```bash
uname -s                       # Darwin이면 macOS
command -v git && git --version
command -v uv || echo "uv 없음"
command -v claude || echo "claude 없음"
```

- **uv가 없으면** 설치한다. 그 뒤 새 셸에서 `uv --version`이 찍히는지 본다.
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **claude CLI**: 지금 이 지침을 Claude Code로 읽고 있다면 이미 있다. 개념 카드 기능은 `claude -p`를 쓰므로 `claude` 명령이 PATH에 있어야 한다.
  - `command -v claude`가 비면 `~/.local/bin/claude`나 `npm ls -g @anthropic-ai/claude-code` 쪽을 찾아 보고, 경로를 사용자에게 알린다.
- Python은 uv가 알아서 받는다(3.11 이상).

## 2. 레포 받기

```bash
mkdir -p ~/Developer && cd ~/Developer
git clone https://github.com/WIM-Corporation/wim-reader.git
cd wim-reader
```

이미 있으면 `git -C ~/Developer/wim-reader pull`. 이후 이 폴더를 `$REPO`라고 부른다.

```bash
REPO=~/Developer/wim-reader
```

## 3. 문서 폴더 정하기

사용자에게 HTML을 모아 둘 폴더를 묻는다. 기본값은 `~/wim-docs`다.

```bash
DOCS=~/wim-docs           # 사용자가 다른 폴더를 말하면 그 경로
mkdir -p "$DOCS"
```

샘플 문서를 하나 넣어 둔다. 검증에도 쓴다.

```bash
cp "$REPO/examples/sample.html" "$DOCS/sample.html"
```

## 4. 리더 서버 실행

### macOS (로그인할 때 자동 실행)

```bash
cd "$REPO"
uv run reader.py --root "$DOCS" install-agent
```

- launchd 에이전트 `io.github.wim-reader`가 등록된다.
- 문서 폴더 경로는 `~/.config/wim-reader/root`에 저장된다.
- 로그는 `$DOCS/.wim_reader/server.log`에 쌓인다.

**확인** (2~3초 기다린 뒤):

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8770/        # 200
```

8770 포트를 이미 다른 프로그램이 쓰고 있으면 `--port 8771`처럼 바꿔 다시 설치하고, 바꾼 포트를 사용자에게 알린다.

### Linux / WSL

launchd가 없으므로 systemd 사용자 유닛으로 띄운다. `UV`에는 `command -v uv` 결과를 넣는다.

```bash
mkdir -p ~/.config/wim-reader ~/.config/systemd/user
echo "$DOCS" > ~/.config/wim-reader/root
cat > ~/.config/systemd/user/wim-reader.service <<EOF
[Unit]
Description=WIM Reader
[Service]
ExecStart=$(command -v uv) run --quiet $REPO/reader.py --root $DOCS serve
Restart=always
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now wim-reader
```

systemd가 없으면 다음처럼 띄우고 끝낸다. 이 경우 재부팅하면 다시 띄워야 한다고 사용자에게 알린다.

```bash
nohup uv run reader.py --root "$DOCS" serve >/tmp/wim-reader.log 2>&1 &
```

## 5. html-brief 스킬 설치

스킬 폴더를 심볼릭 링크로 건다. 그래야 `git pull`만으로 스킬도 함께 갱신된다.

```bash
mkdir -p ~/.claude/skills
ln -sfn "$REPO/skills/html-brief" ~/.claude/skills/html-brief
ls ~/.claude/skills/html-brief/SKILL.md      # 있어야 한다
```

- 특정 프로젝트에서만 쓰려면 그 프로젝트의 `.claude/skills/html-brief`에 같은 링크를 건다.
- 스킬이 그림을 찍을 때 Pillow를 쓴다. 각 스크립트가 uv 인라인 의존성으로 선언해 두었으므로 따로 설치할 것은 없다.
- 스크린샷 검사(`shot.py`)는 Chrome이나 Chromium이 있어야 한다. 없으면 그 단계만 건너뛴다.

새 스킬은 다음 Claude Code 세션부터 보인다.

## 6. (선택) Chrome 확장 — .html 더블클릭을 리더로

```bash
cd "$REPO" && uv run reader.py install-extension
```

이 명령은 `$REPO/reader_chrome_ext/` 폴더를 만든다. 확장 등록은 사용자에게 아래처럼 직접 하도록 안내한다.

1. Chrome 주소창에 `chrome://extensions`를 연다.
2. 오른쪽 위 **개발자 모드**를 켠다.
3. **압축해제된 확장 프로그램 로드**를 눌러 `$REPO/reader_chrome_ext`를 고른다.
4. 확장 **세부정보**에서 **파일 URL에 대한 액세스 허용**을 켠다.

macOS에서는 .html 기본 앱을 바꾸지 말라고 함께 알린다. macOS는 .html 기본 앱을 바꾸면 http 기본 브라우저까지 같이 바꿔 버린다.

## 7. 검증 (모두 통과해야 끝)

```bash
PORT=8770
# 1) 대시보드에 샘플 문서가 보인다
curl -s http://127.0.0.1:$PORT/ | grep -c "WIM Reader 샘플"
# 2) 문서에 리더 스크립트가 끼워진다
curl -s "http://127.0.0.1:$PORT/sample.html" | grep -c "/__reader/r.js"
# 3) 진행도 DB 조회가 된다
uv run "$REPO/reader.py" status
# 4) 개념 설명(빠른 경로)이 claude로 돈다 — 10~30초, 글자가 흘러나오면 통과
curl -sN -X POST http://127.0.0.1:$PORT/__reader/api/explain \
  -d '{"term":"스케줄러","ctx":"운영체제의 스케줄러는 어떤 작업을 먼저 CPU에 올릴지 정한다.","v":2,"k":"sample.html"}' | head -c 400; echo
# 5) 스킬이 보인다
test -f ~/.claude/skills/html-brief/SKILL.md && echo skill-ok
```

- 1·2가 0이면 서버의 `--root`가 `$DOCS`와 같은지 본다: `cat ~/.config/wim-reader/root`, 로그 `$DOCS/.wim_reader/server.log`.
- 4가 비거나 에러면 `claude -p "hi"`가 터미널에서 되는지 본다(로그인·PATH 문제).
  - 서버는 `~/.cache/wim_reader_explain`에서 claude를 돌린다.
  - launchd는 PATH가 짧다. 그래서 서버는 `shutil.which("claude")`가 실패하면 `~/.local/bin/claude`를 쓴다. claude가 다른 곳에 있으면 그 위치에 심볼릭 링크를 만든다.

## 8. 사용자에게 알려 줄 것 (그대로 전달)

- **문서 열기:** `$DOCS` 아래 HTML은 `http://127.0.0.1:8770/`에서 연다. 확장을 깔았으면 더블클릭으로도 열린다.
- **읽기 기록:** 스크롤 위치는 자동으로 남는다. 키는 `b` 책갈피, `m` 메모, `[` `]` 책갈피 이동, `d` 읽음 표시다. 오른쪽 아래 % 버튼을 누르면 패널이 열린다.
- **단어 설명:** 단어를 더블클릭하거나 드래그하면 `설명` 칩이 뜬다. 칩을 누르거나 `e`를 누르면 심층 카드가 열린다.
  - 카드는 4갈래 조사(원리·이 문서·웹 근거·실무) → 카드 쓰기 → 검증 순서로 만들어진다.
  - 새 카드는 2~3분이 걸리고 Claude 사용량이 든다. 같은 카드는 저장본이 바로 뜬다.
- **하단 탭:** `▾ 내리기`나 Esc를 누르면 카드가 하단 탭으로 내려가고, 그 사이에도 계속 만들어진다. 다 되면 알림이 뜬다. 탭을 누르면 다시 열린다(최근 12개).
- **질문·수정:** 문장을 고르고 `a`를 누르면 그 HTML을 만든 Claude Code 세션에 바로 묻거나(`질문`) 문서를 고치게(`HTML 수정`) 할 수 있다. 만든 세션은 `~/.claude/projects/` 세션 기록에서 그 폴더 파일을 고친 세션을 찾는다. 못 찾으면 문서 폴더에서 새 세션으로 묻는다.
- **카드 기록:** 왼쪽 아래 `카드` 버튼을 누르면 지금까지 만든 카드 전체 목록이 뜬다. 검색할 수 있고, 고르면 바로 다시 열린다.
- **HTML 만들기:** Claude Code에서 "이 주제 조사해서 html로 정리해줘"라고 하면 html-brief 스킬이 그림 위주 리서치 HTML을 만든다. 결과물을 `$DOCS`에 두면 리더로 읽을 수 있다.
- **폰·태블릿:** 같은 와이파이에서 `uv run reader.py serve --lan`을 실행하면 토큰이 붙은 접속 URL이 찍힌다.
- **조회 명령:** `uv run reader.py status`, `unread`, `recent`, `marks`, `explains`.
- **설명 대상 바꾸기:** 카드가 가정하는 독자는 환경변수 `WIM_READER_PERSONA`로 바꾼다. 예: `"대학원생"`. launchd를 쓰면 plist에 넣거나 셸에서 serve를 띄울 때 지정한다.

## 9. 갱신·제거

```bash
git -C ~/Developer/wim-reader pull && launchctl kickstart -k gui/$(id -u)/io.github.wim-reader   # macOS 갱신
uv run ~/Developer/wim-reader/reader.py uninstall-agent && rm ~/.claude/skills/html-brief        # 제거
```

- Linux는 `systemctl --user restart wim-reader`로 재시작한다.
- 데이터(읽은 기록·카드 캐시)는 `$DOCS/.wim_reader/`에 있다. 지우면 기록이 초기화된다.
