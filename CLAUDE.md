# wim-reader

로컬 HTML 리더 서버(`reader.py`) + 리서치 HTML 스킬(`skills/html-brief`).

- 사용자가 "셋업해줘/설치해줘"라고 하면 `SETUP_FOR_CLAUDE.md`를 처음부터 끝까지 따른다(검증 단계까지).
- 서버 코드는 `reader.py` 한 파일(표준 라이브러리만, `uv run`). UI JS는 `TRACKER_JS` 문자열로 들어 있다. 고친 뒤 macOS는 `launchctl kickstart -k gui/$(id -u)/io.github.wim-reader`.
- 사용자에게 답할 때는 한국어.
