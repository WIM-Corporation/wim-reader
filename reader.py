# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WIM Reader — 로컬 HTML 리더 서버. 폴더의 HTML을 열어 주면서 어디까지 읽었는지(스크롤 깊이)·책갈피·메모를 SQLite에 남기고,
단어를 고르면 Claude Code(claude -p)가 조사·검증한 개념 카드를 띄운다.

uv run reader.py serve [--root 폴더]        # http://127.0.0.1:8770 대시보드(이 컴퓨터만). 폴더 기본값: 설정 파일 → 현재 폴더
uv run reader.py serve --lan                # 같은 와이파이의 폰·태블릿·PC도(토큰 쿠키 필요, 접속 URL을 찍어 준다)
uv run reader.py status|unread [폴더]|recent [-n 10]|marks [질의] [--json]   # 사람·에이전트 조회
uv run reader.py open <html 경로>           # 서버 URL로 브라우저 열기
uv run reader.py install-agent [--lan] | uninstall-agent   # macOS 로그인 때 자동 실행(launchd), 문서 폴더를 설정 파일에 저장
uv run reader.py install-extension          # .html 더블클릭(file://) → Chrome 확장이 리더 주소로 넘긴다
uv run reader.py explains [질의] [--json]   # 문서에서 물어본 용어 설명 기록

- 문서 폴더(ROOT): --root > 환경변수 WIM_READER_ROOT > ~/.config/wim-reader/root > 현재 폴더
- 디스크의 HTML은 고치지 않는다. 응답할 때 /__reader/r.js를 끼워 넣는다
- 위치는 비율+텍스트 앵커(가까운 제목·문단 앞 40자)로 저장해 폭이 다른 폰에서도 같은 절로 돌아간다
- 문서 키는 ROOT 기준 NFC 상대경로. 파일이 옮겨져 사라지면 같은 파일명이 하나뿐일 때 키를 따라 옮긴다
- 단어를 더블클릭·드래그하면 '설명' 칩(또는 e) → 심층 개념 카드: claude -p(opus) 조사 4갈래(원리·이 문서·웹 근거·실무, 웹 갈래는 WebSearch·WebFetch) 병렬 →
  카드 쓰기(번호 절·핵심 숫자 카드·mermaid 2개·업계 사례·확인할 질문·출처, 초안이 흘러나옴) → 검증(고칠 문장 패치 + '검증' 절). 같은 질문은 DB 캐시.
  카드는 하단 탭으로 내려 두고 계속 읽을 수 있다(끝나면 알림). 후속 질문·'자세히'는 빠른 경로(도구 없음).
  claude는 ~/.cache/wim_reader_explain에서 프로젝트 설정 없이 돌린다(문서 폴더의 훅이 클릭마다 돌지 않게)
- ROOT 밖 HTML(다운로드 등)은 /__file/<절대경로>로 연다. 홈 폴더 아래만, 이 컴퓨터(루프백)에서만
- 데이터: <ROOT>/.wim_reader/ (reading.db·token·server.log). 점 폴더·secrets/·node_modules는 내보내지 않는다
"""
import argparse, hashlib, hmac, html, json, mimetypes, os, plistlib, re, secrets as pysecrets, shutil, sqlite3, subprocess, sys, threading, time, unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen

CONF = Path.home() / ".config/wim-reader/root"


def _root():
    if "--root" in sys.argv:
        i = sys.argv.index("--root")
        v = sys.argv[i + 1]; del sys.argv[i:i + 2]
        return Path(v).expanduser().resolve()
    if os.environ.get("WIM_READER_ROOT"):
        return Path(os.environ["WIM_READER_ROOT"]).expanduser().resolve()
    if CONF.exists() and CONF.read_text().strip():
        return Path(CONF.read_text().strip()).expanduser().resolve()
    return Path.cwd().resolve()


def nfc(s):
    return unicodedata.normalize("NFC", s)


def htmls(folder):
    out = []
    for d, dirs, fs in os.walk(folder):
        dirs[:] = [x for x in dirs if not x.startswith(("_", ".")) and x not in BLOCK]
        out += [Path(d) / f for f in fs if f.endswith((".html", ".htm"))]
    names = {p.name for p in out}
    keep = [p for p in out if p.name.replace(".html", "_local.html") not in names]  # _local 판이 있으면 원판은 뺀다
    return sorted(keep, key=lambda p: (len(p.relative_to(folder).parts), p.name))


def title(p):
    try:
        with open(p, errors="ignore") as fh:
            head = fh.read(20000)
    except OSError:
        return ""
    m = re.search(r"<title>(.*?)</title>", head, re.S) or re.search(r"<h1[^>]*>(.*?)</h1>", head, re.S)
    return nfc(html.unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(1))).strip()) if m else ""


ROOT = _root()
DATA = ROOT / ".wim_reader"
DB = DATA / "reading.db"
TOKEN = DATA / "token"
PORT = 8770
BLOCK = {"secrets", "node_modules"}
LOOPBACK = {"127.0.0.1", "::1"}
LABEL = "io.github.wim-reader"
AGENT = Path.home() / f"Library/LaunchAgents/{LABEL}.plist"
HOME = Path.home()
UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
CLAUDE = shutil.which("claude") or str(HOME / ".local/bin/claude")
EXPLAIN_CWD = HOME / ".cache/wim_reader_explain"
MODELS = {"fast": "opus", "deep": "opus"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(key TEXT PRIMARY KEY, title TEXT, cur_ratio REAL DEFAULT 0, cur_anchor TEXT,
  max_ratio REAL DEFAULT 0, opens INTEGER DEFAULT 0, active_sec INTEGER DEFAULT 0, last_open REAL,
  done_at REAL, done_off INTEGER DEFAULT 0, last_device TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS marks(id INTEGER PRIMARY KEY, key TEXT, ratio REAL, anchor TEXT, note TEXT,
  device TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS visits(id INTEGER PRIMARY KEY, key TEXT, device TEXT, opened_at REAL,
  active_sec INTEGER DEFAULT 0, max_ratio REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS explains(id INTEGER PRIMARY KEY, key TEXT, term TEXT, q TEXT, ctx_hash TEXT, answer TEXT,
  model TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS blocks(key TEXT, h TEXT, first_seen REAL, read_at REAL, PRIMARY KEY(key, h));
CREATE INDEX IF NOT EXISTS marks_key ON marks(key);
CREATE INDEX IF NOT EXISTS explains_hash ON explains(ctx_hash);
CREATE INDEX IF NOT EXISTS visits_key ON visits(key);
"""
LOCK = threading.Lock()


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    have = {r[1] for r in c.execute("PRAGMA table_info(docs)")}
    for col, typ in (("content_hash", "TEXT"), ("hash_mtime", "REAL"), ("revised_at", "REAL"), ("creator_session", "TEXT"), ("creator_cwd", "TEXT"),
                     ("creator_n", "INTEGER"), ("creator_mtime", "REAL"), ("ask_session", "TEXT"),
                     ("blk_total", "INTEGER"), ("blk_read", "INTEGER"), ("blk_new", "INTEGER"), ("read_pct", "REAL")):
        if col not in have:
            c.execute(f"ALTER TABLE docs ADD COLUMN {col} {typ}")
    return c


def revise(c, keys=None):
    """파일 내용(sha1)이 지난번과 달라졌으면 읽음 표시·진행도를 지운다. 위치 앵커·책갈피는 남긴다.
    mtime은 build.py가 매번 바꾸므로 해시를 다시 잴지 고르는 데만 쓴다. 바뀐 키 목록을 돌려준다."""
    rows = c.execute("SELECT key, content_hash, hash_mtime FROM docs" + (f" WHERE key IN ({','.join('?' * len(keys))})" if keys else ""),
                     list(keys or ())).fetchall()
    changed = []
    for r in rows:
        p = resolve(r["key"])[0]
        if p is None:
            continue
        try:
            m = p.stat().st_mtime
            if r["hash_mtime"] == m:
                continue
            h = hashlib.sha1(p.read_bytes()).hexdigest()
        except OSError:
            continue
        with LOCK, c:
            if r["content_hash"] and r["content_hash"] != h:
                c.execute("""UPDATE docs SET done_at=NULL, done_off=0, max_ratio=0, revised_at=?,
                             content_hash=?, hash_mtime=? WHERE key=?""", (time.time(), h, m, r["key"]))
                changed.append(r["key"])
            else:
                c.execute("UPDATE docs SET content_hash=?, hash_mtime=? WHERE key=?", (h, m, r["key"]))
    return changed


# ---------- 경로 ----------

def resolve(urlpath):
    """URL 경로 → (실제 Path, NFC 키). 막힌 경로나 없는 파일은 (None, 키). __file/로 시작하면 홈 폴더 아래 절대경로."""
    key = nfc(unquote(urlpath)).strip("/")
    parts = [p for p in key.split("/") if p]
    if any(p == ".." or p.startswith(".") for p in parts) or (parts and parts[0] in BLOCK):
        return None, key
    base = ROOT
    if parts and parts[0] == "__file":
        base, parts = Path("/"), parts[1:]
    cur = base
    for p in parts:
        cand = cur / p
        if not cand.exists():  # 디스크 이름이 NFD일 수 있다
            try:
                cand = next((x for x in cur.iterdir() if nfc(x.name) == p), None)
            except OSError:
                cand = None
            if cand is None:
                return None, key
        cur = cand
    try:
        cur.resolve().relative_to(ROOT if base == ROOT else HOME)
    except ValueError:
        return None, key
    return cur, key


def key_of(p):
    return nfc(str(Path(p).resolve().relative_to(ROOT)))


def all_html():
    for d, dirs, fs in os.walk(ROOT):
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in BLOCK]
        for f in fs:
            if f.endswith((".html", ".htm")):
                yield Path(d) / f


def migrate():
    """사라진 키의 파일이 다른 곳에 같은 이름으로 하나만 있으면 키를 옮긴다(폴더 정리 대응)."""
    c = db()
    gone = [r["key"] for r in c.execute("SELECT key FROM docs") if not r["key"].startswith("__file/") and resolve(r["key"])[0] is None]
    if not gone:
        return []
    by_name = {}
    for p in all_html():
        by_name.setdefault(nfc(p.name), []).append(key_of(p))
    moved = []
    with c:
        for k in gone:
            cands = by_name.get(k.rsplit("/", 1)[-1], [])
            if len(cands) == 1 and not c.execute("SELECT 1 FROM docs WHERE key=?", (cands[0],)).fetchone():
                for t in ("docs", "marks", "visits"):
                    c.execute(f"UPDATE {t} SET key=? WHERE key=?", (cands[0], k))
                moved.append((k, cands[0]))
    return moved


_titles = {}


def cached_title(p):
    m = p.stat().st_mtime
    if _titles.get(p, (None,))[0] != m:
        _titles[p] = (m, title(p) or nfc(p.stem))
    return _titles[p][1]


def catalog():
    """대시보드 목록: ROOT 아래 HTML 전부(점·밑줄 폴더 제외). 맨 위 폴더별로 묶는다."""
    out = []
    for h in htmls(ROOT):
        rel = h.relative_to(ROOT).parts
        folder = nfc(rel[0]) if len(rel) > 1 else "(맨 위)"
        out.append({"k": key_of(h), "title": cached_title(h), "group": folder, "folder": nfc("/".join(rel[:-1])) or "/", "mtime": h.stat().st_mtime})
    return out


def merged(c=None):
    c = c or db()
    revise(c)
    docs = {r["key"]: dict(r) for r in c.execute("SELECT * FROM docs")}
    nmarks = {r[0]: r[1] for r in c.execute("SELECT key, count(*) FROM marks GROUP BY key")}
    items, seen = [], set()
    for it in catalog():
        seen.add(it["k"])
        items.append(it)
    for k, d in docs.items():  # 색인 밖이라도 한 번 연 문서
        if k not in seen and resolve(k)[0] is not None:
            p = resolve(k)[0]
            items.append({"k": k, "title": d["title"] or nfc(p.stem), "group": "그 밖에 연 문서", "folder": nfc(p.parent.name), "mtime": p.stat().st_mtime})
    for it in items:
        d = docs.get(it["k"], {})
        it.update({f: d.get(f) for f in ("cur_ratio", "max_ratio", "opens", "active_sec", "last_open", "done_at", "last_device", "revised_at",
                                         "blk_total", "blk_read", "blk_new", "read_pct")})
        it["prog"] = it["read_pct"] if it["read_pct"] is not None else (it["max_ratio"] or 0)  # 덩어리 단위 읽음이 있으면 그것, 없으면 옛 스크롤 깊이
        it["marks"] = nmarks.get(it["k"], 0)
        it["revised"] = bool(it["revised_at"] and it["revised_at"] > (it["last_open"] or 0))
        it["state"] = "done" if it["done_at"] else ("reading" if it["prog"] >= 0.02 else "unread")
    return items


# ---------- API ----------

def doc_payload(c, k, blocks=False):
    d = c.execute("SELECT * FROM docs WHERE key=?", (k,)).fetchone()
    marks = [dict(r) for r in c.execute("SELECT * FROM marks WHERE key=? ORDER BY ratio", (k,))]
    out = {"doc": dict(d) if d else None, "marks": marks}
    if blocks:  # 페이지를 열 때만: 이 문서에서 본 적 있는 덩어리(h)와 읽음 여부. 처음 본 때가 첫 방문보다 2분 넘게 늦으면 '새로 바뀐 곳'
        rows = c.execute("SELECT h, first_seen, read_at FROM blocks WHERE key=?", (k,)).fetchall()
        base = min((r["first_seen"] for r in rows), default=None)
        out["blocks"] = {"base": base, "read": [r["h"] for r in rows if r["read_at"]],
                         "fresh": [r["h"] for r in rows if not r["read_at"] and base and r["first_seen"] > base + 120],
                         "known": [r["h"] for r in rows if not r["read_at"]]}
    return out


def save_doc(c, b):
    k, now = b["k"], time.time()
    ratio = max(0.0, min(1.0, float(b.get("ratio") or 0)))
    dev = (b.get("device") or "")[:40]
    act = max(0, min(int(b.get("active") or 0), 600))
    anchor = json.dumps(b["anchor"], ensure_ascii=False) if b.get("anchor") else None
    with LOCK, c:
        c.execute("INSERT OR IGNORE INTO docs(key, updated_at) VALUES(?, ?)", (k, now))
        if b.get("title"):
            c.execute("UPDATE docs SET title=? WHERE key=?", (b["title"][:200], k))
        c.execute("""UPDATE docs SET cur_ratio=?, cur_anchor=coalesce(?, cur_anchor), max_ratio=max(max_ratio, ?),
                     active_sec=active_sec+?, last_device=?, updated_at=? WHERE key=?""", (ratio, anchor, ratio, act, dev, now, k))
        vid = b.get("visit")
        if b.get("open"):
            c.execute("UPDATE docs SET opens=opens+1, last_open=? WHERE key=?", (now, k))
            vid = c.execute("INSERT INTO visits(key, device, opened_at, max_ratio) VALUES(?,?,?,?)", (k, dev, now, ratio)).lastrowid
        elif vid:
            c.execute("UPDATE visits SET active_sec=active_sec+?, max_ratio=max(max_ratio, ?) WHERE id=? AND key=?", (act, ratio, vid, k))
        # 덩어리 단위 읽음: seen = 처음 화면에 나타난 덩어리, reads = 충분히 머물러 읽음으로 확정된 덩어리
        c.executemany("INSERT OR IGNORE INTO blocks(key, h, first_seen) VALUES(?,?,?)", [(k, str(h)[:40], now) for h in (b.get("seen") or [])[:20000]])
        c.executemany("INSERT INTO blocks(key, h, first_seen, read_at) VALUES(?,?,?,?) ON CONFLICT(key, h) DO UPDATE SET read_at=coalesce(read_at, excluded.read_at)",
                      [(k, str(h)[:40], now, now) for h in (b.get("reads") or [])[:20000]])
        if b.get("acks"):  # 노랑(바뀐 곳)을 지나간 덩어리: 처음 본 때를 첫 방문으로 돌려 더는 '바뀐 곳'이 아니게
            base = c.execute("SELECT min(first_seen) FROM blocks WHERE key=?", (k,)).fetchone()[0]
            c.executemany("UPDATE blocks SET first_seen=? WHERE key=? AND h=?", [(base, k, str(h)[:40]) for h in b["acks"][:20000]])
        cov = b.get("cov")
        if isinstance(cov, dict):
            c.execute("UPDATE docs SET blk_total=?, blk_read=?, blk_new=?, read_pct=? WHERE key=?",
                      (int(cov.get("total") or 0), int(cov.get("read") or 0), int(cov.get("new") or 0), max(0.0, min(1.0, float(cov.get("pct") or 0))), k))
        # 자동 완료: 덩어리 기록이 있으면 97% 넘게 읽었을 때, 없으면 끝까지 내려가 15초 넘게 봤을 때(사용자가 되돌렸으면 다시 안 건다)
        c.execute("""UPDATE docs SET done_at=? WHERE key=? AND done_at IS NULL AND done_off=0 AND
                     (CASE WHEN read_pct IS NOT NULL THEN read_pct>=0.97 ELSE max_ratio>=0.95 AND active_sec>=15 END)""", (now, k))
    return {"visit": vid, **doc_payload(c, k)}


def save_mark(c, b):
    with LOCK, c:
        if b.get("del"):
            c.execute("DELETE FROM marks WHERE id=?", (int(b["del"]),))
        elif b.get("id"):
            c.execute("UPDATE marks SET note=? WHERE id=?", ((b.get("note") or "")[:2000], int(b["id"])))
        else:
            c.execute("INSERT INTO marks(key, ratio, anchor, note, device, created_at) VALUES(?,?,?,?,?,?)",
                      (b["k"], float(b.get("ratio") or 0), json.dumps(b.get("anchor"), ensure_ascii=False),
                       (b.get("note") or "")[:2000], (b.get("device") or "")[:40], time.time()))
    k = b.get("k") or (c.execute("SELECT key FROM marks WHERE id=?", (int(b.get("id") or 0),)).fetchone() or [""])[0]
    return doc_payload(c, k)


def save_done(c, b):
    k = b["k"]
    with LOCK, c:
        c.execute("INSERT OR IGNORE INTO docs(key, updated_at) VALUES(?, ?)", (k, time.time()))
        if b.get("done"):
            c.execute("UPDATE docs SET done_at=?, done_off=0 WHERE key=?", (time.time(), k))
        else:
            c.execute("UPDATE docs SET done_at=NULL, done_off=1 WHERE key=?", (k,))
    return doc_payload(c, k)


EXPLAIN_SYS = """너는 어려운 기술 문서를 처음 보는 사람 옆에 앉아 차근차근 풀어 주는 선생님이다.
읽는 사람은 __READER__이다. 똑똑하지만 반도체·컴퓨터 구조·AI 모델을 배운 적이 없다. 중학생에게 설명하듯 바닥부터 쌓아 올린다.
사용자가 문서에서 고른 용어(또는 구절)를, 그 문장이 쓰인 맥락에 맞는 뜻으로 설명하는 '개념 카드'를 한국어 마크다운으로 쓴다.

쉽게 쓰는 규칙(가장 중요):
- 한 문장은 짧게(50자 안팎). 한 문장에 새 개념은 하나만.
- 전문용어를 쓰지 않는다. 꼭 써야 하면 처음 나올 때 바로 쉬운 말로 풀어 쓴다. 약어를 영어 원말로 펼치는 것(TOPS=Tera Operations Per Second)은 설명이 아니다. 그게 실제로 무엇을 세는지 말한다.
- 고른 용어를 설명하려고 또 다른 어려운 용어를 끌어오지 않는다. 문맥의 다른 어려운 단어는 무시하거나 한마디로 풀고 넘어간다.
- 추상적인 말 대신 눈에 그려지는 것(사람·물건·숫자)으로 말한다. 숫자는 작고 쉬운 예로 든다.
- '~입니다/~합니다' 존댓말. 친절하지만 군더더기 없이.

반드시 아래 순서와 형식을 지킨다. 머리말·맺음말 없이 첫 줄부터 시작한다.

IMG: 그림이 실려 있을 영어 위키백과 문서 제목 1~3개를 ' | '로 구분 (예: IMG: Instruction set architecture | Reduced instruction set computer)
## 한마디로
전문용어 없이 1~2문장. 핵심어는 **굵게**.
## 차근차근
1. 번호 목록 4~6단계. 아무것도 모르는 상태에서 시작해 한 단계씩 쌓아 이 개념에 도착한다. 각 단계 1~2문장.
## 그림으로 보기
```mermaid
(위 단계를 눈으로 보여 주는 다이어그램 하나. 라벨도 쉬운 말)
```
그림 읽는 법 1~2문장(왼쪽에서 오른쪽으로 무엇이 무엇이 되는지).
## 예를 들면
숫자가 들어간 구체적인 예 하나. 일상이나 현장 상황으로 3~5문장.
## 이 문장 다시 읽기
고른 용어가 들어간 원래 문장을 쉬운 말로 다시 풀어 쓴다. '이 문장은 결국 ~라는 말입니다.'로 시작해 2~3문장.
## 왜 알아야 하나
읽는 사람이 제품·기술을 고르거나 다른 사람과 이야기할 때 이 개념이 왜 중요한지 1~2문장.
## 헷갈리기 쉬운 것
비슷해서 헷갈리는 개념이 있을 때만. 마크다운 표 3~4행(머리줄 포함), 칸 안도 쉬운 말.
## 같이 볼 용어
- 용어: 쉬운 말 한 줄 설명
(3개)

mermaid 규칙: flowchart TB(위→아래)를 기본으로 쓴다. 가로로 4칸 넘게 이어지면 카드 폭에 맞춰 줄어 글자가 작아지므로, 가로 한 줄은 3칸까지. 노드 4~9개(너무 복잡하게 그리지 않는다). 노드는 A["쉬운 한국어 라벨"] 꼴로 라벨을 항상 큰따옴표 안에 쓴다. 묶음은 subgraph X["라벨"] ... end.
화살표 -->, 화살표 라벨은 -->|짧은 말|. 라벨 안에 큰따옴표·세미콜론·HTML 태그를 쓰지 않는다.
LaTeX 금지. 모르면 모른다고 한다.
추가 질문이 오면 IMG 줄과 위 형식 없이 '## (질문 요지)' 제목 하나 + 짧은 문장 4~8개로 그 질문에만 같은 쉬운 말로 답하고, 그림이 도움이 될 때만 mermaid 하나를 넣는다."""


def explain_prompt(b):
    parts = [f"문서: {b.get('title', '')[:200]}", f"절 제목: {b.get('head', '')[:200]}",
             f"문장(맥락): {b.get('ctx', '')[:1500]}", f"고른 용어: {b['term'][:80]}"]
    if b.get("mode") == "deep":
        parts.append("이번엔 더 자세히: '## 차근차근'을 7~9단계로 늘리고, '## 예를 들면' 다음에 '## 속에서 실제로 일어나는 일'(쉬운 말로, 숫자 예 포함) 절을 더한다. 여전히 쉬운 말 규칙을 지킨다.")
    if b.get("q"):
        parts += [f"앞서 한 설명: {b.get('prev', '')[:3000]}", f"추가 질문: {b['q'][:500]}"]
    return "\n".join(parts)


def explain_hash(b, model):
    raw = json.dumps([b["term"][:80], b.get("ctx", "")[:1500], b.get("q", "")[:500], b.get("prev", "")[:3000], model, b.get("mode") == "deep", "card3"], ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()


def explain_run(prompt, model, write, system=None, tools="", timeout=180):
    """claude -p를 문서 폴더 밖에서 프로젝트 설정 없이 돌려 글자가 나오는 대로 write에 넘긴다. 다 모은 답을 돌려준다.
    tools: 쉼표로 이은 허용 도구(예: "WebSearch,WebFetch"). 빈 문자열이면 도구 없음."""
    EXPLAIN_CWD.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))}
    proc = subprocess.Popen([CLAUDE, "-p", "--model", model, "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                             "--tools", tools, *(["--allowedTools", tools] if tools else []),
                             "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
                             "--disable-slash-commands", "--system-prompt", system or EXPLAIN_SYS, prompt],
                            cwd=EXPLAIN_CWD, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    out, err = [], ""
    try:
        for ln in proc.stdout:
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            e = ev.get("event") or {}
            if ev.get("type") == "stream_event" and e.get("type") == "content_block_delta" and e["delta"].get("type") == "text_delta":
                out.append(e["delta"]["text"])
                write(e["delta"]["text"])
            elif ev.get("type") == "result" and ev.get("is_error"):
                err = str(ev.get("result") or "오류")
        proc.wait()
        if not out:
            err = err or (proc.stderr.read() or "").strip()[-300:] or f"claude 종료 코드 {proc.returncode}"
            write(f"\n[설명 실패] {err}")
    finally:
        timer.cancel()
        if proc.poll() is None:  # 브라우저가 끊고 나갔다
            proc.kill()
    return "".join(out) if out and not err else None


# ---------- 심층 카드(v3): 갈래 4개 병렬 조사 → 수렴(카드 쓰기) → 검증(고칠 문장 패치 + 검증 절) ----------
DEEP_MODEL = "opus"
LANES = [
    ("base", "원리", "", "고른 용어의 원리를 바닥부터 조사한다. 정의, 무엇이 무엇과 어떻게 연결되는지, 속에서 실제로 일어나는 순서(시간 순), 대표 수치(단위 포함)를 정리한다."),
    ("doc", "이 문서", "", "주어진 문서 본문 안에서 이 용어가 무슨 뜻으로, 어떤 숫자·비교·결론과 함께 쓰였는지만 정리한다. 문서의 숫자는 원문 표기 그대로 옮기고, 문서에 없는 것은 쓰지 않는다. 문서가 답하지 않는 질문도 적는다."),
    ("web", "웹 근거", "WebSearch,WebFetch", "웹을 검색해 공식 문서·표준·소스코드·논문·제조사 자료에서 이 용어(문서 맥락의 뜻)에 대한 근거와 수치, 실제 제품·업계 사례를 찾는다. 주장마다 출처 URL을 붙인다. 5~8개 출처."),
    ("prac", "실무·함정", "WebSearch,WebFetch", "현장에서 이 개념 때문에 생기는 문제, 흔한 오해, 비슷한 개념과의 차이, 설정을 확인하는 방법, 엔지니어·고객에게 물어볼 질문을 조사한다. 필요하면 웹으로 확인하고 출처 URL을 붙인다."),
]
LANE_SYS = """너는 기술 조사원이다. 결과는 다른 사람이 '개념 카드'를 쓸 때 쓰는 조사 메모다.
한국어 마크다운 메모로 쓴다(머리말 없이 바로). 3,000자 이내. 짧은 불릿 위주, 숫자는 단위와 함께.
확인한 사실은 끝에 (출처: URL) 또는 (출처: 문서)를 붙인다. 확인 못 한 것은 ★를 붙인다. 지어내지 않는다. 표의 | 구분자는 쓰지 않는다."""
CARD_SYS = """너는 어려운 기술 개념을 처음 보는 사람 옆에서 풀어 주는 선생님이자 편집자다.
읽는 사람은 __READER__이다. 똑똑하지만 운영체제·반도체·제어·AI를 배운 적이 없다.
조사원 4명이 쓴 메모와 문서 본문을 받아, 고른 용어를 그 문서 맥락의 뜻으로 설명하는 '심층 개념 카드'를 한국어 마크다운으로 쓴다.
단편적인 한 장이 아니라, 이 카드 하나로 개념을 바닥부터 문서의 결론까지 따라갈 수 있어야 한다.

쉽게 쓰는 규칙:
- 한 문장은 짧게(60자 안팎), 한 문장에 새 개념 하나. '~입니다/~합니다' 존댓말.
- 전문용어는 처음 나올 때 바로 쉬운 말로 푼다. 약어를 영어로 펼치는 것은 설명이 아니다.
- 추상어 대신 사람·물건·숫자로 말한다.
- 숫자·사실은 메모와 문서에 있는 것만 쓴다. 메모에서 ★인 것은 문장 끝에 ★를 붙인다. 출처 없는 숫자를 만들지 않는다.

형식(이 순서, 머리말·맺음말 없이 첫 줄부터). 각 '## ' 절 바로 아래 첫 문장은 그 절의 요점 한 문장(굵게 쓰지 않는다)이다.

IMG: 그림이 있을 영어 위키백과 문서 제목 1~3개를 ' | '로 구분
## 한마디로
전문용어 없이 2문장. 핵심어는 **굵게**.
```cards
핵심 숫자나 사실 :: 짧은 이름 :: 한 줄 설명 :: green
(3~4줄. 색은 green·blue·amber·red·gray 중 하나. 값은 문서·메모에 있는 것만)
```
## 차근차근
요점 한 문장.
1. 아무것도 모르는 상태에서 시작해 이 개념에 닿는 6~8단계. 단계마다 1~2문장.
## 구조 그림
요점 한 문장.
```mermaid
(무엇이 무엇과 연결되는지 보이는 구조도)
```
그림 읽는 법 1~2문장.
## 속에서 일어나는 일
요점 한 문장.
```mermaid
(시간 순서: sequenceDiagram 또는 위→아래 흐름. 실제 수치가 있으면 라벨에)
```
수치가 들어간 설명 3~5문장.
## 예를 들면
숫자가 들어간 구체적인 장면 하나(현장·실무). 4~6문장.
## 이 문서에서는
요점 한 문장. 그다음 문서의 숫자·비교를 그대로 인용하며 이 용어가 문서의 결론에 어떻게 이어지는지 3~6문장. 고른 문장을 쉬운 말로 다시 풀어 쓴다.
## 업계에서는
요점 한 문장. 웹 메모의 실제 제품·표준·사례 3~5개를 불릿으로, 각 줄 끝에 출처 링크 [사이트](URL).
## 헷갈리기 쉬운 것
마크다운 표(머리줄 포함 4~5행, 3열). 칸 안도 쉬운 말.
## 확인할 질문
엔지니어나 고객에게 물어볼 질문 4~5개를 번호 목록으로. 각 질문 뒤에 '— 왜: ...' 한 줄.
## 같이 볼 용어
- 용어: 쉬운 말 한 줄 설명
(4~6개)
## 출처
- [제목](URL)
(메모에 나온 URL만. 문서 근거는 '- 이 문서: 절 제목')

mermaid 규칙: flowchart는 TB, 가로 한 줄은 3칸까지, 노드 4~10개. 노드는 A["쉬운 한국어 라벨"] 꼴로 라벨을 큰따옴표 안에. 묶음은 subgraph X["라벨"] ... end.
화살표 -->, 라벨은 -->|짧은 말|. sequenceDiagram이면 participant A as 라벨. 라벨 안에 큰따옴표·세미콜론·HTML 태그·괄호 짝 안 맞는 것 금지.
LaTeX 금지. 표 밖에서는 | 문자를 쓰지 않는다."""
READER = os.environ.get("WIM_READER_PERSONA", "기술 문서를 처음 읽는 실무자(제품·사업 담당)")
EXPLAIN_SYS, CARD_SYS = EXPLAIN_SYS.replace("__READER__", READER), CARD_SYS.replace("__READER__", READER)

VERIFY_SYS = """너는 사실 검증 담당이다. 개념 카드 초안을 조사 메모·문서 본문과 대조해 틀리거나 근거가 없는 문장을 찾는다.
JSON 하나만 출력한다(코드 블록 없이). 형식:
{"checks":[{"claim":"검사한 주장 요약","verdict":"ok|fix|unsure","find":"카드 안의 정확한 부분 문자열(fix·unsure일 때)","replace":"고친 문장(fix일 때)","why":"근거 한 줄(출처 URL 또는 '문서'·'메모')"}]}
규칙: 숫자·버전·수치 비교·인과 주장 위주로 8~14개 검사한다. ok도 포함한다. find는 카드에 그대로 한 번만 나오는 짧은 구절(10~60자)이어야 한다.
메모·문서로 뒷받침되지 않지만 틀렸다고 단정할 수 없으면 unsure. 틀린 것이 확실하면 fix와 고친 문장. 쉬운 말 문체는 유지한다."""


def deep_hash(b):
    sess = bool(b.get("k") and creator(b["k"]))
    raw = json.dumps([b["term"][:80], b.get("ctx", "")[:1500], (b.get("doc") or "")[:200], DEEP_MODEL, "deep5s" if sess else "deep4"], ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()


def deep_base(b):
    return "\n".join([f"문서 제목: {b.get('title', '')[:200]}", f"절 제목: {b.get('head', '')[:200]}",
                      f"고른 용어: {b['term'][:80]}", f"고른 문장(맥락): {b.get('ctx', '')[:1500]}"])


def apply_checks(card, raw):
    """검증 JSON을 적용한다. find가 카드에 한 번만 있을 때만 고치고, 모든 검사를 '## 검증' 절로 붙인다."""
    try:
        j = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        checks = [c for c in j.get("checks", []) if isinstance(c, dict)][:16]
    except (ValueError, AttributeError):
        return card + "\n## 검증\n검증 결과를 읽지 못했습니다. 이 카드는 검증되지 않았습니다 ★\n", []
    lines, fixed = [], 0
    for c in checks:
        v, f, r = c.get("verdict"), (c.get("find") or "").strip(), (c.get("replace") or "").strip()
        why = (c.get("why") or "").replace("|", "/").strip()
        claim = (c.get("claim") or f).replace("|", "/").strip()
        if v == "fix" and f and r and card.count(f) == 1:
            card = card.replace(f, r); fixed += 1
            lines.append(f"- 수정됨: {claim} — {why}")
        elif v == "fix":
            lines.append(f"- 확인 필요 ★: {claim} — {why} (자동 수정 못 함)")
        elif v == "unsure":
            if f and card.count(f) == 1 and "★" not in f:
                card = card.replace(f, f + " ★")
            lines.append(f"- 근거 약함 ★: {claim} — {why}")
        else:
            lines.append(f"- 확인됨 ✓: {claim} — {why}")
    head = f"검사 {len(checks)}건 · 수정 {fixed}건 · ★ {sum(1 for c in checks if c.get('verdict') != 'ok') - fixed}건"
    return card.rstrip() + "\n## 검증\n" + head + "\n" + "\n".join(lines) + "\n", checks


def deep_card(b, emit):
    """갈래 조사 → 카드 → 검증. emit(dict)로 진행을 보낸다. 최종 카드 문자열을 돌려준다(실패면 None)."""
    base, doc = deep_base(b), (b.get("doc") or "")[:7000]
    notes, t0 = {}, time.time()

    def lane(key, label, tools, task):
        emit({"e": "lane", "id": key, "label": label, "st": "run"})
        n, last = [0], [0.0]

        def w(t):
            n[0] += len(t)
            if time.time() - last[0] > 0.8:
                last[0] = time.time(); emit({"e": "lane", "id": key, "st": "run", "n": n[0]})
        prompt = f"{base}\n\n문서 본문(이 용어가 나온 절 주변):\n{doc}\n\n이번 갈래: {label}\n할 일: {task}" if key in ("doc", "base") \
            else f"{base}\n\n이번 갈래: {label}\n할 일: {task}"
        try:
            info = creator(b.get("k", "")) if key == "doc" and b.get("k") else None
            if info:
                emit({"e": "lane", "id": key, "label": "이 문서(만든 세션)", "st": "run"})
                _, out, er = session_run(["--resume", info["sid"], "--fork-session", "--no-session-persistence", "--tools", "Read,Grep,Glob",
                                          "--setting-sources", "", "--permission-mode", "bypassPermissions", "--append-system-prompt", LANE_SYS],
                                         f"{prompt}\n\n이 문서를 네가 만들었다. 만들 때 쓴 근거 자료(_raw 등)와 판단 이유까지 조사 메모에 넣는다. 파일은 고치지 않는다.",
                                         info["cwd"] or str(ROOT), w, timeout=300)
                out = out if out and not er else explain_run(prompt, DEEP_MODEL, w, system=LANE_SYS, tools=tools, timeout=300)
            else:
                out = explain_run(prompt, DEEP_MODEL, w, system=LANE_SYS, tools=tools, timeout=300)
        except Exception as e:  # noqa: BLE001 — 갈래 하나가 죽어도 나머지로 간다
            out = None; emit({"e": "lane", "id": key, "st": "fail", "msg": str(e)[:200]})
        if out:
            notes[key] = out
            emit({"e": "lane", "id": key, "st": "done", "n": len(out), "src": out.count("http"), "sec": round(time.time() - t0)})
        else:
            emit({"e": "lane", "id": key, "st": "fail", "sec": round(time.time() - t0)})
    th = [threading.Thread(target=lane, args=L, daemon=True) for L in LANES]
    for x in th: x.start()
    for x in th: x.join()
    if not notes:
        emit({"e": "err", "msg": "조사 갈래가 모두 실패했습니다"}); return None
    memo = "\n\n".join(f"### 조사 메모: {lab}\n{notes[k]}" for k, lab, _, _ in LANES if k in notes)
    miss = [lab for k, lab, _, _ in LANES if k not in notes]
    emit({"e": "stage", "st": "write", "sec": round(time.time() - t0)})
    syn = f"{base}\n\n문서 본문:\n{doc}\n\n{memo}" + (f"\n\n(실패한 조사 갈래: {', '.join(miss)})" if miss else "")
    info = creator(b.get("k", "")) if b.get("k") else None
    card = None
    if info:  # 이 문서를 만든 세션이 카드를 쓴다: 만들 때 본 근거·숫자·판단 맥락이 카드에 들어간다
        emit({"e": "stage", "st": "write", "who": "session", "sec": round(time.time() - t0)})
        _, card, er = session_run(["--resume", info["sid"], "--fork-session", "--no-session-persistence", "--tools", "Read,Grep,Glob",
                                   "--setting-sources", "", "--permission-mode", "bypassPermissions", "--append-system-prompt", CARD_SYS],
                                  syn + "\n\n이 문서는 네가 만들었다. 만들 때 쓴 근거 자료(_raw·출처·실측값)와 판단 이유를 살려 카드를 충실하게 쓴다. "
                                  "필요하면 파일을 읽어 확인한다. 도구를 쓰기 전에는 아무 글도 쓰지 말고, 출력은 카드 본문만(IMG: 줄부터).",
                                  info["cwd"] or str(ROOT), lambda t: emit({"e": "syn", "d": t}), timeout=600)
        if card and not er:
            m = re.search(r"^(IMG:|#{1,3} )", card, re.M)
            card = card[m.start():] if m else card
        else:
            card = None
            emit({"e": "syn", "d": "\n\n(만든 세션이 카드를 못 써서 새로 씁니다)\n\n"})
    if not card:
        card = explain_run(syn, DEEP_MODEL, lambda t: emit({"e": "syn", "d": t}), system=CARD_SYS, timeout=480)
    if not card:
        emit({"e": "err", "msg": "카드 쓰기에 실패했습니다"}); return None
    emit({"e": "stage", "st": "verify", "sec": round(time.time() - t0)})
    vr = explain_run(f"{base}\n\n문서 본문:\n{doc}\n\n{memo}\n\n### 검증할 카드 초안\n{card}", DEEP_MODEL, lambda t: None,
                     system=VERIFY_SYS, timeout=300) or ""
    final, checks = apply_checks(card, vr)
    if miss:
        final += f"- 조사 갈래 실패: {', '.join(miss)}\n"
    emit({"e": "final", "card": final, "sec": round(time.time() - t0),
          "stat": {"lanes": len(notes), "fixed": sum(1 for c in checks if c.get('verdict') == 'fix'), "checks": len(checks)}})
    return final


WIKI_UA = "wim-reader/1.0 (+https://github.com/WIM-Corporation/wim-reader)"
WIKI_CACHE = {}
WIKI_SKIP = ("icon", "logo", "question_book", "ambox", "edit-clear", "padlock", "symbol_", "wiktionary", "commons-")


def wiki_images(titles):
    """영어 위키백과 문서에 실린 그림(캡션 포함)을 모은다. 문서가 없으면 건너뛴다."""
    out, seen = [], set()
    for t in titles[:3]:
        t = t.strip().replace(" ", "_")
        if not t:
            continue
        if t not in WIKI_CACHE:
            try:
                req = Request(f"https://en.wikipedia.org/api/rest_v1/page/media-list/{quote(t, safe='')}", headers={"User-Agent": WIKI_UA})
                with urlopen(req, timeout=8) as r:
                    items = json.load(r).get("items", [])
            except (OSError, ValueError):
                items = []
            WIKI_CACHE[t] = [{"src": "https:" + i["srcset"][-1]["src"] if i["srcset"][-1]["src"].startswith("//") else i["srcset"][-1]["src"],
                              "cap": (i.get("caption") or {}).get("text", "")[:240], "file": i.get("title", ""), "page": t}
                             for i in items if i.get("type") == "image" and i.get("showInGallery") and i.get("srcset")
                             and not any(w in i.get("title", "").lower() for w in WIKI_SKIP)]
        for x in WIKI_CACHE[t]:
            if x["file"] not in seen:
                seen.add(x["file"])
                out.append(x)
    return out[:10]


def mermaid_js():
    f = EXPLAIN_CWD / "mermaid.min.js"
    if not f.exists():
        EXPLAIN_CWD.mkdir(parents=True, exist_ok=True)
        with urlopen("https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js", timeout=20) as r:
            f.write_bytes(r.read())
    return f.read_bytes()


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "wim-reader"
    hosts = set()
    token = None
    lan_url = None

    def log_message(self, *a):
        pass

    def local(self):
        return self.client_address[0] in LOOPBACK

    def guard(self):
        host = (self.headers.get("Host") or "").lower()
        if host not in self.hosts:  # DNS 리바인딩 막기
            self.send_error(403, "bad host")
            return False
        if self.local():
            return True
        if self.token and hmac.compare_digest(self.cookie("rt") or "", self.token):
            return True
        u = urlsplit(self.path)
        if u.path == "/__reader/login" and self.token and hmac.compare_digest(parse_qs(u.query).get("t", [""])[0], self.token):
            self.send_response(302)
            self.send_header("Set-Cookie", f"rt={self.token}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/")
            self.end_headers()
            return False
        self.send_error(401, "login: /__reader/login?t=<token>")
        return False

    def cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return unquote(v)
        return None

    def send(self, body, ctype, code=200, cache="no-store"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", code)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.guard():
            return
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/__reader", "/__reader/"):
            return self.send(dashboard(self.lan_url if self.local() else None), "text/html; charset=utf-8")
        if u.path == "/__reader/r.js":
            return self.send(TRACKER_JS, "text/javascript; charset=utf-8")
        if u.path == "/__reader/api/doc":
            c, k = db(), nfc(q.get("k", ""))
            revise(c, [k])
            return self.json(doc_payload(c, k, blocks=True))
        if u.path == "/__reader/api/list":
            c = db()
            return self.json({"items": merged(c), "marks": [dict(r) for r in c.execute("SELECT * FROM marks ORDER BY created_at DESC")]})
        if u.path == "/__reader/mermaid.js":
            return self.send(mermaid_js(), "text/javascript; charset=utf-8", cache="max-age=604800")
        if u.path == "/__reader/api/session":  # 이 문서를 만든 Claude Code 세션(폴더 안 파일을 가장 최근에 여러 번 고친 세션)
            if not self.local():
                return self.json({"error": "이 컴퓨터에서만"}, 403)
            info = creator(nfc(q.get("k", "")).strip("/"), refresh=bool(q.get("refresh")))
            return self.json(info or {"sid": None})
        if u.path == "/__reader/api/explains":  # 왼쪽 아래 기록 버튼: 지금까지 만든 카드(용어·문서별 최신 1개)
            c = db()
            if q.get("id"):
                r = c.execute("SELECT id, key, term, answer, created_at FROM explains WHERE id=?", (int(q["id"]),)).fetchone()
                return self.json(dict(r) if r else {})
            like, only = f"%{q.get('q', '')}%", nfc(q.get("k", "")).strip("/")
            rows = c.execute("""SELECT e.id, e.key, e.term, e.created_at, e.model, d.title FROM explains e
                JOIN (SELECT max(id) id FROM explains WHERE q='' AND answer<>'' GROUP BY term, key) m ON m.id = e.id
                LEFT JOIN docs d ON d.key = e.key WHERE (e.term LIKE ? OR e.key LIKE ? OR ifnull(d.title,'') LIKE ?) AND (?='' OR e.key=?)
                ORDER BY e.id DESC LIMIT 400""", (like, like, like, only, only)).fetchall()
            return self.json({"items": [dict(r) for r in rows]})
        if u.path == "/__reader/api/images":
            return self.json({"items": wiki_images(q.get("t", "").split("|"))})
        if u.path.startswith("/__reader/"):
            return self.send_error(404)
        if u.path.startswith("/__file/") and not self.local():  # 홈 폴더는 LAN에 열지 않는다
            return self.send_error(403)
        p, key = resolve(u.path)
        if p is None:
            return self.send_error(404)
        if p.is_dir():
            if (p / "index.html").exists():
                p = p / "index.html"
            else:
                return self.send(listing(p, key), "text/html; charset=utf-8")
        if p.suffix.lower() in (".html", ".htm"):
            return self.send(inject(p.read_bytes()), "text/html; charset=utf-8")
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(p.stat().st_size))
        self.end_headers()
        if self.command != "HEAD":
            with open(p, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile)

    def do_POST(self):
        if not self.guard():
            return
        path = urlsplit(self.path).path
        try:
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")  # sendBeacon은 text/plain
            if b.get("k"):
                b["k"] = nfc(b["k"]).strip("/")
            if path == "/__reader/api/explain":
                return self.explain(b)
            if path == "/__reader/api/ask":
                return self.ask(b)
            fn = {"/__reader/api/doc": save_doc, "/__reader/api/mark": save_mark, "/__reader/api/done": save_done}.get(path)
            if not fn:
                return self.send_error(404)
            self.json(fn(db(), b))
        except (ValueError, KeyError, TypeError) as e:
            self.json({"error": str(e)}, 400)


    def explain(self, b):
        if not (b.get("term") or "").strip():
            return self.json({"error": "term"}, 400)
        if b.get("v") == 3 and not b.get("q") and not b.get("mode"):
            return self.explain_deep(b)
        model = MODELS.get(b.get("mode"), MODELS["fast"])
        h, c = explain_hash(b, model), db()
        hit = None if b.get("fresh") else c.execute("SELECT answer FROM explains WHERE ctx_hash=? ORDER BY id DESC", (h,)).fetchone()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Explain", f"{'cache' if hit else 'live'} {model}")
        self.end_headers()
        if not b.get("v"):  # 서버 재시작 전에 열린 탭의 옛 스크립트는 카드를 못 그린다
            self.wfile.write("⚠ 리더가 새 버전(개념 카드)으로 바뀌었습니다. 이 페이지를 새로고침(⌘R)한 뒤 다시 고르세요.\n\n".encode())
            self.wfile.flush()
        if hit:
            self.wfile.write(hit[0].encode())
            return

        def write(t):
            self.wfile.write(t.encode())
            self.wfile.flush()
        try:
            ans = explain_run(explain_prompt(b), model, write)
        except (BrokenPipeError, ConnectionResetError):
            return
        if ans:
            with LOCK, c:
                c.execute("INSERT INTO explains(key, term, q, ctx_hash, answer, model, created_at) VALUES(?,?,?,?,?,?,?)",
                          (b.get("k", ""), b["term"][:80], (b.get("q") or "")[:500], h, ans, model, time.time()))


DEEP_JOBS, DEEP_LK = {}, threading.Lock()  # 만드는 중인 카드: 같은 카드를 다시 열면(새로고침·다른 탭) 새로 돌리지 않고 붙는다


def _deep_job(b, h):
    job = {"evs": [], "done": False, "cv": threading.Condition()}

    def push(ev):
        with job["cv"]:
            job["evs"].append(ev); job["cv"].notify_all()

    def run():
        try:
            card = deep_card(b, push)
            if card:
                c = db()
                with LOCK, c:
                    c.execute("INSERT INTO explains(key, term, q, ctx_hash, answer, model, created_at) VALUES(?,?,?,?,?,?,?)",
                              (b.get("k", ""), b["term"][:80], "", h, card, DEEP_MODEL + "-deep", time.time()))
        except Exception as e:  # noqa: BLE001
            push({"e": "err", "msg": str(e)[:200]})
        finally:
            with job["cv"]:
                job["done"] = True; job["cv"].notify_all()
            with DEEP_LK:
                DEEP_JOBS.pop(h, None)
    threading.Thread(target=run, daemon=True).start()
    return job


def _deep_handler(self, b):
    h, c = deep_hash(b), db()
    hit = None if b.get("fresh") else c.execute("SELECT answer FROM explains WHERE ctx_hash=? ORDER BY id DESC", (h,)).fetchone()
    with DEEP_LK:
        job = None if hit else DEEP_JOBS.get(h)
        attach = bool(job)
        if not hit and not job:
            job = DEEP_JOBS[h] = _deep_job(b, h)
    self.send_response(200)
    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("X-Explain", f"{'cache' if hit else 'attach' if attach else 'live'} {DEEP_MODEL}-deep")
    self.end_headers()

    def emit(ev):
        self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode()); self.wfile.flush()
    try:
        if hit:
            return emit({"e": "final", "card": hit[0], "cache": 1})
        i = 0  # 파이프라인은 연결과 떨어져 돈다: 창을 닫아도 끝까지 만들어 캐시에 넣는다
        while True:
            with job["cv"]:
                while i >= len(job["evs"]) and not job["done"]:
                    job["cv"].wait(15)
                evs, fin = job["evs"][i:], job["done"]
            i += len(evs)
            for ev in evs:
                emit(ev)
            if fin and i >= len(job["evs"]):
                break
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


Handler.explain_deep = _deep_handler


# ---------- 만든 세션에 묻기·고치기: 문서를 만든 Claude Code 세션을 이어받아(--resume --fork-session) 묻거나 HTML을 고친다 ----------
PROJECTS = Path.home() / ".claude/projects"
ASK_JOBS, ASK_LK, SESS_LK = {}, threading.Lock(), {}
TOOL_ARG = ("file_path", "command", "pattern", "url", "query", "path", "description", "prompt")


def _slug(p):
    return re.sub(r"[^A-Za-z0-9]", "-", str(p))


def _sess_cwd(f):
    try:
        with open(f, errors="ignore") as fh:
            for i, ln in enumerate(fh):
                if '"cwd"' in ln:
                    try:
                        return json.loads(ln).get("cwd")
                    except ValueError:
                        pass
                if i > 400:
                    break
    except OSError:
        pass
    return None


_RG = []


def _rg():
    """rg 실행 파일. 없으면 Claude Code에 든 rg(claude 바이너리를 argv0=rg로)를 쓴다."""
    if not _RG:
        exe = shutil.which("rg") or next((x for x in ("/opt/homebrew/bin/rg", "/usr/local/bin/rg", "/usr/bin/rg") if Path(x).exists()), None)
        if not exe and Path(CLAUDE).exists():
            try:
                c = os.path.realpath(CLAUDE)
                if subprocess.run(["rg", "--version"], executable=c, capture_output=True, text=True, timeout=10).stdout.startswith("ripgrep"):
                    exe = c
            except (OSError, subprocess.SubprocessError):
                pass
        _RG.append(exe)
    return _RG[0]


def find_creator(key):
    """문서가 든 폴더 안 파일을 file_path로 고친(Write·Edit) 세션을 세션 기록(jsonl)에서 찾는다. 서브에이전트 기록은 부모 세션으로 친다.
    3번 이상 고친 세션 중 가장 최근 것, 없으면 가장 많이 고친 것."""
    p = resolve(key)[0]
    if p is None:
        return None
    seg = nfc(p.parent.name) if p.parent != ROOT else nfc(p.name)
    pats = {re.escape(x) for x in (seg, unicodedata.normalize("NFD", seg), json.dumps(seg)[1:-1])}
    home = PROJECTS / _slug(ROOT)
    dirs = [home] if home.is_dir() else ([d for d in PROJECTS.iterdir() if d.is_dir()] if PROJECTS.is_dir() else [])
    if not dirs:
        return None
    rx = ['"file_path":"[^"]*/' + x + '/' for x in pats]
    lines, exe = [], _rg()
    if exe:
        r = subprocess.run(["rg", "-c", "--no-messages", "-g", "*.jsonl", *sum((["-e", x] for x in rx), []), *map(str, dirs)],
                           executable=exe, capture_output=True, text=True, timeout=120)
        lines = [ln.rsplit(":", 1) for ln in r.stdout.splitlines() if ":" in ln]
    else:  # rg가 없으면 글자 그대로 먼저 거르고 걸린 파일만 정규식으로 센다
        cre = re.compile("|".join(rx).encode())
        lits = [("/" + x + "/").encode() for x in (seg, unicodedata.normalize("NFD", seg), json.dumps(seg)[1:-1])]
        for d in dirs:
            for f in d.rglob("*.jsonl"):
                try:
                    data = f.read_bytes()
                except OSError:
                    continue
                if any(x in data for x in lits):
                    n = len(cre.findall(data))
                    if n:
                        lines.append((str(f), str(n)))
    cnt, main = {}, {}
    for f, n in lines:
        fp = Path(f)
        if fp.parent.name == "subagents":
            sid, m = fp.parent.parent.name, fp.parent.parent.parent / f"{fp.parent.parent.name}.jsonl"
        else:
            sid, m = fp.stem, fp
        cnt[sid] = cnt.get(sid, 0) + int(n); main[sid] = m
    cands = [(sid, n, main[sid].stat().st_mtime) for sid, n in cnt.items() if main[sid].exists()]
    if not cands:
        return None
    many = [c for c in cands if c[1] >= 3]
    sid, n, mt = max(many, key=lambda c: c[2]) if many else max(cands, key=lambda c: (c[1], c[2]))
    return {"sid": sid, "n": n, "mtime": mt, "cwd": _sess_cwd(main[sid]) or str(ROOT)}


def creator(key, refresh=False):
    """만든 세션 정보(캐시). 못 찾으면 None. fork = 리더에서 이어 묻는 갈래 세션."""
    if not key:
        return None
    c = db()
    r = c.execute("SELECT creator_session, creator_cwd, creator_n, creator_mtime, ask_session FROM docs WHERE key=?", (key,)).fetchone()
    if r and r[0] is not None and not refresh:
        if not r[0]:
            return None
        if (PROJECTS / _slug(r[1] or ROOT) / f"{r[0]}.jsonl").exists():  # 세션 기록이 지워졌거나 옮겨졌으면 다시 찾는다
            return {"sid": r[0], "cwd": r[1], "n": r[2], "mtime": r[3], "fork": r[4]}
    info = find_creator(key)
    with LOCK, c:
        if not r:
            c.execute("INSERT OR IGNORE INTO docs(key) VALUES(?)", (key,))
        c.execute("UPDATE docs SET creator_session=?, creator_cwd=?, creator_n=?, creator_mtime=?, ask_session=NULL WHERE key=?",
                  (info["sid"] if info else "", info and info["cwd"], info and info["n"], info and info["mtime"], key))
    return dict(info, fork=None) if info else None


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _killpg(pid):
    try:
        os.killpg(os.getpgid(pid), 9)
    except OSError:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _tail_lines(path, alive):
    """파일에 쌓이는 줄을 따라 읽는다. alive()가 거짓이 되면 남은 줄까지 읽고 끝."""
    with open(path, errors="ignore") as fh:
        buf = ""
        while True:
            ln = fh.readline()
            if ln:
                buf += ln
                if buf.endswith("\n"):
                    yield buf; buf = ""
                continue
            if not alive():
                yield from (buf + fh.read()).splitlines()
                return
            time.sleep(0.15)


def _parse_stream(lines, on_text, on_tool=None, on_init=None):
    sid, out, err = None, [], ""
    for ln in lines:
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            sid = ev.get("session_id")
            if on_init and sid:
                on_init(sid)
        elif t == "stream_event":
            e = ev.get("event") or {}
            if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                out.append(e["delta"]["text"]); on_text(e["delta"]["text"])
        elif t == "assistant" and on_tool:
            for blk in (ev.get("message") or {}).get("content") or []:
                if blk.get("type") == "tool_use":
                    if out and not out[-1].endswith("\n\n"):  # 도구를 쓰고 이어 쓰는 글이 앞 글에 붙지 않게
                        out.append("\n\n"); on_text("\n\n")
                    inp = blk.get("input") or {}
                    arg = next((str(inp[k]) for k in TOOL_ARG if inp.get(k)), "")
                    on_tool(blk.get("name", ""), arg.replace(str(ROOT) + "/", "")[:160])
        elif t == "result":
            sid = ev.get("session_id") or sid
            if ev.get("is_error"):
                err = str(ev.get("result") or "오류")[:300]
    return sid, out, err


def session_run(extra, prompt, cwd, on_text, on_tool=None, timeout=900, on_init=None, log=None, on_pid=None):
    """claude -p를 cwd에서 돌린다(이어받기 인자는 extra). (새 세션 id, 답, 오류)를 돌려준다.
    log를 주면 출력을 그 파일에 쓰고 claude를 따로 된 프로세스 그룹으로 띄운다: 리더 서버가 재시작돼도 claude는 계속 돌고 서버가 파일을 다시 이어 읽는다."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))}
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"  # 백그라운드 에이전트를 10분 만에 끊지 않고 끝까지 기다린다(전체 시간 제한은 아래 timer)
    cmd = [CLAUDE, "-p", "--model", DEEP_MODEL, "--output-format", "stream-json", "--verbose", "--include-partial-messages", *extra, prompt]
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "w") as fo, open(str(log) + ".err", "w") as fe:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, start_new_session=True)
        if on_pid:
            on_pid(proc.pid)
        lines = _tail_lines(log, lambda: proc.poll() is None)
    else:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = proc.stdout
    late = []
    timer = threading.Timer(timeout, lambda: (late.append(1), _killpg(proc.pid) if log else proc.kill())); timer.start()
    try:
        sid, out, err = _parse_stream(lines, on_text, on_tool, on_init)
        proc.wait()
        if late:
            err = f"시간 제한 {timeout // 60}분을 넘겨 멈췄습니다. 같은 창에 '이어서 마무리'를 보내면 이 대화에서 이어 갑니다"
        if err in ("", "오류") and log and not out:  # 결과 줄에 이유가 없으면 claude가 stderr에 남긴 이유를 쓴다
            err = Path(str(log) + ".err").read_text(errors="ignore").strip()[-300:] or err
        if not out and not err and proc.returncode:
            tail = Path(str(log) + ".err").read_text(errors="ignore") if log else (proc.stderr.read() or "")
            err = tail.strip()[-300:] or f"claude 종료 코드 {proc.returncode}"
    finally:
        timer.cancel()
        if proc.poll() is None:
            _killpg(proc.pid) if log else proc.kill()
    return sid, "".join(out), err


ASK_RULE = {
    "ask": "규칙: 이 문서를 만든 맥락과 근거 자료(_raw·출처·실측값·메일·회의)를 바탕으로 한국어로 충분히 자세히 답한다. 짧게 줄이지 않는다. "
           "읽는 사람은 이 분야 비전공자라고 가정한다. 글보다 그림으로 설명한다: "
           "먼저 결론을 한두 문장으로 말하고, 절(## 제목)마다 ```mermaid 그림을 하나 이상 넣는다. "
           "구조는 flowchart(무엇이 무엇에 연결되는지), 시간 순서는 sequenceDiagram, 상태 변화는 stateDiagram, 비교는 표로 그린다. "
           "그림 밑 글은 그림을 읽는 법 두세 문장이면 된다. 함수 이름·레지스터·소스 행 번호 같은 코드 수준 근거는 본문에 늘어놓지 말고 마지막 '근거' 절에 한 줄씩 모은다. "
           "이 문서의 숫자와 그 출처, 헷갈리기 쉬운 점, 다음에 확인할 것도 넣는다. "
           "필요하면 파일을 읽고 웹을 찾아 확인한다. 어떤 파일도 고치지 않는다. 확인 안 된 것은 ★로 표시한다. "
           "에이전트(Agent)를 쓸 때는 run_in_background를 쓰지 말고 결과를 받은 뒤 다음으로 간다. '기다리겠다'며 턴을 끝내지 않는다: 턴이 끝나면 작업도 끝난다. ",
    "edit": "규칙: 이 HTML을 사용자 요청대로 고친다. 빌드 원본(_build/pages 조각 등)이 있으면 원본을 고치고 그 폴더의 원래 빌드 명령으로 다시 만든다. "
            "출력 _local.html만 손으로 고치지 않는다. 다른 문서·폴더는 건드리지 않는다. "
            "그림 위주로 쓴다(가장 중요한 규칙): 읽는 사람은 비전공자라고 가정하고, 새로 쓰거나 고치는 절마다 SVG 그림을 먼저 만든다. "
            "구조·연결은 아키텍처·블록도, 시간 순서는 타임라인·시퀀스, 원리는 신호·데이터 흐름도, 선택지는 나란히 비교 그림이나 판정 격자로 그린다. "
            "새 절 하나에 그림 2개 이상, 설명은 그림 안 라벨로 옮기고 절 본문은 한두 문장, 캡션은 그림 하나당 250자 이하로 쓴다. "
            "함수 이름·레지스터 주소·소스 행 번호·인용문 같은 코드 수준 근거는 문단으로 늘어놓지 않는다: 꼭 필요한 것만 그림 라벨에 넣고 나머지는 접힌 '근거 더 보기'에 한 줄씩 짧게 둔다. "
            "기존 절이 글로 가득하면(접힌 부분 포함) 그 글을 그림으로 바꾸는 것도 고치는 일에 포함한다. "
            "이 문서 폴더나 _build의 AUTHORING.md, 그리고 html-brief 스킬 규격(~/.claude/skills/html-brief/references/authoring.md 또는 이 폴더 위쪽 .claude/skills/html-brief/references/authoring.md)이 있으면 먼저 읽고 따른다. "
            "사용자가 브라우저에서 바뀌는 모습을 지켜보고 있으니, 고칠 곳이 여러 군데면 한 군데 고칠 때마다 빌드해서 바로 보이게 한다. "
            "내용은 이 문서를 만들 때와 같은 수준으로 충실하게 쓴다: 근거 자료를 다시 읽고, 필요하면 웹을 찾아 숫자와 출처를 붙이고, 이 문서의 그림 양식(SVG 클래스·색)을 그대로 따른다. 한 줄 땜질로 끝내지 않는다. "
            "끝나면 무엇을 어디에 왜 바꿨는지, 그림을 몇 개 넣었는지 한국어로 정리해 답한다. "
            "에이전트(Agent)를 쓸 때는 run_in_background를 쓰지 말고 결과를 받은 뒤 다음으로 간다. '기다리겠다'며 턴을 끝내지 않는다: 턴이 끝나면 작업도 끝난다. ""원본을 고쳤으면 턴을 끝내기 전에 반드시 빌드까지 마친다.",
}


EDIT_ACTIVE, EDIT_CV = {}, threading.Condition()
JOBDIR = DB.parent / "jobs"  # 질문·수정 작업 기록: <id>.json(요청·pid·끝 결과) + <id>.out(claude 출력). 서버가 재시작돼도 여기서 이어 받는다


def _job_meta(jid, upd):
    f = JOBDIR / f"{jid}.json"
    JOBDIR.mkdir(parents=True, exist_ok=True)
    try:
        cur = json.loads(f.read_text()) if f.exists() else {}
    except ValueError:
        cur = {}
    cur.update(upd)
    f.write_text(json.dumps(cur, ensure_ascii=False))


def _src_changed(p, t0):
    """출력 HTML은 그대로인데 같은 폴더의 원본(_build·pages 등)만 바뀌었는지: 빌드 전에 끊긴 수정."""
    if not p:
        return []
    out = []
    for f in p.parent.rglob("*"):
        try:
            if f.is_file() and f != p and f.stat().st_mtime > t0 and "/.git/" not in str(f):
                out.append(str(f.relative_to(p.parent)))
        except OSError:
            pass
    return sorted(out)[:12]


def _ask_resume(jid, push):
    """서버가 다시 켜진 뒤: 기록된 작업을 출력 파일 처음부터 다시 읽어 보내고, claude가 아직 돌면 끝날 때까지 따라간다."""
    m = json.loads((JOBDIR / f"{jid}.json").read_text())
    p = resolve(m["k"])[0]
    push({"e": "meta", "how": m.get("how"), "sid": m.get("sid"), "mode": m.get("mode")})
    if m.get("final"):
        _parse_stream(_tail_lines(m["log"], lambda: False), lambda t: push({"e": "text", "d": t}), lambda n, a: push({"e": "tool", "name": n, "arg": a}))
        return push(m["final"])
    push({"e": "note", "msg": "리더 서버가 다시 켜져 돌던 작업을 이어 받습니다"})
    pid, stop = m.get("pid") or 0, threading.Event()
    if m.get("mode") == "edit" and p:
        if p.stat().st_mtime != m.get("mt0"):
            push({"e": "file", "t": p.stat().st_mtime})

        def watch(last=p.stat().st_mtime):
            while not stop.wait(0.5):
                try:
                    if p.stat().st_mtime != last:
                        time.sleep(0.3); last = p.stat().st_mtime; push({"e": "file", "t": last})
                except OSError:
                    pass
        threading.Thread(target=watch, daemon=True).start()
    _, out, err = _parse_stream(_tail_lines(m["log"], lambda: pid and _alive(pid)), lambda t: push({"e": "text", "d": t}),
                                lambda n, a: push({"e": "tool", "name": n, "arg": a}))
    stop.set()
    changed = bool(p and p.exists() and p.stat().st_mtime != m.get("mt0"))
    fin = {"e": "err", "msg": err or "작업이 중간에 끊겼습니다"} if not out else {"e": "done", "changed": changed, "sec": round(time.time() - m.get("t0", time.time())), "how": m.get("how"), "err": err}
    if m.get("mode") == "edit" and not changed:
        fin["src"] = _src_changed(p, m.get("t0", time.time()))
    _job_meta(jid, {"final": fin})
    push(fin)  # 폴더별로 돌고 있는 수정 요청(같은 덩어리는 줄 세우고, 다른 덩어리는 동시에)


def _ask_run(b, push):
    """한 창(스레드)의 요청 하나. 창마다 자기 세션 갈래를 가져 여러 창이 동시에 돈다.
    질문은 모두 동시에, 수정은 다른 덩어리끼리 동시에·같은 덩어리는 먼저 온 순서로(중재). 빌드는 폴더 잠금으로 한 번에 하나."""
    k, mode = b["k"], ("edit" if b.get("mode") == "edit" else "ask")
    thread = re.sub(r"[^A-Za-z0-9-]", "", str(b.get("thread") or ""))[:64]
    t0, info = time.time(), creator(k)
    p = resolve(k)[0]
    folder = str(p.parent) if p else k
    me = {"id": str(b.get("id") or ""), "blk": str(b.get("blk") or "")[:200], "sel": (b.get("sel") or "")[:100], "q": b["q"].strip()[:100]}
    others = []
    if mode == "edit":
        with EDIT_CV:
            told = False
            while any(o["blk"] and o["blk"] == me["blk"] for o in EDIT_ACTIVE.get(folder, [])):
                n = sum(1 for o in EDIT_ACTIVE[folder] if o["blk"] == me["blk"])
                if not told:
                    push({"e": "queue", "n": n}); push({"e": "note", "msg": f"같은 덩어리를 고치는 요청 {n}개가 먼저 돌고 있어 끝나길 기다립니다"}); told = True
                EDIT_CV.wait(5)
            others = list(EDIT_ACTIVE.get(folder, []))
            EDIT_ACTIVE.setdefault(folder, []).append(me)
        if told:
            push({"e": "note", "msg": "차례가 와서 시작합니다"})
    lk = SESS_LK.setdefault(thread, threading.Lock()) if thread else None
    if lk and lk.locked():
        push({"e": "note", "msg": "이 창의 앞 요청이 끝나면 이어서 묻습니다"})
    if lk:
        lk.acquire()
    try:
        mt0 = p.stat().st_mtime if p else 0
        rule = ASK_RULE[mode]
        if mode == "edit":
            lock = f"uv run --quiet {Path(__file__).resolve()} lock {folder} -- <빌드 명령>"
            rule += (f" 빌드 명령은 반드시 `{lock}` 형태로 감싸 돌린다(여러 요청이 동시에 빌드하지 않게). "
                     "파일 전체를 Write로 덮어쓰지 말고 Edit로 필요한 부분만 고친다. 'modified since read' 오류가 나면 다시 읽고 고친다.")
            if others:
                rule += " 지금 같은 문서의 다른 곳을 동시에 고치는 요청이 있다. 그 부분은 건드리지 않는다: " + " / ".join(f"«{o['sel']}»({o['q']})" for o in others)
        prompt = "\n".join([f"[WIM Reader에서 보낸 {'질문' if mode == 'ask' else 'HTML 수정 요청'}]", f"문서: {p or k}",
                             f"절: {b.get('head', '')[:200]}", f"사용자가 고른 부분: «{(b.get('sel') or '')[:2000]}»",
                             f"주변 글: «{(b.get('ctx') or '')[:1500]}»", "", b["q"].strip()[:4000], "", rule])
        tools = (["--tools", "Read,Grep,Glob,WebSearch,WebFetch", "--setting-sources", ""] if mode == "ask" else []) + ["--permission-mode", "bypassPermissions"]
        stop = threading.Event()

        def watch():  # 고치는 동안 파일이 바뀔 때마다 알린다(브라우저가 바뀐 곳만 바로 바꿔 끼운다)
            last = mt0
            while not stop.wait(0.5):
                try:
                    m = p.stat().st_mtime
                    if m == last:
                        continue
                    time.sleep(0.3)  # 쓰는 중이면 끝나길 잠깐 기다린다
                    last = p.stat().st_mtime
                except OSError:
                    continue
                push({"e": "file", "t": last})
        if p and mode == "edit":
            threading.Thread(target=watch, daemon=True).start()
        for attempt in range(2):
            cwd = (info or {}).get("cwd") or str(ROOT)
            if thread and (PROJECTS / _slug(cwd) / f"{thread}.jsonl").exists():
                how, res = "cont", ["--resume", thread]
            elif info:
                how, res = "fork", ["--resume", info["sid"], "--fork-session"]
            else:
                how, res, cwd = "new", [], str(ROOT)
            push({"e": "meta", "how": how, "sid": info and info["sid"], "n": info and info["n"], "mtime": info and info["mtime"], "mode": mode})
            jid = b.get("_jid") or pysecrets.token_hex(8)
            meta = {"k": k, "mode": mode, "how": how, "sid": info and info["sid"], "mt0": mt0, "t0": t0, "log": str(JOBDIR / f"{jid}.out")}
            sid, out, err = session_run(res + tools, prompt, cwd, lambda t: push({"e": "text", "d": t}),
                                        lambda n, a: push({"e": "tool", "name": n, "arg": a}), timeout=3600 if mode == "edit" else 1800,
                                        on_init=(lambda x: push({"e": "thread", "sid": x})) if how != "cont" else None,
                                        log=JOBDIR / f"{jid}.out", on_pid=lambda pid: _job_meta(jid, dict(meta, pid=pid)))
            if err and how == "cont" and not out and attempt == 0:  # 이 창의 갈래가 사라졌으면 만든 세션에서 다시 갈라 시작
                thread = ""
                continue
            break
        if sid and how == "new" and not err:
            c = db()
            with LOCK, c:
                c.execute("UPDATE docs SET creator_session=?, creator_cwd=?, creator_n=0, creator_mtime=? WHERE key=?", (sid, cwd, time.time(), k))
        stop.set()
        changed = bool(p and p.exists() and p.stat().st_mtime != mt0)
        fin = {"e": "err", "msg": err} if err and not out else {"e": "done", "changed": changed, "sec": round(time.time() - t0), "how": how, "err": err}
        if mode == "edit" and not changed:
            fin["src"] = _src_changed(p, t0)
        if b.get("_jid"):
            _job_meta(b["_jid"], {"final": fin})
        push(fin)
    finally:
        if lk:
            lk.release()
        if mode == "edit":
            with EDIT_CV:
                if me in EDIT_ACTIVE.get(folder, []):
                    EDIT_ACTIVE[folder].remove(me)
                EDIT_CV.notify_all()


def _ask_handler(self, b):
    if not self.local():  # 파일을 고칠 수 있는 경로라 LAN에는 열지 않는다
        return self.json({"error": "질문·수정은 이 컴퓨터에서만 됩니다"}, 403)
    if not b.get("k") or not (b.get("q") or "").strip():
        return self.json({"error": "질문이 비었습니다"}, 400)
    jid = re.sub(r"[^A-Za-z0-9_-]", "", str(b.get("id") or ""))[:64] or pysecrets.token_hex(8)
    with ASK_LK:
        job = ASK_JOBS.get(jid)
        attach = bool(job)
        stored = not job and (JOBDIR / f"{jid}.json").exists()
        if not job and (stored or not b.get("resume_only")):
            job = ASK_JOBS[jid] = {"evs": [], "done": False, "cv": threading.Condition()}
            b["_jid"] = jid

            def push(ev, job=job):
                with job["cv"]:
                    job["evs"].append(ev); job["cv"].notify_all()

            def run(job=job, push=push, stored=stored):
                try:
                    _ask_resume(jid, push) if stored else _ask_run(b, push)
                except Exception as e:  # noqa: BLE001
                    push({"e": "err", "msg": str(e)[:300]})
                finally:
                    with job["cv"]:
                        job["done"] = True; job["cv"].notify_all()
                    threading.Timer(600, lambda: ASK_JOBS.pop(jid, None)).start()  # 끝난 뒤 10분은 다시 붙을 수 있게 남긴다
            threading.Thread(target=run, daemon=True).start()
    self.send_response(200)
    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("X-Ask", "attach" if attach else "live")
    self.end_headers()

    def emit(ev):
        self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode()); self.wfile.flush()
    try:
        if not job:  # 서버가 재시작돼 작업이 사라졌다. 수정 요청을 두 번 돌리지 않도록 새로 시작하지 않는다
            return emit({"e": "err", "msg": "서버가 재시작돼 요청이 끊겼습니다. 결과를 확인하고 필요하면 다시 보내 주세요"})
        i = 0
        while True:
            with job["cv"]:
                if i >= len(job["evs"]) and not job["done"]:
                    job["cv"].wait(15)
                evs, fin = job["evs"][i:], job["done"]
            if not evs and not fin:  # 심장 박동: 브라우저가 연결이 살아 있는지 안다
                emit({"e": "hb"}); continue
            i += len(evs)
            for ev in evs:
                emit(ev)
            if fin and i >= len(job["evs"]):
                break
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


Handler.ask = _ask_handler


def inject(raw):
    tag = b'<script src="/__reader/r.js" defer></script>'
    i = raw.lower().rfind(b"</body>")
    return raw[:i] + tag + raw[i:] if i >= 0 else raw + tag


def listing(p, key):
    rows = []
    for x in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), nfc(x.name))):
        if x.name.startswith(".") or x.name in BLOCK:
            continue
        n = nfc(x.name) + ("/" if x.is_dir() else "")
        rows.append(f'<li><a href="/{quote((key + "/" if key else "") + n)}">{html.escape(n)}</a></li>')
    return (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(key or "/")}</title><body style="font:15px system-ui;margin:16px">'
            f'<p><a href="/">← 대시보드</a></p><h3>/{html.escape(key)}</h3><ul>{"".join(rows)}</ul>')


def dashboard(lan_url):
    c = db()
    data = {"items": merged(c), "marks": [dict(r) for r in c.execute("SELECT * FROM marks ORDER BY created_at DESC")], "lan": lan_url}
    return DASHBOARD_HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))


def lan_ip():
    try:
        return subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, timeout=3).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def serve(a):
    for f in JOBDIR.glob("*") if JOBDIR.is_dir() else []:
        if f.stat().st_mtime < time.time() - 3 * 86400:
            f.unlink(missing_ok=True)
    moved = migrate()
    for old, new in moved:
        print(f"경로 이전: {old} → {new}")
    binds = [("127.0.0.1", a.port)]
    Handler.hosts = {f"127.0.0.1:{a.port}", f"localhost:{a.port}"}
    if a.lan:
        ip = a.host or lan_ip()
        if not ip:
            sys.exit("LAN IP를 못 찾았다(--host로 지정)")
        if not TOKEN.exists():
            TOKEN.parent.mkdir(exist_ok=True)
            TOKEN.write_text(pysecrets.token_urlsafe(18))
            TOKEN.chmod(0o600)
        Handler.token = TOKEN.read_text().strip()
        Handler.hosts.add(f"{ip}:{a.port}")
        Handler.lan_url = f"http://{ip}:{a.port}/__reader/login?t={Handler.token}"
        binds.append((ip, a.port))
    servers = []
    for h, p in binds:
        s = ThreadingHTTPServer((h, p), Handler)
        s.daemon_threads = True
        servers.append(s)
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f"리더: http://127.0.0.1:{a.port}/   DB: {DB.relative_to(ROOT)}", flush=True)
    if Handler.lan_url:
        print(f"LAN(폰·다른 PC에서 한 번 열면 쿠키가 남는다): {Handler.lan_url}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


# ---------- CLI 조회 ----------

def ago(t):
    if not t:
        return "-"
    s = time.time() - t
    return f"{max(1, int(s // 60))}분 전" if s < 3600 else f"{int(s // 3600)}시간 전" if s < 86400 else time.strftime("%m-%d", time.localtime(t))


def line(it):
    pct = "완료" if it["done_at"] else f"{round((it['prog'] or 0) * 100)}%"
    pct += " · 고쳐짐" if it.get("revised") else ""
    if it.get("blk_total") and not it["done_at"]:
        pct += f" · 안 읽은 덩어리 {it['blk_total'] - (it['blk_read'] or 0)}" + (f"(바뀐 곳 {it['blk_new']})" if it.get("blk_new") else "")
    extra = f" · {it['last_device']} {ago(it['last_open'])}" if it["last_open"] else ""
    marks = f" · 책갈피 {it['marks']}" if it["marks"] else ""
    return f"- [{pct}] {it['title']} ({it['k']}){extra}{marks}"


def cli(a):
    items = merged()
    if a.cmd == "status":
        n = {s: sum(1 for i in items if i["state"] == s) for s in ("done", "reading", "unread")}
        recent = sorted((i for i in items if i["last_open"]), key=lambda i: -i["last_open"])[:5]
        if a.json:
            return print(json.dumps({"counts": n, "recent": recent}, ensure_ascii=False, indent=1))
        print(f"전체 {len(items)} · 완료 {n['done']} · 읽는 중 {n['reading']} · 안 읽음 {n['unread']}  (DB {DB.relative_to(ROOT)})")
        print("\n최근 연 문서:" if recent else "\n아직 연 문서 없음. `uv run reader.py serve`")
        for i in recent:
            print(line(i))
        reading = [i for i in items if i["state"] == "reading" and i not in recent]
        if reading:
            print("\n읽다 만 문서:")
            for i in sorted(reading, key=lambda i: -(i["last_open"] or 0))[:10]:
                print(line(i))
    elif a.cmd == "unread":
        sel = [i for i in items if i["state"] == "unread" and (not a.q or a.q in i["k"] or a.q in i["title"])]
        if a.json:
            return print(json.dumps(sel, ensure_ascii=False, indent=1))
        print(f"안 읽은 문서 {len(sel)}건")
        for g in dict.fromkeys(i["group"] for i in sel):
            print(f"\n## {g}")
            for i in sel:
                if i["group"] == g:
                    print(f"- {i['title']} ({i['k']})")
    elif a.cmd == "recent":
        sel = sorted((i for i in items if i["last_open"]), key=lambda i: -i["last_open"])[: a.n]
        if a.json:
            return print(json.dumps(sel, ensure_ascii=False, indent=1))
        for i in sel:
            print(line(i))
    elif a.cmd == "reset":
        c = db()
        for path in a.path:
            pth = Path(path).expanduser()
            k = key_of(pth) if pth.exists() else nfc(path).strip("/")
            with LOCK, c:
                n = c.execute("UPDATE docs SET done_at=NULL, done_off=0, max_ratio=0, read_pct=0, blk_read=0, revised_at=? WHERE key=?", (time.time(), k)).rowcount
                c.execute("UPDATE blocks SET read_at=NULL WHERE key=?", (k,))
            print(f"{'초기화' if n else '기록 없음'}: {k}")
    elif a.cmd == "explains":
        c = db()
        titles = {i["k"]: i["title"] for i in items}
        rows = [dict(r) for r in c.execute("SELECT * FROM explains ORDER BY created_at DESC")
                if not a.q or a.q in r["term"] or a.q in r["answer"] or a.q in r["key"]]
        if a.json:
            return print(json.dumps(rows, ensure_ascii=False, indent=1))
        for r in rows:
            q = f" ↳ {r['q']}" if r["q"] else ""
            print(f"- {r['term']}{q} · {titles.get(r['key'], r['key'])} ({r['model']}, {ago(r['created_at'])})")
            print("  " + r["answer"].replace("\n", " ")[:160])
    elif a.cmd == "marks":
        c = db()
        titles = {i["k"]: i["title"] for i in items}
        rows = [dict(r) for r in c.execute("SELECT * FROM marks ORDER BY key, ratio")
                if not a.q or a.q in (r["note"] or "") or a.q in r["key"] or a.q in titles.get(r["key"], "")]
        if a.json:
            return print(json.dumps(rows, ensure_ascii=False, indent=1))
        for r in rows:
            note = f" — {r['note']}" if r["note"] else ""
            print(f"- {titles.get(r['key'], r['key'])} {round(r['ratio'] * 100)}%{note} ({r['device']}, {ago(r['created_at'])})  /{r['key']}#__rm={r['id']}")


def alive(port):
    try:
        with urlopen(f"http://127.0.0.1:{port}/__reader/r.js", timeout=1):
            return True
    except OSError:
        return False


def ensure_server(port):
    """서버가 안 떠 있으면 뒤에서 띄우고 응답할 때까지 기다린다."""
    if alive(port):
        return
    log = DATA / "server.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([UV, "run", "--quiet", str(Path(__file__).resolve()), "serve", "--port", str(port)], cwd=ROOT,
                     stdout=open(log, "a"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if alive(port):
            return
        time.sleep(0.2)


def url_for(path, port):
    p = Path(path).expanduser()
    p = (p if p.is_absolute() else Path.cwd() / p).resolve()
    try:
        k = key_of(p)
    except ValueError:  # ROOT 밖
        k = "__file" + nfc(str(p))
    return f"http://127.0.0.1:{port}/{quote(k)}"


def open_cmd(a):
    ensure_server(a.port)
    urls = [url_for(x, a.port) for x in a.path] or [f"http://127.0.0.1:{a.port}/"]
    for url in urls:
        print(url)
        subprocess.run(["open", url])  # http는 기본 브라우저로(.html 기본 앱이 이 오프너여도 되돌아오지 않는다)


def agent(a):
    if a.cmd == "uninstall-agent":
        subprocess.run(["launchctl", "unload", str(AGENT)], capture_output=True)
        AGENT.unlink(missing_ok=True)
        return print("리더 자동 실행 해제")
    CONF.parent.mkdir(parents=True, exist_ok=True); CONF.write_text(str(ROOT))
    args = [UV, "run", "--quiet", str(Path(__file__).resolve()), "--root", str(ROOT), "serve", "--port", str(a.port)] + (["--lan"] if a.lan else [])
    AGENT.parent.mkdir(parents=True, exist_ok=True)
    log = DATA / "server.log"
    AGENT.write_bytes(plistlib.dumps({"Label": LABEL, "ProgramArguments": args, "RunAtLoad": True, "KeepAlive": True,
                                      "WorkingDirectory": str(ROOT), "StandardOutPath": str(log), "StandardErrorPath": str(log)}))
    subprocess.run(["launchctl", "unload", str(AGENT)], capture_output=True)
    subprocess.run(["launchctl", "load", str(AGENT)], check=True)
    print(f"로그인 때 자동 실행: {AGENT}\n문서 폴더: {ROOT}\n로그: {log}")


EXT_DIR = Path(__file__).resolve().parent / "reader_chrome_ext"

EXT_BG = r"""// file://로 열린 HTML을 리더 서버 주소로 넘긴다. 서버가 안 떠 있으면 그대로 둔다.
const ROOT = __ROOT__, HOME = __HOME__, BASE = __BASE__;
const enc = p => p.split('/').map(encodeURIComponent).join('/');
async function alive() {
  try { const r = await fetch(BASE + '/__reader/r.js', {method: 'HEAD', cache: 'no-store'}); return r.ok; } catch { return false; }
}
chrome.webNavigation.onBeforeNavigate.addListener(async d => {
  if (d.frameId !== 0) return;
  const u = new URL(d.url);
  const path = decodeURIComponent(u.pathname).normalize('NFC');
  if (!/\.x?html?$/i.test(path)) return;
  let key = null;
  if (path.startsWith(ROOT + '/')) key = path.slice(ROOT.length + 1);
  else if (path.startsWith(HOME + '/')) key = '__file' + path;
  if (!key || key.split('/').some(p => p.startsWith('.'))) return;
  if (!(await alive())) return;
  chrome.tabs.update(d.tabId, {url: BASE + '/' + enc(key) + u.hash});
}, {url: [{schemes: ['file']}]});
"""


def extension(a):
    EXT_DIR.mkdir(exist_ok=True)
    base = f"http://127.0.0.1:{a.port}"
    (EXT_DIR / "manifest.json").write_text(json.dumps({
        "manifest_version": 3, "name": "WIM Reader", "version": "1.0",
        "description": "로컬 HTML을 리더 서버(읽기 진행도·책갈피)로 연다",
        "permissions": ["webNavigation", "tabs"], "host_permissions": ["file:///*", f"{base}/*"],
        "background": {"service_worker": "bg.js"}}, ensure_ascii=False, indent=1))
    (EXT_DIR / "bg.js").write_text(EXT_BG.replace("__ROOT__", json.dumps(nfc(str(ROOT)))).replace("__HOME__", json.dumps(nfc(str(HOME))))
                                   .replace("__BASE__", json.dumps(base)))
    print(f"확장 폴더: {EXT_DIR}")
    print("Chrome: chrome://extensions → 개발자 모드 → '압축해제된 확장 프로그램 로드'로 위 폴더 선택 → 세부정보에서 '파일 URL에 대한 액세스 허용' 켜기")


def main():
    ap = argparse.ArgumentParser(description="HTML 리더 서버·읽기 진행도 DB")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--lan", action="store_true")
    s.add_argument("--host")
    s.add_argument("--port", type=int, default=PORT)
    for n in ("status", "unread", "recent", "marks", "explains"):
        p = sub.add_parser(n)
        p.add_argument("--json", action="store_true")
        if n in ("unread", "marks", "explains"):
            p.add_argument("q", nargs="?", default="")
        if n == "recent":
            p.add_argument("-n", type=int, default=10)
    r = sub.add_parser("reset", help="읽음 표시·진행도 지우기(위치·책갈피는 유지)")
    r.add_argument("path", nargs="+")
    e = sub.add_parser("install-extension")
    e.add_argument("--port", type=int, default=PORT)
    o = sub.add_parser("open")
    o.add_argument("path", nargs="*")
    o.add_argument("--port", type=int, default=PORT)
    lk = sub.add_parser("lock", help="폴더 잠금을 잡고 명령 실행(동시에 들어온 HTML 수정 요청들의 빌드를 한 번에 하나씩)")
    lk.add_argument("dir")
    lk.add_argument("command", nargs=argparse.REMAINDER)
    for n in ("install-agent", "uninstall-agent"):
        p = sub.add_parser(n)
        p.add_argument("--lan", action="store_true")
        p.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    if a.cmd == "lock":
        import fcntl
        d = Path(a.dir).expanduser()
        cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
        with open((d if d.is_dir() else d.parent) / ".wim_build.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            sys.exit(subprocess.run(cmd).returncode if cmd else 0)
    if a.cmd == "serve":
        serve(a)
    elif a.cmd == "open":
        open_cmd(a)
    elif a.cmd.endswith("agent"):
        agent(a)
    elif a.cmd == "install-extension":
        extension(a)
    else:
        cli(a)


# ---------- 페이지에 끼워 넣는 스크립트 ----------

TRACKER_JS = r"""
(() => {
  if (window.__wimReader || window.top !== window) return;
  window.__wimReader = 1;
  const K = decodeURIComponent(location.pathname).normalize('NFC').replace(/^\/+/, '');
  const API = '/__reader/api/';
  const SEL = 'h1,h2,h3,h4,h5,h6,p,li,pre,figcaption,dt,dd,td,blockquote,figure,img,svg,table';
  const ck = n => (document.cookie.split('; ').find(s => s.startsWith(n + '=')) || '').slice(n.length + 1);
  const guessDev = () => { const u = navigator.userAgent;
    if (/iPhone/.test(u)) return '아이폰'; if (/iPad/.test(u) || (/Macintosh/.test(u) && navigator.maxTouchPoints > 1)) return '아이패드';
    if (/Android/.test(u)) return '안드로이드'; if (/Macintosh/.test(u)) return 'Mac'; return 'PC'; };
  let DEV = decodeURIComponent(ck('rdev')) || guessDev();
  const setDev = d => { DEV = d; document.cookie = 'rdev=' + encodeURIComponent(d) + '; path=/; max-age=31536000; samesite=lax'; };
  if (!ck('rdev')) setDev(DEV);

  // 스크롤 주체: 문서가 안 움직이면 가장 많이 스크롤되는 요소(덱형 페이지)
  let S = null;
  const findScroller = () => {
    const se = document.scrollingElement || document.documentElement;
    if (se.scrollHeight - innerHeight > 40) return null;
    let best = null, gap = 40;
    for (const el of document.body.querySelectorAll('*')) {
      const g = el.scrollHeight - el.clientHeight;
      if (g > gap && el.clientHeight > innerHeight * 0.4 && /(auto|scroll)/.test(getComputedStyle(el).overflowY)) { best = el; gap = g; }
    }
    return best;
  };
  const top = () => S ? S.scrollTop : scrollY;
  const span = () => S ? S.scrollHeight - S.clientHeight : (document.scrollingElement || document.documentElement).scrollHeight - innerHeight;
  const ratio = () => { const m = span(); return m <= 2 ? 1 : Math.max(0, Math.min(1, top() / m)); };
  const viewTop = () => S ? S.getBoundingClientRect().top : 0;
  const scrollToY = y => S ? S.scrollTo({top: y}) : scrollTo({top: y});

  const txt = el => (el.tagName === 'IMG' ? (el.alt || el.src.slice(-40)) : (el.textContent || '')).replace(/\s+/g, ' ').trim().slice(0, 40);
  const anchor = () => {
    const y0 = viewTop() + 8;
    for (const el of document.querySelectorAll(SEL)) {
      if (el.closest('#__wim_reader')) continue;
      const r = el.getBoundingClientRect();
      if (r.height < 1 || r.bottom <= y0) continue;
      const tag = el.tagName.toLowerCase();
      return {tag, i: [...document.getElementsByTagName(tag)].indexOf(el), t: txt(el), o: Math.max(0, Math.min(1, (y0 - r.top) / r.height))};
    }
    return null;
  };
  const goTo = (a, r) => {
    if (a && a.tag) {
      const els = [...document.getElementsByTagName(a.tag)];
      let el = els[a.i] && txt(els[a.i]) === a.t ? els[a.i] : null;
      if (!el && a.t) { let best = 1e9;
        els.forEach((e, i) => { if (txt(e) === a.t && Math.abs(i - a.i) < best) { best = Math.abs(i - a.i); el = e; } }); }
      if (el) { const rc = el.getBoundingClientRect(); scrollToY(top() + rc.top - viewTop() + rc.height * (a.o || 0) - 8); return; }
    }
    if (r != null) scrollToY(r * span());
  };

  // UI(그림자 DOM으로 페이지 CSS와 격리)
  const host = document.createElement('div'); host.id = '__wim_reader';
  host.style.cssText = 'all:initial;position:fixed;inset:0 auto auto 0;z-index:2147483647;width:0;height:0';
  const R = host.attachShadow({mode: 'open'});
  R.innerHTML = `<style>
    :host{--bg:#fff;--fg:#1d1d1f;--mu:#6e6e73;--ac:#2f6fed;--ln:#e3e3e8}
    @media (prefers-color-scheme:dark){:host{--bg:#1f1f22;--fg:#f2f2f5;--mu:#a1a1a8;--ac:#6c9cff;--ln:#3a3a40}}
    *{box-sizing:border-box;font:13px/1.45 -apple-system,system-ui,"Apple SD Gothic Neo",sans-serif}
    .rg{position:fixed;inset:0;pointer-events:none;overflow:hidden}.rg>i{position:absolute;left:0;top:0;width:100%;height:0}
    .rg b{position:absolute;width:3px;border-radius:2px;transition:background .6s,opacity .6s}.rg b.r{background:#22a35a;opacity:.5}
    .rg b.u{background:#9aa0a6;opacity:.28}.rg b.n{background:#f5b301;width:4px;opacity:.95}
    .rg s{position:absolute;background:#f5b3011a;border-radius:6px;text-decoration:none}
    .rg b.now{animation:rbnow 1.2s ease-out}@keyframes rbnow{0%{background:#f5b301;opacity:1;box-shadow:0 0 0 4px #22a35a55}100%{opacity:.5}}
    .rl{position:fixed;right:1px;top:3px;bottom:0;width:7px;cursor:pointer;opacity:.85}.rl:hover{width:12px;opacity:1}
    .rl b{position:absolute;left:0;width:100%;border-radius:1px;pointer-events:none}.rl b.r{background:#22a35a99}.rl b.u{background:#9aa0a644}.rl b.n{background:#f5b301}
    .rl i{position:absolute;left:-1px;right:-1px;border:1.5px solid var(--ac);border-radius:3px;pointer-events:none}
    .fab{position:fixed;right:14px;bottom:14px;width:38px;height:38px;border-radius:19px;border:1px solid var(--ln);background:var(--bg);color:var(--fg);
      opacity:.55;cursor:pointer;box-shadow:0 2px 8px #0002;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600}
    .fab:hover{opacity:1}
    .toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--bg);color:var(--fg);border:1px solid var(--ln);
      border-radius:10px;padding:8px 12px;box-shadow:0 4px 16px #0003;display:flex;gap:10px;align-items:center;max-width:calc(100vw - 32px)}
    button{background:none;border:1px solid var(--ln);border-radius:7px;padding:4px 9px;color:var(--fg);cursor:pointer}
    button.p{background:var(--ac);border-color:var(--ac);color:#fff}
    .pan{position:fixed;right:14px;bottom:60px;width:min(340px,calc(100vw - 28px));max-height:70vh;overflow:auto;background:var(--bg);color:var(--fg);
      border:1px solid var(--ln);border-radius:12px;box-shadow:0 8px 28px #0004;padding:12px}
    .pan h4{margin:0 0 4px;font-weight:600;font-size:14px}.mu{color:var(--mu)}
    .row{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
    .mk{display:flex;gap:8px;align-items:flex-start;padding:6px 0;border-top:1px solid var(--ln);cursor:pointer}
    .mk span:first-child{min-width:36px;color:var(--ac);font-weight:600}.mk .x{margin-left:auto;color:var(--mu);border:0;padding:0 4px}
    a{color:var(--ac)}[hidden]{display:none!important}
    .chip{position:fixed;display:flex;align-items:center;background:var(--ac);border-radius:14px;padding:2px;box-shadow:0 2px 10px #0003}
    .chip button{background:none;border:0;color:#fff;font-size:12px;font-weight:700;padding:3px 10px;border-radius:12px;cursor:pointer}
    .chip button:hover{background:#fff3}.chip button+button:not([hidden]){box-shadow:inset 1px 0 #fff6}
    .ask{position:fixed;width:min(620px,calc(100vw - 24px));max-height:min(72vh,680px);display:flex;flex-direction:column;
      --k-bg:#f7f6f2;--k-paper:#fff;--k-ink:#1d2330;--k-muted:#5b6475;--k-line:#d9dce3;--k-acc:#1f5fbf;--k-acc-soft:#e3edfb;--k-box:#eef1f6;--k-ok:#2c8a55;--k-warn:#c43d3d;
      --k-font:"Pretendard","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif;
      background:var(--k-paper);color:var(--k-ink);border:1px solid var(--k-line);border-radius:14px;box-shadow:0 18px 50px #0006;overflow:hidden}
    .ask,.ask *{font-family:var(--k-font)}
    .ask header{display:flex;align-items:center;gap:8px;padding:9px 12px;border-bottom:1px solid var(--k-line);background:var(--k-box);cursor:default}
    .ask header b{font-size:13.5px;white-space:nowrap}.ask header small{flex:1;min-width:0;font-size:11.5px;color:var(--k-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .ask button{color:var(--k-ink);border-color:var(--k-line);background:var(--k-paper);font-size:12.5px}
    .ask .x{border:0;background:none;font-size:20px;line-height:1;padding:0 4px;color:var(--k-muted)}
    .ask .qs{margin:10px 12px 0;padding:6px 10px;border-left:4px solid #f5b301;background:color-mix(in srgb,#f5b301 14%,transparent);border-radius:6px;
      font-size:13px;line-height:1.5;max-height:4.8em;overflow:auto;white-space:pre-wrap}
    .ask .qs b{font-size:11px;color:var(--k-muted);margin-right:6px}
    .ask .th{flex:1;min-height:0;overflow:auto;padding:2px 14px 8px;overscroll-behavior:contain;user-select:text;-webkit-user-select:text}.ask .th:empty{display:none}
    .ask .tq{margin:12px 0 4px;font-weight:700;font-size:14px;line-height:1.5}
    .ask .tq i{font-style:normal;font-size:11px;padding:1px 7px;border-radius:6px;background:var(--k-acc-soft);color:var(--k-acc);margin-right:6px;vertical-align:1px}
    .ask .tq i.ed{background:color-mix(in srgb,var(--k-warn) 16%,transparent);color:var(--k-warn)}
    .ask .tl,.card .tl{font:11.5px/1.5 ui-monospace,Menlo,monospace;color:var(--k-muted);margin:1px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .ask .ta *{font-size:14.5px;line-height:1.65}.ask .ta p{margin:6px 0}.ask .ta ul,.ask .ta ol{margin:4px 0 8px;padding-left:1.3em}
    .ask .ta h2{font-size:15px;margin:10px 0 4px}.ask .ta h2 .n{display:none}.ask .ta code{font-family:ui-monospace,Menlo,monospace;font-size:13px;background:var(--k-box);padding:1px 4px;border-radius:4px}
    .ask .ta .mm{margin:10px 0;padding:10px;background:var(--k-bg);border:1px solid var(--k-line);border-radius:10px;overflow-x:auto;text-align:center;min-height:40px}
    .ask .ta .mm:empty::before{content:'그림 그리는 중…';color:var(--k-muted);font-size:13px}.ask .ta .mm svg{max-width:100%;height:auto}.ask .ta .mm svg *{font:inherit}
    .ask .ta a{color:var(--k-acc)}.ask .ta table{border-collapse:collapse;margin:6px 0}.ask .ta td,.ask .ta th{border:1px solid var(--k-line);padding:4px 8px;font-size:13px}
    .ask .cur::after{content:'▍';color:var(--k-acc);animation:bl 1s steps(2) infinite}
    .ask .ok{margin:8px 0;padding:8px 10px;border-radius:8px;background:color-mix(in srgb,var(--k-ok) 16%,transparent);font-size:13.5px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
    .ask .ok button{background:var(--k-ok);border-color:var(--k-ok);color:#fff;font-weight:700}
    .ask form{border-top:1px solid var(--k-line);padding:10px 12px;display:flex;flex-direction:column;gap:8px;background:var(--k-paper)}
    .ask textarea{width:100%;resize:vertical;min-height:54px;max-height:30vh;background:var(--k-bg);color:var(--k-ink);border:1px solid var(--k-line);border-radius:9px;padding:8px 10px;font-size:14px;line-height:1.5}
    .ask textarea:focus{outline:2px solid color-mix(in srgb,var(--k-acc) 50%,transparent);border-color:var(--k-acc)}
    .ask .row2{display:flex;align-items:center;gap:8px}.ask .hint{flex:1;font-size:11.5px;color:var(--k-muted)}
    .ask .seg{display:inline-flex;border:1px solid var(--k-line);border-radius:8px;overflow:hidden}
    .ask .seg button{border:0;border-radius:0;padding:5px 11px;color:var(--k-muted);background:none}
    .ask .seg button.on{background:var(--k-acc);color:var(--k-paper);font-weight:700}.ask .seg button[data-m="edit"].on{background:var(--k-warn)}
    .ask button.p{background:var(--k-acc);border-color:var(--k-acc);color:var(--k-paper);font-weight:700;padding:5px 14px}
    .ask.edit button.p{background:var(--k-warn);border-color:var(--k-warn)}.ask button:disabled{opacity:.6}
    .card .q small{font-size:12px;color:var(--k-muted);font-weight:600}
    .ask{z-index:1}.ask.focus{z-index:3}.ask .ms{display:none}
    /* 겹침 순서: 문서 위 질문 창(1~3)·표시 < 카드(20) < 하단 탭·기록(21) < 카드 안에서 연 질문 창(25) < 칩·알림(30) */
    .ov{z-index:20}.dock,.hb,.hp{z-index:21}.ask.oncard,.work.oncard{z-index:25}.chip,.toast,.jump{z-index:30}
    .ask.mini{cursor:pointer;box-shadow:0 6px 18px #0004}.ask.mini .qs,.ask.mini .th,.ask.mini form{display:none}
    .ask.mini header{border-bottom:0}.ask.mini .si{display:none}.ask.mini .ms{display:block;flex:1;min-width:0;font-size:12px;color:var(--k-ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .ask.mini:hover{outline:2px solid color-mix(in srgb,var(--k-acc) 50%,transparent)}
    .ask.mv{transition:left .32s cubic-bezier(.2,.8,.2,1),top .32s cubic-bezier(.2,.8,.2,1),bottom .32s cubic-bezier(.2,.8,.2,1),width .32s ease,max-height .32s ease}
    .ask.side{width:min(380px,calc(100vw - 24px))}.ask.side .qs{max-height:2.9em;font-size:12px}.ask.side textarea{min-height:40px}.ask.side .hint{display:none}
    .ask.side .ta *{font-size:13.5px}.ask.side header b{font-size:12.5px}
    .ask .hb2{border:0;background:none;color:var(--k-muted);font-size:12px;padding:2px 7px;border-radius:6px;white-space:nowrap}.ask .hb2:hover{background:var(--k-line);color:var(--k-ink)}
    .ask .live{display:flex;align-items:center;gap:7px;color:var(--k-acc);font-weight:700;margin:4px 0 2px}.ask .live .tm{color:var(--k-muted);font-weight:400}
    .ask .live .sp{flex:0 0 10px;width:10px;height:10px;border:2px solid var(--k-acc);border-right-color:transparent;border-radius:50%;animation:spin .8s linear infinite}
    .ask .live.end{color:var(--k-muted)}.ask .ok button.lnk{background:none;border:0;color:var(--k-muted);text-decoration:underline;font-weight:400}
    .work{position:fixed;pointer-events:none;border:2px dashed var(--ac);border-radius:10px;box-shadow:0 0 0 4px color-mix(in srgb,var(--ac) 20%,transparent);animation:wpulse 1.4s ease-in-out infinite}
    .work b{position:absolute;top:-25px;left:-2px;background:var(--ac);color:#fff;font-size:11.5px;font-weight:700;padding:3px 9px;border-radius:8px;white-space:nowrap}
    @keyframes wpulse{50%{box-shadow:0 0 0 10px color-mix(in srgb,var(--ac) 6%,transparent)}}
    .jump{position:fixed;left:50%;bottom:58px;transform:translateX(-50%);background:#f5b301;color:#1d1d1f;border:0;border-radius:16px;padding:7px 15px;font-weight:800;font-size:12.5px;box-shadow:0 4px 16px #0004;cursor:pointer}
    .ov{position:fixed;inset:0;background:#0009;display:flex;align-items:center;justify-content:center;padding:3vh 3vw 46px}
    .dock{position:fixed;right:62px;left:62px;bottom:0;display:flex;flex-direction:row-reverse;gap:6px;overflow-x:auto;scrollbar-width:none;pointer-events:none}
    .dock .tb{pointer-events:auto;flex:0 0 auto;display:flex;align-items:center;gap:7px;max-width:240px;height:38px;padding:0 4px 0 12px;background:var(--bg);color:var(--fg);
      border:1px solid var(--ln);border-bottom:0;border-radius:11px 11px 0 0;box-shadow:0 -3px 14px #0003;cursor:pointer}
    .dock .tb:hover{filter:brightness(.97)}
    .dock .tb b{font-size:13.5px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;color:inherit}
    .dock .tb small{font-size:11.5px;color:var(--mu);white-space:nowrap;font-variant-numeric:tabular-nums}
    .dock .tb i{flex:0 0 10px;width:10px;height:10px;border-radius:5px;background:var(--mu);opacity:.5}
    .dock .run i{opacity:1;background:none;border:2px solid var(--ac);border-right-color:transparent;animation:spin .8s linear infinite}
    .dock .done i{opacity:1;background:#2fa565}.dock .fail i{opacity:1;background:#d24b4b}
    .dock .new{border-color:var(--ac);box-shadow:inset 0 3px 0 var(--ac),0 -3px 14px #0003;animation:pop .5s ease 3}.dock .new small{color:var(--ac);font-weight:700}
    .dock .on{background:var(--ac);border-color:var(--ac);color:#fff}.dock .on small{color:#fffc}.dock .on.run i{border-color:#fff;border-right-color:transparent}.dock .on.done i{background:#fff}
    .dock .dx{border:0;padding:2px 6px;color:inherit;opacity:.55;font-size:16px;line-height:1}.dock .dx:hover{opacity:1}
    @keyframes spin{to{transform:rotate(360deg)}}@keyframes pop{50%{transform:translateY(-4px)}}
    .toast.up{bottom:52px}
    .card .failbox{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;margin:6px 0 16px;padding:14px 18px;border-radius:12px;border:1px solid var(--k-warn);border-left-width:5px;background:var(--k-box)}
    .card .failbox b{font-size:15px}.card .failbox span{flex:1 1 260px;font-size:13.5px;color:var(--k-muted)}
    .card .failbox button{background:var(--k-acc);border-color:var(--k-acc);color:var(--k-paper);font-weight:700;padding:6px 14px}
    .ov.full{padding:0 0 42px;background:#000d}
    .ov.full .card{width:100%;height:100%;max-height:100%;border-radius:0;border:0;box-shadow:none}
    .ov.full .card .sc{padding:34px max(40px,calc((100% - 1180px) / 2)) 80px}
    .ov.full .card .bd .sx{padding:24px 30px 14px}.ov.full .card .gal{grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}.ov.full .card figure img{height:190px}
    .ov.full .card form{padding:12px max(16px,calc((100% - 1180px) / 2))}
    .card .toc{display:none}
    @media (min-width:1500px){.ov.full .card .sc{padding-left:max(300px,calc((100% - 1180px) / 2));padding-right:max(48px,calc((100% - 1180px) / 2))}
      .ov.full .card .toc{display:block;position:absolute;left:22px;top:70px;bottom:80px;width:240px;overflow:auto;padding:6px 0;border-right:1px solid var(--k-line)}}
    .card .toc a{display:block;padding:6px 14px 6px 12px;margin:0 10px 2px 0;border-radius:8px;font-size:13.5px;line-height:1.4;color:var(--k-muted);cursor:pointer;text-decoration:none;word-break:keep-all}
    .card .toc a:hover{background:var(--k-box);color:var(--k-ink)}.card .toc a.on{background:var(--k-acc-soft);color:var(--k-ink);font-weight:700;box-shadow:inset 3px 0 var(--k-acc)}
    .card .toc a i{font-style:normal;color:var(--k-acc);font-weight:700;margin-right:6px;font-variant-numeric:tabular-nums}
    .card .toc b{display:block;padding:0 12px 8px;font-size:12px;color:var(--k-muted);letter-spacing:.04em}
    .hb{position:fixed;left:14px;bottom:14px;height:38px;min-width:38px;padding:0 12px;border-radius:19px;border:1px solid var(--ln);background:var(--bg);color:var(--fg);
      opacity:.7;cursor:pointer;box-shadow:0 2px 8px #0002;font-size:13px;font-weight:700}.hb:hover,.hb.on{opacity:1;border-color:var(--ac)}
    .hp{position:fixed;left:14px;bottom:60px;width:min(380px,calc(100vw - 28px));max-height:72vh;display:flex;flex-direction:column;background:var(--bg);color:var(--fg);
      border:1px solid var(--ln);border-radius:12px;box-shadow:0 8px 28px #0004;overflow:hidden}
    .hp header{padding:10px 12px 8px;border-bottom:1px solid var(--ln)}.hp header b{font-size:14px}
    .hp .hsc{float:right;display:inline-flex;border:1px solid var(--ln);border-radius:7px;overflow:hidden}.hp .hsc button{border:0;border-radius:0;padding:2px 9px;font-size:12px;color:var(--mu)}
    .hp .hsc button.on{background:var(--ac);color:#fff}
    .hp input{width:100%;margin-top:8px;background:none;color:var(--fg);border:1px solid var(--ln);border-radius:8px;padding:7px 10px;font-size:13px}
    .hp .ls{overflow:auto;overscroll-behavior:contain}
    .hp .it{display:block;width:100%;text-align:left;border:0;border-bottom:1px solid var(--ln);border-radius:0;padding:8px 12px}
    .hp .it:hover{background:color-mix(in srgb,var(--ac) 12%,transparent)}.hp .it b{font-size:14px;font-weight:700}
    .hp .it small{display:block;color:var(--mu);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .hp .it.here b::after{content:' · 이 문서';color:var(--ac);font-size:11.5px;font-weight:600}
    .card{--k-bg:#f7f6f2;--k-paper:#fff;--k-ink:#1d2330;--k-muted:#5b6475;--k-line:#d9dce3;--k-acc:#1f5fbf;--k-acc-soft:#e3edfb;--k-box:#eef1f6;
      --k-wim:#7a3fd0;--k-wim-soft:#efe6fb;--k-code:#f0f2f5;--k-font:"Pretendard","Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif;
      width:min(1120px,100%);height:min(94vh,1240px);max-height:100%;display:flex;flex-direction:column;background:var(--k-bg);color:var(--k-ink);
      border:1px solid var(--k-line);border-radius:16px;box-shadow:0 24px 70px #0007;overflow:hidden;position:relative}
    .card,.card *{font-family:var(--k-font)}
    .card header{display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:1px solid var(--k-line);background:var(--k-paper)}
    .card header .sp{flex:1}.card .tag{font-size:12px;color:var(--k-muted)}
    .card button{color:var(--k-ink);border-color:var(--k-line);background:var(--k-paper);font-size:13px}
    .card .x{border:0;font-size:22px;line-height:1;color:var(--k-muted);padding:0 6px;background:none}
    .card .sc{flex:1;overflow:auto;overscroll-behavior:contain;padding:26px 40px 56px}
    .card h1{font-size:27px;line-height:1.35;margin:0 0 14px;font-weight:700;word-break:keep-all}
    .card h1 .num{display:inline-block;background:var(--k-acc);color:var(--k-paper);border-radius:8px;padding:0 10px;margin-right:10px;font-size:17px;line-height:1.6;vertical-align:middle;font-weight:700}
    .card h1 .tt{font-size:27px;font-weight:700}
    .card .ctx{border-radius:12px;padding:10px 16px;margin:0 0 18px;border-left:5px solid var(--k-acc);background:var(--k-acc-soft);font-size:14.5px;line-height:1.7}
    .card .ctx small{display:block;font-size:12px;color:var(--k-muted);font-weight:600;margin-bottom:2px}
    .card mark{background:color-mix(in srgb,#f5b301 38%,transparent);color:inherit;border-radius:3px;padding:0 2px;font-size:inherit;box-shadow:inset 0 -2px #f5b301}
    .card .bd{user-select:text;-webkit-user-select:text}
    .card .bd *{font-size:16px;line-height:1.7;word-break:keep-all;overflow-wrap:anywhere}
    .card .bd .sx{background:var(--k-paper);border:1px solid var(--k-line);border-radius:14px;padding:20px 24px 12px;margin:0 0 16px}
    .card .bd h2{font-size:19.5px;font-weight:800;margin:0 0 6px;line-height:1.4;letter-spacing:-.01em}
    .card .bd h2 .n{color:var(--k-acc);margin-right:9px;font-variant-numeric:tabular-nums;font-size:19.5px;font-weight:800}
    .card .bd p.lead{font-weight:600;margin:0 0 12px}
    .card .bd a{color:var(--k-acc);text-decoration:underline;text-underline-offset:2px}
    .card .bd .kc{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:12px 0 10px}
    .card .bd .kc div{background:var(--k-bg);border:1px solid var(--k-line);border-left:5px solid var(--c,var(--k-acc));border-radius:12px;padding:11px 14px}
    .card .bd .kc i{display:block;font-style:normal;font-size:12px;font-weight:800;letter-spacing:.04em;color:var(--c,var(--k-acc))}
    .card .bd .kc b{display:block;font-size:21px;font-weight:800;line-height:1.3;letter-spacing:-.02em;margin:2px 0}
    .card .bd .kc small{display:block;font-size:13px;line-height:1.5;color:var(--k-muted)}
    .card .kc .green{--c:var(--k-ok)}.card .kc .blue{--c:var(--k-acc)}.card .kc .amber{--c:var(--k-amber)}.card .kc .red{--c:var(--k-warn)}.card .kc .gray{--c:var(--k-muted)}
    .card .bd li.ck-ok::marker{content:'✓  ';color:var(--k-ok)}.card .bd li.ck-fix{color:var(--k-ink)}.card .bd li.ck-fix::marker{content:'✎  ';color:var(--k-acc)}
    .card .bd li.ck-star::marker{content:'★  ';color:var(--k-amber)}
    .card .sx[data-h="검증"],.card .sx[data-h="출처"]{background:var(--k-box)}
    .card .sx[data-h="검증"] li,.card .sx[data-h="출처"] li{font-size:14px;line-height:1.6;margin:0 0 4px}
    .card .sx[data-h="검증"] p{font-weight:700;font-size:14.5px}
    .card .prog{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px;margin:0 0 16px}
    .card .prog div{background:var(--k-paper);border:1px solid var(--k-line);border-left:5px solid var(--k-line);border-radius:10px;padding:8px 12px}
    .card .prog b{display:block;font-size:13.5px;font-weight:800}.card .prog small{display:block;font-size:12.5px;color:var(--k-muted)}
    .card .prog .run{border-left-color:var(--k-acc)}.card .prog .run small::after{content:' …';animation:bl 1s steps(2) infinite}
    .card .prog .done{border-left-color:var(--k-ok)}.card .prog .fail{border-left-color:var(--k-warn)}
    .card .sx[data-h*="한마디"] p,.card .sx[data-h*="정의"] p{font-size:18.5px;line-height:1.65;font-weight:500}.card .sx[data-h*="한마디"] strong{color:var(--k-acc)}
    .card .sx[data-h*="차근"] ol{list-style:none;counter-reset:st;padding:0;margin:6px 0 10px}
    .card .sx[data-h*="차근"] li{counter-increment:st;position:relative;padding:10px 14px 10px 52px;margin:0 0 8px;background:var(--k-bg);border:1px solid var(--k-line);border-radius:10px}
    .card .sx[data-h*="차근"] li::before{content:counter(st);position:absolute;left:14px;top:10px;width:26px;height:26px;border-radius:13px;background:var(--k-acc);color:var(--k-paper);
      font-size:14px;font-weight:700;line-height:26px;text-align:center}
    .card .bd ol{padding-left:1.4em;margin:0 0 10px}
    .card .sx[data-h*="다시 읽기"] p{border-radius:12px;padding:10px 16px;margin:10px 0;border-left:5px solid var(--k-acc);background:var(--k-acc-soft)}
    .card .bd p{margin:0 0 10px}.card .bd ul{margin:0 0 10px;padding-left:1.3em}.card .bd strong{font-weight:700}
    .card .bd code{font-family:ui-monospace,Menlo,monospace;font-size:14px;background:var(--k-code);padding:1px 5px;border-radius:4px}
    .card .sx[data-h*="예를"] p,.card .sx[data-h*="비유"] p{border-radius:12px;padding:10px 16px;margin:10px 0;border-left:5px solid var(--k-wim);background:var(--k-wim-soft)}
    .card .bd table{border-collapse:collapse;width:100%;margin:10px 0 16px;background:var(--k-paper);border:1px solid var(--k-line);border-radius:12px;overflow:hidden}
    .card .bd th,.card .bd td{border-bottom:1px solid var(--k-line);padding:9px 12px;text-align:left;vertical-align:top;font-size:14.5px;line-height:1.6}
    .card .bd th{background:var(--k-box);font-weight:700}.card .bd td:first-child{font-weight:600}
    .card .mm{margin:14px 0 12px;padding:16px;background:var(--k-bg);border:1px solid var(--k-line);border-radius:12px;overflow-x:auto;text-align:center}
    .card .mm.wait,.card .mm:empty{color:var(--k-muted);font-size:13px;padding:44px}
    .card .mm svg{max-width:100%;height:auto}.card .mm svg *{font:inherit}
    .card .mmerr{text-align:left;font:12px/1.5 ui-monospace,Menlo,monospace;color:var(--k-muted);white-space:pre-wrap}
    .card .sx[data-h*="같이 볼"] ul{list-style:none;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
    .card .sx[data-h*="같이 볼"] li{background:var(--k-bg);border:1px solid var(--k-line);border-radius:10px;padding:10px 14px}
    .card .sx[data-h*="같이 볼"] li small{display:block;color:var(--k-muted);font-size:13.5px;line-height:1.55}
    .card .rel{color:var(--k-acc);font-weight:700;cursor:pointer}.card .rel:hover{text-decoration:underline}
    .card .q{color:var(--k-acc);font-weight:700;font-size:17px;margin:48px 0 0;padding-top:16px;border-top:3px solid var(--k-acc)}
    .card .cur::after{content:'▍';color:var(--k-acc);animation:bl 1s steps(2) infinite}
    @keyframes bl{50%{opacity:0}}
    .card .gal{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px;margin:0 0 26px}
    .card .gal h5{grid-column:1/-1;margin:0;font-size:12.5px;font-weight:600;color:var(--k-muted)}
    .card figure{margin:0;padding:8px;background:var(--k-paper);border:1px solid var(--k-line);border-radius:12px;cursor:zoom-in;display:flex;flex-direction:column}
    .card figure img{width:100%;height:150px;object-fit:contain;background:#fff;border-radius:6px;display:block}
    .card figcaption{font-size:12.5px;line-height:1.45;margin-top:6px;color:var(--k-muted);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
    .card .sk{height:200px;border-radius:12px;background:linear-gradient(90deg,var(--k-line),var(--k-paper),var(--k-line));background-size:200% 100%;animation:sh 1.3s infinite linear}
    @keyframes sh{to{background-position:-200% 0}}
    .card form{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--k-line);background:var(--k-paper)}
    .card input{flex:1;min-width:0;background:var(--k-bg);color:var(--k-ink);border:1px solid var(--k-line);border-radius:9px;padding:9px 12px;font-size:14px}
    .card .lb{position:absolute;inset:0;background:#000e;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:24px;cursor:zoom-out}
    .card .lb img{max-width:100%;max-height:78%;background:#fff;border-radius:6px}
    .card .lb div{color:#ddd;max-width:760px;text-align:center}.card .lb a{color:#9cf}
    @media (max-width:600px){.ov{padding:0 0 40px}.card{height:100%;border-radius:0;border:0}.card .sc{padding:16px 16px 40px}.card h1,.card h1 .tt{font-size:22px}
      .card .gal{grid-template-columns:repeat(2,minmax(0,1fr))}.card figure img{height:110px}}
  </style>
  <div class="rg"><i></i></div><div class="rl" title="문서 지도: 초록 읽음 · 회색 안 읽음 · 노랑 바뀐 곳 (누르면 이동, n 다음 안 읽은 곳)"></div>
  <div class="toast" hidden></div>
  <div class="pan" hidden></div>
  <div class="ov" hidden><div class="card"><header><button data-x="back" title="이전 카드" hidden>← 이전</button><span class="sp"></span><span class="tag"></span>
    <button data-x="deep" title="동작 원리·수치·문서 주제와의 관계까지">자세히</button><button data-x="re" title="저장된 카드 말고 새로 쓰기">다시</button>
    <button data-x="full" title="전체화면 (f) · 넓은 화면에선 왼쪽에 목차">⤢ 전체화면</button><button data-x="min" title="하단 탭으로 내리기 (Esc) · 만드는 동안 계속 읽고, 끝나면 탭이 알려 줌">▾ 내리기</button><button class="x" data-x="close" title="하단 탭으로 내리기 (Esc)">×</button></header>
    <nav class="toc"></nav><div class="sc"></div>
    <form><input placeholder="더 물어보기 (예: 그럼 이 문서의 경우는?)" enterkeyhint="send"></form><div class="lb" hidden></div></div></div>
  <div class="dock" hidden></div>
  <div class="work" hidden><b></b></div><button class="jump" hidden></button>
  <button class="hb" title="설명 카드 기록 — 지금까지 만든 카드 다시 열기">카드</button>
  <div class="hp" hidden><header><b>설명 카드 기록</b> <span class="mu cnt"></span><span class="hsc"><button data-h="doc" class="on">이 문서</button><button data-h="all">전체</button></span><input placeholder="용어·문서 검색"></header><div class="ls"></div></div>
  <div class="chip" hidden><button data-c="e" title="개념 카드 (e)">설명</button><button data-c="a" title="이 문서를 만든 세션에 질문·수정 요청 (a)">질문·수정</button></div>
  <div class="ask" hidden><header><b>만든 세션에 묻기</b><small class="si"></small><small class="ms"></small><button class="hb2" data-q="side" title="옆으로 작게 (본문 보면서)">옆으로 ▸</button><button class="hb2" data-q="flip" title="반대쪽으로" hidden>⇄</button><button class="hb2" data-q="float" title="고른 곳 옆에 크게" hidden>⤢</button><button class="hb2" data-q="mini" title="접기" hidden>–</button><button class="x" data-q="x" title="닫기 (Esc) · 돌고 있으면 접힘">×</button></header>
    <div class="qs"></div><div class="th"></div>
    <form><textarea rows="2"></textarea><div class="row2"><span class="seg"><button type="button" data-m="ask" class="on">질문</button><button type="button" data-m="edit">HTML 수정</button></span>
      <span class="hint">Enter 보내기 · Shift+Enter 줄바꿈</span><button class="p" type="submit">보내기</button></div></form></div>
  <button class="fab" title="읽기 기록 (b 책갈피 · m 메모 · [ ] 이동 · d 읽음 · 단어 선택 후 e 설명)">0%</button>`;
  const $ = s => R.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

  let D = null, M = [], visit = null, dirty = false, active = 0, lastAct = Date.now(), restored = false, userMoved = false;
  const post = (p, b) => fetch(API + p, {method: 'POST', body: JSON.stringify(b), keepalive: true}).then(r => r.json());
  const body = extra => ({k: K, ratio: ratio(), anchor: anchor(), device: DEV, visit, active: Math.round(active), ...rbBody(), ...extra});
  const take = j => { if (j.doc) D = j.doc; if (j.marks) M = j.marks; if (j.visit) visit = j.visit; if (j.blocks) rbLoad(j.blocks); paint(); };
  const flush = beacon => {
    const b = body(); active = 0; dirty = false;
    if (beacon && navigator.sendBeacon) navigator.sendBeacon(API + 'doc', JSON.stringify(b)); else post('doc', b).then(take).catch(() => {});
  };
  const ago = t => { if (!t) return ''; const s = Date.now() / 1000 - t;
    return s < 3600 ? Math.max(1, Math.round(s / 60)) + '분 전' : s < 86400 ? Math.round(s / 3600) + '시간 전' : Math.round(s / 86400) + '일 전'; };
  const pct = r => Math.round((r || 0) * 100) + '%';

  function paint() {
    const r = ratio();
    $('.fab').textContent = D?.done_at ? '✓' : pct(COV ? COV.pct : r);
    if (!$('.pan').hidden) drawPanel();
  }
  function drawPanel() {
    const p = $('.pan');
    p.innerHTML = `<h4>${esc(document.title || K)}</h4>
      <div class="mu">${COV ? `읽음 ${pct(COV.pct)} · 안 읽은 덩어리 ${COV.total - COV.read}개${COV.new ? ` (바뀐 곳 ${COV.new})` : ''}` : `지금 ${pct(ratio())}`} · ${D?.opens || 1}회 · ${Math.round((D?.active_sec || 0) / 60)}분${D?.done_at ? ' · 완료' : ''}</div>
      <div class="mu" style="font-size:12px">화면에 충분히 머문 덩어리만 읽음(초록)으로 칩니다. 고쳐진·새로 생긴 덩어리는 노랑.</div>
      <div class="row"><button data-a="nu">다음 안 읽은 곳 (n)</button><button data-a="pu">이전 (N)</button></div>
      <div class="row"><button class="p" data-a="b">책갈피 (b)</button><button data-a="m">메모 (m)</button>
        <button data-a="d">${D?.done_at ? '완료 취소' : '읽음 표시'} (d)</button></div>
      <div class="row mu">기기 <button data-a="dev">${esc(DEV)} ✎</button><a href="/" style="margin-left:auto">대시보드 →</a></div>
      ${M.length ? M.map(m => `<div class="mk" data-id="${m.id}"><span>${pct(m.ratio)}</span><span>${esc(m.note) || '<span class=mu>책갈피</span>'}
        <div class="mu">${esc(m.device)} · ${ago(m.created_at)}</div></span><button class="x" data-del="${m.id}">×</button></div>`).join('')
        : '<div class="mu">책갈피 없음</div>'}`;
  }
  const addMark = withNote => {
    const note = withNote ? prompt('메모') : '';
    if (withNote && note === null) return;
    post('mark', {k: K, ratio: ratio(), anchor: anchor(), note, device: DEV}).then(j => { take(j); toast('책갈피 저장 · ' + pct(ratio()), 1500); });
  };
  // ---------- 개념 카드(선택 → 칩 → claude 스트리밍: 글·mermaid 다이어그램·위키백과 그림) ----------
  const BLK = 'p,li,dd,dt,td,th,figcaption,h1,h2,h3,h4,h5,h6,pre,blockquote,caption,label,summary';
  const PH = '더 물어보기 (예: 그럼 이 문서의 경우는?)';
  let pick = null, X = null, MM = null, mmN = 0;
  const SVG = new Map(), hist = [];  // mermaid 코드 → svg(그리는 중이면 null)
  const clip = (t, term, n) => { t = (t || '').replace(/\s+/g, ' ').trim(); if (t.length <= n) return t;
    const i = Math.max(0, t.indexOf(term)); const a = Math.max(0, i - n / 2); return t.slice(a, a + n); };
  const headOf = el => { let h = ''; for (const x of el.ownerDocument.querySelectorAll('h1,h2,h3,h4')) {
    if (x.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING || x.contains(el)) h = txt(x); else break; } return h; };
  const inl = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // 마크다운 일부(제목·목록·표·mermaid)만 그린다. 덜 끝난 mermaid는 자리표시
  const md = s => {
    const out = []; let ul = false, tb = null, sec = '', mm = null, open = false, kc = null, no = 0, lead = false;
    const endUl = () => { if (ul) { out.push(ul === 'ol' ? '</ol>' : '</ul>'); ul = false; } };
    const endTb = () => { if (tb) { out.push('<table>' + tb.map((r, i) => '<tr>' + r.map(c => i ? `<td>${inl(c)}</td>` : `<th>${inl(c)}</th>`).join('') + '</tr>').join('') + '</table>'); tb = null; } };
    for (const raw of s.split('\n')) {
      if (mm) { if (/^\s*```/.test(raw)) { out.push(`<div class="mm" data-c="${esc(mm.join('\n'))}"></div>`); mm = null; } else mm.push(raw); continue; }
      if (kc) { if (/^\s*```/.test(raw)) { out.push('<div class="kc">' + kc.join('') + '</div>'); kc = null; continue; }
        const f = raw.split('::').map(x => x.trim()); if (f[0]) kc.push(`<div class="${/^(green|blue|amber|red|gray)$/.test(f[3] || '') ? f[3] : 'blue'}"><i>${inl(f[1] || '')}</i><b>${inl(f[0])}</b><small>${inl(f[2] || '')}</small></div>`); continue; }
      if (/^\s*```\s*cards/.test(raw)) { endUl(); endTb(); kc = []; continue; }
      if (/^\s*```\s*mermaid/.test(raw)) { endUl(); endTb(); mm = []; continue; }
      if (/^\s*```/.test(raw) || /^IMG:/.test(raw)) continue;
      if (/^\s*\|/.test(raw)) { endUl(); const cells = raw.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
        if (!cells.every(c => /^:?-{2,}:?$/.test(c))) (tb = tb || []).push(cells); continue; }
      endTb();
      const h = raw.match(/^#{1,4}\s+(.*)/);
      if (h) { endUl(); sec = h[1].trim(); no++; lead = !/한마디|출처|검증|같이 볼/.test(sec);
        out.push((open ? '</div>' : '') + `<div class="sx" data-h="${esc(sec)}"><h2><span class="n">${String(no).padStart(2, '0')}</span>${inl(sec)}</h2>`); open = true; continue; }
      const oli = raw.match(/^\s*\d+[.)]\s+(.*)/);
      if (oli) { if (ul && ul !== 'ol') endUl(); if (!ul) { out.push('<ol>'); ul = 'ol'; } out.push('<li>' + inl(oli[1]) + '</li>'); continue; }
      const li = raw.match(/^\s*[-•*]\s+(.*)/);
      if (li) { if (ul === 'ol') endUl(); if (!ul) { out.push('<ul>'); ul = true; }
        const m = /같이 볼/.test(sec) && li[1].match(/^\**([^:：*]+?)\**\s*[:：]\s*(.*)$/);
        const ck = sec === '검증' ? (/^확인됨/.test(li[1]) ? ' class="ck-ok"' : /^수정됨/.test(li[1]) ? ' class="ck-fix"' : ' class="ck-star"') : '';
        out.push(`<li${ck}>` + (m ? `<a class="rel" data-t="${esc(m[1].trim())}">${esc(m[1].trim())}</a><small>${inl(m[2])}</small>` : inl(li[1])) + '</li>'); lead = false; continue; }
      endUl(); if (raw.trim()) { out.push((lead ? '<p class="lead">' : '<p>') + inl(raw) + '</p>'); lead = false; }
    }
    endUl(); endTb();
    if (mm) out.push('<div class="mm wait">다이어그램 그리는 중…</div>');
    if (kc) out.push('<div class="kc">' + kc.join('') + '</div>');
    if (open) out.push('</div>');
    return out.join('');
  };
  // 문서 색은 바탕·종이·글자·강조 4개만 읽고 나머지는 그 넷을 섞어 만든다. 문서마다 변수 이름이 달라
  // 일부만 읽히면(예: 다크 문서 글자 + 라이트 기본 상자) 흰 바탕에 흰 글씨가 된다.
  const TOK = {bg: ['--bg', '--background'], paper: ['--paper', '--card', '--surface', '--panel'], ink: ['--ink', '--fg', '--text'],
    acc: ['--acc', '--accent', '--ac', '--primary', '--blue']};
  const PAL = {light: {bg: [247, 246, 242], paper: [255, 255, 255], ink: [29, 35, 48], acc: [31, 95, 191]},
    dark: {bg: [21, 23, 27], paper: [30, 33, 38], ink: [232, 233, 236], acc: [110, 160, 255]}};
  let KT = {};
  const probe = v => { if (!v) return null; const d = document.createElement('i'); d.style.color = v; R.append(d);
    const m = getComputedStyle(d).color.match(/[\d.]+/g); d.remove();
    return m && m.length >= 3 && (m.length < 4 || +m[3] > 0.5) ? m.slice(0, 3).map(Number) : null; };
  const lum = c => { const f = x => (x /= 255) <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; return .2126 * f(c[0]) + .7152 * f(c[1]) + .0722 * f(c[2]); };
  const con = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); };
  const mix = (a, b, t) => a.map((v, i) => Math.round(v * (1 - t) + b[i] * t));
  const rgb = c => `rgb(${c.join(',')})`;
  const theme = () => {
    const cs = getComputedStyle(document.documentElement), bs = getComputedStyle(document.body), c = $('.card');
    const get = k => { for (const n of TOK[k]) { const v = probe(cs.getPropertyValue(n).trim() || bs.getPropertyValue(n).trim()); if (v) return v; } return null; };
    let bg = get('bg') || probe(bs.backgroundColor) || probe(cs.backgroundColor), ink = get('ink') || probe(bs.color);
    const D = bg ? lum(bg) < 0.2 : matchMedia('(prefers-color-scheme: dark)').matches, P = D ? PAL.dark : PAL.light;
    if (!bg || !ink || con(ink, bg) < 4.5) { bg = P.bg; ink = P.ink; }
    let paper = get('paper'); if (!paper || con(ink, paper) < 4.5 || (lum(paper) < 0.2) !== D) paper = D ? mix(bg, [255, 255, 255], .06) : [255, 255, 255];
    let acc = get('acc'); if (!acc || con(acc, paper) < 3) acc = P.acc;
    const wim = D ? [169, 139, 240] : [122, 63, 208];
    KT = {dark: D, bg: rgb(bg), paper: rgb(paper), ink: rgb(ink), acc: rgb(acc), muted: rgb(mix(ink, paper, .38)), line: rgb(mix(ink, paper, .84)),
      'acc-soft': rgb(mix(acc, paper, .84)), box: rgb(mix(ink, paper, .94)), code: rgb(mix(ink, paper, .9)), wim: rgb(wim), 'wim-soft': rgb(mix(wim, paper, .85)),
      ok: rgb(D ? [92, 194, 138] : [44, 138, 85]), warn: rgb(D ? [239, 123, 123] : [196, 61, 61]), amber: rgb(D ? [227, 169, 79] : [170, 110, 20])};
    c.removeAttribute('style');
    for (const [k, v] of Object.entries(KT)) if (k !== 'dark') { c.style.setProperty('--k-' + k, v); $('.ask').style.setProperty('--k-' + k, v); }
    if (bs.fontFamily && !/^(-apple-system|system-ui|sans-serif)/.test(bs.fontFamily)) { c.style.setProperty('--k-font', bs.fontFamily); $('.ask').style.setProperty('--k-font', bs.fontFamily); }
  };
  const dark = () => KT.dark ?? matchMedia('(prefers-color-scheme: dark)').matches;
  const kv = n => getComputedStyle($('.card')).getPropertyValue('--k-' + n).trim();
  const loadMM = () => MM || (MM = new Promise((ok, no) => {
    if (window.mermaid) return ok(window.mermaid);
    const sc = document.createElement('script'); sc.src = '/__reader/mermaid.js'; sc.onload = () => ok(window.mermaid); sc.onerror = no; document.head.append(sc);
  }));
  const mmConf = () => ({startOnLoad: false, securityLevel: 'strict', theme: 'base', fontFamily: kv('font'),
    flowchart: {htmlLabels: false, useMaxWidth: true, curve: 'basis', padding: 12},
    themeVariables: {darkMode: dark(), background: kv('paper'), fontFamily: kv('font'), fontSize: '15px',
      primaryColor: kv('acc-soft'), primaryBorderColor: kv('acc'), primaryTextColor: kv('ink'), textColor: kv('ink'),
      secondaryColor: kv('box'), tertiaryColor: kv('box'), lineColor: kv('muted'), clusterBkg: kv('box'), clusterBorder: kv('line'),
      titleColor: kv('ink'), edgeLabelBackground: kv('paper'), nodeTextColor: kv('ink')}});
  const fillMM = root => root.querySelectorAll('.mm[data-c]').forEach(async el => {
    const code = el.dataset.c, key = dark() + code;
    if (SVG.get(key)) { el.innerHTML = SVG.get(key); return; }
    if (SVG.has(key)) return;
    SVG.set(key, null);
    try { const m = await loadMM(); m.initialize(mmConf()); SVG.set(key, (await m.render('wimmm' + (++mmN), code)).svg); }
    catch (e) { SVG.set(key, `<pre class="mmerr">다이어그램을 못 그렸다. 원본:\n${esc(code)}</pre>`);
      document.querySelectorAll('[id^="dwimmm"],[id^="wimmm"]').forEach(x => { if (!x.closest('#__wim_reader')) x.remove(); }); }
    SS.forEach(s => s.bd.querySelectorAll('.mm[data-c]').forEach(x => { if (x.dataset.c === code) x.innerHTML = SVG.get(key); }));
    PANES.forEach(P => P.el.querySelectorAll('.mm[data-c]').forEach(x => { if (x.dataset.c === code) x.innerHTML = SVG.get(key); }));  // 질문·수정 창의 그림도
  });
  const loadImgs = (line, gal) => {
    const ts = line.split('|').map(x => x.trim()).filter(Boolean);
    fetch(API + 'images?t=' + encodeURIComponent(ts.join('|'))).then(r => r.json()).then(j => {
      gal._items = j.items || [];
      gal.innerHTML = gal._items.length ? '<h5>위키백과 그림 · 누르면 크게</h5>' + gal._items.map((x, i) => `<figure data-i="${i}"><img src="${esc(x.src)}" loading="lazy" alt="">
        <figcaption>${esc(x.cap || x.file.replace(/^File:/, ''))}</figcaption></figure>`).join('')
        : `<h5>위키백과에 그림 없음 (${esc(ts.join(', '))})</h5>`;
    }).catch(() => { gal.innerHTML = ''; });
  };
  const innerPick = () => {
    const s = R.getSelection?.(); if (!s || s.isCollapsed || !s.rangeCount || !X) return null;
    const n = s.getRangeAt(0).commonAncestorContainer, el = n.nodeType === 1 ? n : n.parentElement;
    const term = s.toString().replace(/\s+/g, ' ').trim();
    if (!el?.closest?.('.bd') || !term || term.length > 80) return null;
    return {term, ctx: clip(X.last, term, 800), head: X.term + ' 카드 안에서', rect: s.getRangeAt(0).getBoundingClientRect(), inner: true};
  };
  // 합본 페이지는 본문이 srcdoc iframe 안에 있다. 선택·키 이벤트는 문서 경계를 넘지 않으므로 같은 출처 iframe마다 따로 본다
  const allDocs = (d = document, out = []) => { out.push(d);
    for (const f of d.querySelectorAll('iframe')) { try { if (f.contentDocument) allDocs(f.contentDocument, out); } catch {} }
    return out; };
  const toTop = (rect, doc) => { let x = 0, y = 0;  // iframe 안 좌표 → 바깥 화면 좌표(칩은 바깥에 있다)
    for (let w = doc.defaultView; w?.frameElement; w = w.parent) { const f = w.frameElement, b = f.getBoundingClientRect(); x += b.left + f.clientLeft; y += b.top + f.clientTop; }
    return {top: rect.top + y, bottom: rect.bottom + y, right: rect.right + x, left: rect.left + x}; };
  // 심층 카드에 보낼 문서 본문: 고른 곳을 감싸는 절(없으면 3,000자 넘을 때까지 위로), 그림 라벨은 ' · '로 잇고 7,000자 창으로 자른다
  const docText = (blk, node, term) => {
    const body = blk.ownerDocument.body; let root = blk.closest('section,article');
    if (!root || (root.innerText || '').length < 600) { root = blk; while (root.parentElement && root !== body && (root.innerText || '').length < 3000) root = root.parentElement; }
    const w = blk.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT); let n, out = '', at = -1;
    while ((n = w.nextNode())) { const p = n.parentElement; if (!p || p.closest('script,style,#__wim_reader')) continue;
      const t = n.data.replace(/\s+/g, ' ').trim(); if (!t) continue; if (at < 0 && n === node) at = out.length;
      out += (p.closest('svg') ? ' · ' : ' ') + t; if (out.length > 60000 && at >= 0) break; }
    if (out.length <= 7000) return out.trim(); const c = at >= 0 ? at : Math.max(0, out.indexOf(term)); return out.slice(Math.max(0, c - 3500), c + 3500).trim(); };
  const readPick = (doc = document) => {
    const sel = doc.getSelection(); if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const term = sel.toString().replace(/\s+/g, ' ').trim(); if (!term || term.length > 3000) return null;  // 80자 넘으면 질문만
    const r = sel.getRangeAt(0), n = r.commonAncestorContainer, el = n.nodeType === 1 ? n : n.parentElement;
    if (!el || el.closest('#__wim_reader')) return null;
    const sv = el.closest('svg');
    let blk, ctx;
    if (sv) {  // 그림 라벨을 고르면 절 전체 글이 뒤섞여 들어가던 것을: 절 제목·요점·그림 이름·캡션·근처 라벨만
      const fig = sv.closest('figure'), sec = sv.closest('section,article') || fig?.parentElement || sv.parentElement;
      const texts = [...sv.querySelectorAll('text')], me = el.closest('text'), i = Math.max(0, texts.indexOf(me));
      const near = texts.slice(Math.max(0, i - 4), i + 6).map(x => x.textContent.replace(/\s+/g, ' ').trim()).filter(Boolean).join(' · ');
      const h = sec?.querySelector('h1,h2,h3,h4'), lp = [...(sec?.querySelectorAll('p') || [])].find(x => !x.closest('svg'));
      ctx = [h && txt(h), lp && lp.innerText, sv.getAttribute('aria-label') && '그림: ' + sv.getAttribute('aria-label'),
        fig?.querySelector('figcaption')?.innerText, near && '그림 속 라벨: ' + near].filter(Boolean).map(x => x.replace(/\s+/g, ' ').trim()).join(' / ');
      ctx = clip(ctx, term, 1200); blk = sec || fig || sv;
    } else {
      blk = el.closest(BLK) || el.closest('section,article,div') || el;
      const row = blk.closest('tr');
      ctx = clip((row ? row.innerText : blk.innerText) || '', term, 1200);
      const th = blk.closest('table')?.querySelector('tr'); if (row && th && th !== row) ctx = '표 머리: ' + clip(th.innerText, '', 300) + ' / 행: ' + ctx;
    }
    return {term, ctx, head: headOf(blk), rect: toTop(r.getBoundingClientRect(), doc), odoc: doc, doc: docText(blk, r.startContainer, term), range: r.cloneRange()};
  };
  const anyPick = () => { for (const d of allDocs()) { const p = readPick(d); if (p) return p; } return null; };
  let st;
  const onSel = () => { clearTimeout(st); st = setTimeout(() => {
    pick = innerPick() || anyPick(); const c = $('.chip');
    if (!pick) { c.hidden = true; return; }
    c.querySelector('[data-c="e"]').hidden = pick.term.length > 80;
    c.hidden = false; const w = c.offsetWidth || 44;
    c.style.left = Math.min(innerWidth - w - 8, Math.max(8, pick.rect.right - w / 2)) + 'px';
    c.style.top = (pick.rect.bottom + 34 > innerHeight ? pick.rect.top - 30 : pick.rect.bottom + 6) + 'px';
  }, 220); };
  // 카드 하나가 세션 하나다. 내리면(▾·Esc·바깥 클릭) 하단 탭으로 들어가고 만드는 일은 계속된다(링크드인 채팅 탭처럼).
  // 탭을 누르면 다시 열린다. 다른 페이지·새로고침에도 남도록 최근 12개를 localStorage에 둔다(카드 본문은 서버 캐시에서 다시 받음)
  const DK = 'wimdock', DMAX = 12, SS = [];
  const setTag = (S, t) => { S.tag = t; if (X === S) $('.card .tag').textContent = t; };
  const setBusy = (S, on, ph) => { S.busy = on; S.ph = ph || PH; if (X === S) { const i = $('.card input'); i.disabled = on; i.placeholder = S.ph; } };
  // 하단 탭은 페이지마다 따로: 이 페이지에서 연 카드만 보인다(다른 페이지 것은 남겨 두고 덮어쓰지 않는다)
  const saveDock = () => { try { const all = JSON.parse(localStorage.getItem(DK) || '[]').filter(x => (x.page || x.k) !== K);
    localStorage.setItem(DK, JSON.stringify(all.concat(SS.slice(-DMAX).map(s => ({term: s.term, ctx: s.ctx, head: s.head, doc: s.doc,
      k: s.k, page: K, title: s.title, st: s.st === 'run' ? 'run' : 'idle'}))).slice(-300))); } catch {} };
  const session = x => {
    const S = {term: x.term, ctx: x.ctx || '', head: x.head || '', doc: x.doc || '', k: x.k || K, title: x.title || document.title,
      last: '', st: 'idle', tag: '', busy: false, ph: PH, top: 0, t0: Date.now()};
    const v = S.view = document.createElement('div');
    v.innerHTML = '<h1><span class="num">개념</span><span class="tt"></span></h1><div class="ctx"></div><div class="gal"></div><div class="prog" hidden></div><div class="bd"></div>';
    v.querySelector('.tt').textContent = S.term;
    const ct = clip(S.ctx, S.term, 260), cx = v.querySelector('.ctx');
    cx.innerHTML = ct ? '<small>문서 속 문장</small>' + esc(ct).split(esc(S.term)).join(`<mark>${esc(S.term)}</mark>`) : ''; cx.hidden = !ct;
    Object.assign(S, {bd: v.querySelector('.bd'), gal: v.querySelector('.gal'), prog: v.querySelector('.prog')});
    return S;
  };
  const run = (S, fresh) => { S.bd.innerHTML = ''; S.gal.innerHTML = '<div class="sk"></div><div class="sk"></div><div class="sk"></div>'; S.gal.dataset.done = '';
    S.prog.hidden = true; S.prog.innerHTML = ''; S.last = ''; S.top = 0; deep(S, fresh); };
  const open = S => {
    if (X && X !== S) X.top = $('.card .sc').scrollTop;
    X = S; S.unseen = false; theme();
    const c = $('.card'), sc = c.querySelector('.sc'); sc.replaceChildren(S.view);
    c.querySelector('[data-x="back"]').hidden = !hist.length; c.querySelector('.tag').textContent = S.tag;
    const i = c.querySelector('input'); i.disabled = S.busy; i.placeholder = S.ph;
    c.querySelector('.lb').hidden = true; $('.ov').hidden = false; sc.scrollTop = S.top || 0;
    if (S.st === 'idle' || S.st === 'fail') run(S);  // 실패한 카드는 누르면 다시 만든다
    $('.ov').classList.toggle('full', FULL); toc();
    paintDock();
  };
  // 전체화면: 카드가 창을 꽉 채우고(브라우저 전체화면도 시도) 넓은 화면이면 왼쪽에 절 목차·현재 위치 표시. 선택은 기억한다
  let FULL = false; try { FULL = localStorage.getItem('wimfull') === '1'; } catch {}
  const fsOn = () => document.fullscreenElement || document.webkitFullscreenElement;
  const setFull = (on, screen) => {
    FULL = on; try { localStorage.setItem('wimfull', on ? '1' : '0'); } catch {}
    $('.ov').classList.toggle('full', on);
    const b = $('[data-x="full"]'); b.textContent = on ? '⤡ 창으로' : '⤢ 전체화면'; b.title = on ? '창 크기로 (f)' : '전체화면 (f) · 넓은 화면에선 왼쪽에 목차';
    try { if (on && screen && !fsOn()) document.documentElement.requestFullscreen?.().catch(() => {});
      if (!on && fsOn()) document.exitFullscreen?.().catch(() => {}); } catch {}
    toc();
  };
  document.addEventListener('fullscreenchange', () => { if (!fsOn() && FULL && !$('.ov').hidden) setFull(false); });
  let tocT;
  const toc = () => { const t = $('.card .toc'); if (!FULL || !X) { t.innerHTML = ''; return; }
    const hs = [...X.bd.querySelectorAll('.sx > h2')];
    t.innerHTML = hs.length ? '<b>목차</b>' + hs.map((h, i) => `<a data-i="${i}"><i>${esc(h.querySelector('.n')?.textContent || '')}</i>${esc(h.textContent.replace(/^\d+/, ''))}</a>`).join('') : '';
    spy(); };
  const spy = () => { if (!FULL || !X) return; const sc = $('.card .sc'), y = sc.getBoundingClientRect().top + 90;
    const hs = [...X.bd.querySelectorAll('.sx > h2')]; let k = 0; hs.forEach((h, i) => { if (h.getBoundingClientRect().top <= y) k = i; });
    $('.card .toc').querySelectorAll('a').forEach((a, i) => a.classList.toggle('on', i === k)); };
  $('.card .sc').addEventListener('scroll', spy, {passive: true});
  new MutationObserver(() => { clearTimeout(tocT); tocT = setTimeout(toc, 400); }).observe($('.card .sc'), {childList: true, subtree: true});
  $('.card .toc').addEventListener('click', e => { const a = e.target.closest('a'); if (!a || !X) return;
    X.bd.querySelectorAll('.sx > h2')[+a.dataset.i]?.closest('.sx').scrollIntoView({block: 'start', behavior: 'smooth'}); });
  $('.card header').addEventListener('dblclick', e => { if (!e.target.closest('button')) setFull(!FULL, true); });
  const minimize = () => { if (X) X.top = $('.card .sc').scrollTop; $('.ov').hidden = true; if (fsOn()) document.exitFullscreen?.().catch(() => {}); X = null; hist.length = 0; paintDock(); };
  const drop = S => { if (!S) return; S.ctl?.abort(); SS.splice(SS.indexOf(S), 1); if (X === S) minimize(); saveDock(); paintDock(); };
  let dTick = null;
  function paintDock() {
    const d = $('.dock'), run = SS.some(s => s.st === 'run');
    d.innerHTML = SS.map((s, i) => [s, i]).reverse().map(([s, i]) => `<div class="tb ${s.st}${s.unseen ? ' new' : ''}${s === X ? ' on' : ''}" data-s="${i}" title="${esc(s.term)} · ${esc(s.title)}">
      <i></i><b>${esc(s.term)}</b><small>${s.st === 'run' ? Math.round((Date.now() - s.t0) / 1000) + '초' : s.st === 'fail' ? '실패' : s.unseen ? '완료' : ''}</small><button class="dx" data-dx="${i}" title="탭 닫기">×</button></div>`).join('');
    d.hidden = !SS.length; $('.toast').classList.toggle('up', SS.length > 0);
    if (run && !dTick) dTick = setInterval(paintDock, 1000); else if (!run && dTick) { clearInterval(dTick); dTick = null; }
  }
  async function ask(S, b, label) {
    S.ctl?.abort(); const my = S.ctl = new AbortController();
    setBusy(S, true, '답하는 중…'); setTag(S, '생각 중…');
    const sec = document.createElement('section'), box = document.createElement('div');
    if (label) sec.innerHTML = `<div class="q">${esc(label)}</div>`;
    box.className = 'cur'; sec.append(box); S.bd.append(sec);
    if (label && X === S) sec.scrollIntoView({block: 'start', behavior: 'smooth'});
    let acc = '', how = [];
    try {
      const r = await fetch(API + 'explain', {method: 'POST', body: JSON.stringify({k: S.k, title: S.title, v: 2, ...b}), signal: my.signal});
      how = (r.headers.get('X-Explain') || '').split(' ');
      setTag(S, how[0] === 'cache' ? '저장된 답' : how[1] + ' 쓰는 중…');
      const rd = r.body.getReader(), dec = new TextDecoder();
      for (;;) { const {done, value} = await rd.read(); if (done) break;
        acc += dec.decode(value, {stream: true}); box.innerHTML = md(acc); fillMM(box); }
    } catch (e) { acc += e.name === 'AbortError' ? '\n(중단됨)' : '\n[연결 실패] 리더 서버를 확인하세요'; box.innerHTML = md(acc); }
    box.className = '';
    if (my !== S.ctl) return;
    S.last = (S.last ? S.last + '\n' : '') + acc;
    setTag(S, how[0] === 'cache' ? '저장된 답' : ''); setBusy(S, false);
  }
  // 심층 카드: 조사 4갈래(원리·이 문서·웹 근거·실무) 병렬 → 카드 쓰기(초안이 흘러나옴) → 검증 후 고친 최종본으로 바꿈
  async function deep(S, fresh) {
    S.ctl?.abort(); const my = S.ctl = new AbortController(), bd = S.bd, gal = S.gal, pg = S.prog, box = document.createElement('div');
    S.st = 'run'; S.t0 = Date.now(); S.unseen = false; saveDock(); paintDock();
    setBusy(S, true, '카드 만드는 중… ▾ 내리고 계속 읽어도 됩니다');
    box.className = 'cur'; bd.append(box);
    const L = {}, t0 = S.t0; let acc = '', stage = '조사 중', fin = null, cache = false, stat = null, how = [], failMsg = '';
    const imgs = s => { if (!gal.dataset.done) { const m = s.match(/^IMG:(.*)\n/m); if (m) { gal.dataset.done = 1; loadImgs(m[1], gal); } } };
    const paint = () => { if (cache) return; pg.hidden = false;
      pg.innerHTML = Object.values(L).map(v => `<div class="${v.st}"><b>조사 · ${esc(v.label || '')}</b><small>${v.st === 'done' ? `${(v.n || 0).toLocaleString()}자 · 링크 ${v.src || 0} · ${v.sec}초` : v.st === 'fail' ? '실패(나머지로 진행)' : `${(v.n || 0).toLocaleString()}자`}</small></div>`).join('')
        + `<div class="${fin ? 'done' : failMsg ? 'fail' : 'run'}"><b>${esc(stage)}</b><small>${[fin || failMsg ? '' : Math.round((Date.now() - t0) / 1000) + '초', failMsg, stat ? `검증 ${stat.checks}건 · 수정 ${stat.fixed}건` : ''].filter(Boolean).join(' · ')}</small></div>`; };
    const tick = setInterval(paint, 1000);
    setTag(S, '조사 4갈래 → 카드 → 검증');
    // 연결이 끊기면(서버 재시작·와이파이·절전) 실패로 끝내지 않고 다시 붙는다. 서버에 만들던 카드가 있으면 이어 받고,
    // 없으면(재시작으로 사라짐) 새로 만든다. 서버가 '실패'를 보낸 경우만 멈추고 다시 시도 버튼을 보인다
    let err = null;
    for (let n = 0; ; n++) {
      for (const k in L) delete L[k];
      acc = ''; box.innerHTML = ''; err = null;
      try {
        const r = await fetch(API + 'explain', {method: 'POST', signal: my.signal,
          body: JSON.stringify({k: S.k, title: S.title, v: 3, term: S.term, ctx: S.ctx, head: S.head, doc: S.doc, fresh: !!fresh && n === 0})});
        if (!r.ok) throw new Error('HTTP ' + r.status);
        how = (r.headers.get('X-Explain') || '').split(' ');
        stage = '조사 중';
        if (how[0] === 'attach') setTag(S, '만들던 카드에 다시 붙음 · 조사 → 카드 → 검증');
        else if (n) setTag(S, '다시 연결됨 · 조사 → 카드 → 검증');
        const rd = r.body.getReader(), dec = new TextDecoder(); let buf = '';
        for (;;) { const {done, value} = await rd.read(); if (done) break; buf += dec.decode(value, {stream: true});
          for (let i; (i = buf.indexOf('\n')) >= 0;) { const ln = buf.slice(0, i); buf = buf.slice(i + 1); let ev; try { ev = JSON.parse(ln); } catch { continue; }
            if (ev.e === 'lane') L[ev.id] = {...(L[ev.id] || {}), ...ev};
            else if (ev.e === 'stage') stage = ev.st === 'write' ? (ev.who === 'session' ? '만든 세션이 카드 쓰는 중 (초안)' : '카드 쓰는 중 (초안)') : '검증 중 (초안 대조)';
            else if (ev.e === 'syn') { acc += ev.d; imgs(acc); box.innerHTML = md(acc); fillMM(box); }
            else if (ev.e === 'final') { fin = ev.card; cache = !!ev.cache; stat = ev.stat || null; stage = cache ? '저장된 카드' : `끝 · ${ev.sec}초`; }
            else if (ev.e === 'err') err = ev.msg || '알 수 없는 오류';
          }
          paint(); }
      } catch (e) { if (e.name === 'AbortError' || my !== S.ctl) { clearInterval(tick); return; } }
      if (fin || err) break;
      if (n >= 30) { err = '서버와 연결이 계속 끊깁니다. 리더 서버를 확인하세요'; break; }
      const w = Math.min(2 + n * 2, 10);
      for (let t = w; t > 0; t--) { stage = `연결 끊김 · ${t}초 뒤 다시 붙음 (${n + 1}번째)`; paint(); await new Promise(ok => setTimeout(ok, 1000)); if (my !== S.ctl) { clearInterval(tick); return; } }
      stage = '다시 연결 중…'; paint();
    }
    if (err) { stage = '실패'; failMsg = err; }
    clearInterval(tick);
    if (my !== S.ctl) return;
    if (fin) { imgs(fin); box.innerHTML = md(fin); fillMM(box); acc = fin; }
    else { const f = document.createElement('div'); f.className = 'failbox';
      f.innerHTML = `<b>카드를 끝까지 못 만들었습니다</b><span>${esc(failMsg)}</span><button data-x="re">다시 시도</button>`; box.after(f); }
    box.className = ''; paint(); if (cache) pg.hidden = true;
    if (!gal.dataset.done) gal.innerHTML = '';
    S.last = acc; S.st = fin ? 'done' : 'fail';
    setTag(S, cache ? '저장된 카드' : fin ? (stat ? `조사 ${stat.lanes}갈래 · 검증 ${stat.checks}건 · 수정 ${stat.fixed}건` : '') : '');
    setBusy(S, false);
    if (X !== S && !cache && SS.includes(S)) { S.unseen = true; toast(`‘${esc(S.term)}’ 카드 ${fin ? '완성' : '실패'}`, 9000, [['열기', () => { hist.length = 0; open(S); }]]); }
    saveDock(); paintDock();
  }
  const explain = p => {
    if (!p) return; if (p.term.length > 80) return openAsk(p); $('.chip').hidden = true;
    try { p.odoc?.getSelection().removeAllRanges(); getSelection().removeAllRanges(); R.getSelection?.()?.removeAllRanges(); } catch {}
    const doc = p.inner ? X?.doc : p.doc;
    if (p.inner && X) hist.push(X); else if (!p.inner) hist.length = 0;
    let S = SS.find(s => s.term === p.term && s.k === K);
    if (S) SS.splice(SS.indexOf(S), 1);  // 같은 용어면 그 탭을 맨 앞(최근)으로
    else { S = session({term: p.term, ctx: p.ctx, head: p.head, doc});
      while (SS.length >= DMAX) { const o = SS.find(s => s.st !== 'run' && s !== X && !hist.includes(s)) || SS[0]; o.ctl?.abort(); SS.splice(SS.indexOf(o), 1); } }
    SS.push(S); open(S);
  };
  // 왼쪽 아래 '카드' 버튼: 서버에 저장된 모든 카드 목록 → 고르면 저장본을 바로 연다(새로 만들지 않음)
  let hpT;
  const ago2 = t => { const d = new Date(t * 1000); return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
  let HSC = 'doc';
  $('.hp .hsc').addEventListener('click', e => { const h = e.target.closest('[data-h]')?.dataset.h; if (!h) return; HSC = h;
    $('.hp .hsc').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.h === h)); loadHist(); });
  const loadHist = () => fetch(API + 'explains?q=' + encodeURIComponent($('.hp input').value.trim()) + (HSC === 'doc' ? '&k=' + encodeURIComponent(K) : '')).then(r => r.json()).then(j => {
    const it = j.items || []; $('.hp .cnt').textContent = it.length + '개';
    $('.hp .ls').innerHTML = it.length ? it.map(x => `<button class="it${x.key === K ? ' here' : ''}" data-id="${x.id}"><b>${esc(x.term)}</b>
      <small>${esc(x.title || x.key || '')} · ${ago2(x.created_at)}${/deep/.test(x.model || '') ? '' : ' · 간단 카드'}</small></button>`).join('')
      : `<div class="mu" style="padding:14px">${HSC === 'doc' ? '이 문서에서 만든 카드 없음 · 위 \'전체\'를 누르면 다른 문서 카드' : '기록 없음'}</div>`;
  }).catch(() => { $('.hp .ls').innerHTML = '<div class="mu" style="padding:14px">불러오기 실패</div>'; });
  const fromHist = id => fetch(API + 'explains?id=' + id).then(r => r.json()).then(x => {
    if (!x.term) return;
    $('.hp').hidden = true; $('.hb').classList.remove('on');
    let S = SS.find(s => s.term === x.term && s.k === x.key);
    if (S) { SS.splice(SS.indexOf(S), 1); if (S.st === 'idle') S.st = 'load'; }
    if (!S || S.st === 'load') {
      S = S || session({term: x.term, k: x.key, title: $('.hp [data-id="' + id + '"] small')?.textContent.split(' · ')[0] || x.key});
      S.st = 'done'; S.prog.hidden = true; S.gal.innerHTML = ''; S.gal.dataset.done = '';
      const m = x.answer.match(/^IMG:(.*)\n/m); if (m) { S.gal.dataset.done = 1; loadImgs(m[1], S.gal); }
      S.bd.innerHTML = md(x.answer); fillMM(S.bd); S.last = x.answer; S.tag = '저장된 카드 · ' + ago2(x.created_at);
    }
    while (SS.length >= DMAX) { const o = SS.find(s => s.st !== 'run' && s !== X) || SS[0]; o.ctl?.abort(); SS.splice(SS.indexOf(o), 1); }
    SS.push(S); hist.length = 0; open(S); saveDock();
  });
  $('.hb').addEventListener('click', () => { const p = $('.hp'); p.hidden = !p.hidden; $('.hb').classList.toggle('on', !p.hidden); if (!p.hidden) { loadHist(); $('.hp input').focus(); } });
  $('.hp input').addEventListener('input', () => { clearTimeout(hpT); hpT = setTimeout(loadHist, 200); });
  $('.hp').addEventListener('keydown', e => { e.stopPropagation(); if (e.key === 'Escape') { $('.hp').hidden = true; $('.hb').classList.remove('on'); } });
  $('.hp .ls').addEventListener('click', e => { const b = e.target.closest('.it'); if (b) fromHist(b.dataset.id); });
  // ---------- 만든 세션에 묻기·고치기: 고른 곳 위·아래에 입력창을 띄워 이 HTML을 만든 Claude Code 세션에 묻거나 고치게 한다 ----------
  const SIC = {}, TOOLK = {Read: '읽기', Grep: '찾기', Glob: '파일 목록', Edit: '고치기', MultiEdit: '고치기', Write: '쓰기', Bash: '실행', WebSearch: '웹 검색',
    WebFetch: '웹 읽기', Task: '에이전트', Agent: '에이전트', TodoWrite: '할 일', Skill: '스킬'};
  const sessOf = (k, refresh) => (!refresh && SIC[k]) ? Promise.resolve(SIC[k]) :
    fetch(API + 'session?k=' + encodeURIComponent(k) + (refresh ? '&refresh=1' : '')).then(r => r.ok ? r.json() : {error: 1}).then(j => SIC[k] = j).catch(() => ({error: 1}));
  const dstr = t => { const d = new Date(t * 1000); return `${d.getMonth() + 1}/${d.getDate()}`; };
  const sessLabel = si => !si ? '세션 찾는 중…' : si.error ? '이 컴퓨터에서만 쓸 수 있습니다' : si.sid
    ? `세션 ${si.sid.slice(0, 8)} · ${dstr(si.mtime)}${si.n ? ` · 이 폴더 ${si.n}번 수정` : ''}${si.fork ? ' · 이어서 묻는 중' : ''}` : '만든 세션을 못 찾음 → 새 세션으로 묻습니다';
  // 요청은 서버에서 따로 돈다. 연결이 끊기면 같은 id로 다시 붙는다(수정 요청이 두 번 돌지 않게 resume_only)
  // id가 같으면 서버가 같은 작업에 다시 붙인다(새로고침·서버 재시작 뒤에도 기록에서 이어 받음). 45초 동안 아무것도 안 오면 끊긴 것으로 보고 다시 붙는다
  async function askStream(b, h, id, reattach) {
    id = id || Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    for (let n = 0; n < 60; n++) {
      let fin = false;
      try {
        const r = await fetch(API + 'ask', {method: 'POST', body: JSON.stringify({k: K, ...b, id, resume_only: reattach || n > 0})});
        if (!r.ok) { let m = '요청 거절'; try { m = (await r.json()).error || m; } catch {} h.err(m); return; }
        h.reset?.();
        const rd = r.body.getReader(), dec = new TextDecoder(); let buf = '';
        for (;;) {
          const res = await Promise.race([rd.read(), new Promise(ok => setTimeout(() => ok({stall: 1}), 45000))]);
          if (res.stall) { try { rd.cancel(); } catch {} break; }
          if (res.done) break; buf += dec.decode(res.value, {stream: true});
          for (let i; (i = buf.indexOf('\n')) >= 0;) { const ln = buf.slice(0, i); buf = buf.slice(i + 1); let ev; try { ev = JSON.parse(ln); } catch { continue; }
            if (ev.e === 'done') { fin = true; h.done(ev); } else if (ev.e === 'err') { fin = true; h.err(ev.msg); } else h[ev.e]?.(ev); } }
      } catch {}
      if (fin) return;
      h.note?.({msg: '연결 끊김 · 다시 붙는 중'}); await new Promise(ok => setTimeout(ok, Math.min(2000 + n * 1000, 8000)));
    }
    h.err('서버와 연결이 계속 끊깁니다');
  }

  // 고른 곳마다 따로 창(스레드)을 띄운다. 창마다 자기 세션 갈래를 가져 여러 개가 동시에 돈다(같은 덩어리 수정은 서버가 줄 세움)
  const TPL = $('.ask'), WTPL = $('.work'), PANES = [];
  let SIDE = 'right'; try { SIDE = localStorage.getItem('wimaskside') || 'right'; } catch {}
  let FOCUS = null, BASE = null, LIVE = Promise.resolve(), LASTCHG = [];
  const AP = {ask: '예: 그럼 지금 하드웨어는 USB로 연결된 건가? USB 대기는 뭐지?', edit: '예: 이 그림 아래에 USB 대기가 무엇인지 한 문단 추가해줘'};
  const blkOf = range => { const n0 = range?.startContainer, e0 = n0 && (n0.nodeType === 1 ? n0 : n0.parentElement);
    let b = e0?.closest?.('figure') || e0?.closest?.('svg') || e0?.closest?.(BLK) || e0; while (b?.ownerSVGElement) b = b.ownerSVGElement; return b || null; };
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const sigOf = el => el ? el.tagName + ':' + norm(el.textContent).slice(0, 80) : '';
  const findBySig = sig => { if (!sig) return null; const i = sig.indexOf(':'), tag = sig.slice(0, i), t = sig.slice(i + 1); if (!t) return null;
    for (const d of allDocs()) for (const el of d.querySelectorAll(tag)) if (norm(el.textContent).startsWith(t)) return {el, doc: d}; return null; };
  const skin = el => { for (const [k, v] of Object.entries(KT)) if (k !== 'dark') el.style.setProperty('--k-' + k, v);
    const f = $('.card').style.getPropertyValue('--k-font'); if (f) el.style.setProperty('--k-font', f); };
  const rectOf = P => { try { const el = (P.work?.el?.isConnected && P.work.el) || (P.blk?.isConnected && P.blk);
      if (el) return toTop(el.getBoundingClientRect(), P.doc);
      if (P.range) { const b = P.range.getBoundingClientRect(); if (b.width || b.height) return toTop(b, P.doc); } } catch {} return null; };
  const setMode = (P, m) => { P.mode = m; P.el.classList.toggle('edit', m === 'edit');
    P.el.querySelectorAll('[data-m]').forEach(b => b.classList.toggle('on', b.dataset.m === m)); P.el.querySelector('textarea').placeholder = AP[m]; };
  const updMini = P => { P.el.querySelector('.ms').textContent = P.status || norm(P.sel).slice(0, 40); };
  const placeWork = P => { const w = P.wk;
    if (!P.work) { w.hidden = true; return; }
    const r = rectOf(P); if (!r) { w.hidden = true; return; }
    const top = Math.max(-20, r.top - 6), bot = Math.min(innerHeight + 20, r.bottom + 6);
    if (bot < 0 || top > innerHeight) { w.hidden = true; return; }
    w.hidden = false; w.querySelector('b').textContent = P.work.label;
    Object.assign(w.style, {left: (r.left - 6) + 'px', top: top + 'px', width: (r.right - r.left + 12) + 'px', height: (bot - top) + 'px'}); };
  // 창 배치: 보내기 전 창은 고른 곳 위·아래, 보낸 창은 그 덩어리 옆. 옆 창이 여럿이면 위에서부터 줄 세우고 겹치면 아래로 민다.
  // 화면이 아니라 본문 좌표라 스크롤하면 그 절과 같이 지나간다. 지금 보는 창(FOCUS) 하나만 펼치고 나머지는 한 줄로 접는다
  function layout() {
    const side = [];
    for (const P of PANES) { const a = P.el; placeWork(P);
      P.mini = P.side && P !== FOCUS;
      a.classList.toggle('side', P.side); a.classList.toggle('mini', P.mini); a.classList.toggle('focus', P === FOCUS);
      const oc = !!P.blk?.closest?.('.ov'); a.classList.toggle('oncard', oc); P.wk.classList.toggle('oncard', oc);  // 카드 안 글에서 연 창만 카드 위로
      a.style.width = ''; a.style.right = 'auto'; a.style.bottom = 'auto';
      a.querySelector('[data-q="side"]').hidden = P.side; a.querySelector('[data-q="flip"]').hidden = !P.side || P.mini;
      a.querySelector('[data-q="float"]').hidden = !P.side || P.mini || !rectOf(P); a.querySelector('[data-q="mini"]').hidden = !P.side || P.mini;
      updMini(P);
      if (P.side) { side.push(P); continue; }
      const w = a.offsetWidth, r = rectOf(P); let x, y;
      if (!r) { a.style.maxHeight = ''; x = (innerWidth - w) / 2; y = innerHeight - a.offsetHeight - 56; }
      else { x = Math.min(innerWidth - w - 12, Math.max(12, r.left));
        const below = innerHeight - r.bottom - 60, above = r.top - 20, down = below >= above, room = Math.max(260, (down ? below : above));
        a.style.maxHeight = Math.min(room, innerHeight * 0.72, 680) + 'px'; const h2 = a.offsetHeight;
        y = down ? r.bottom + 10 : r.top - h2 - 10; }
      a.style.left = Math.max(12, x) + 'px'; a.style.top = y + 'px'; }
    const items = side.map(P => ({P, r: rectOf(P)})), anch = items.filter(o => o.r).sort((a, b) => a.r.top - b.r.top || PANES.indexOf(a.P) - PANES.indexOf(b.P));
    let bottom = -1e9;
    for (const {P, r} of anch) { const a = P.el; a.style.maxHeight = P.mini ? '' : Math.min(innerHeight - 140, 560) + 'px';
      const gapR = innerWidth - r.right - 28, gapL = r.left - 28;
      const useL = SIDE === 'left' ? gapL >= 300 : (gapR < 300 && gapL >= 300), useR = !useL && gapR >= 300;
      let x; if (useL) { const w = Math.min(380, gapL); a.style.width = w + 'px'; x = r.left - w - 14; }
      else if (useR) { const w = Math.min(380, gapR); a.style.width = w + 'px'; x = r.right + 14; }
      else x = SIDE === 'left' ? 14 : innerWidth - Math.min(380, innerWidth - 24) - 14;
      const y = Math.max(r.top, bottom + 8); a.style.left = x + 'px'; a.style.top = y + 'px'; bottom = y + a.offsetHeight; }
    let yb = 58;
    for (const {P} of items.filter(o => !o.r)) { const a = P.el; a.style.maxHeight = P.mini ? '' : Math.min(innerHeight - 140, 560) + 'px';
      Object.assign(a.style, SIDE === 'left' ? {left: '14px', right: 'auto'} : {left: 'auto', right: '14px'}, {top: 'auto', bottom: yb + 'px'}); yb += a.offsetHeight + 8; }
  }
  const glide = P => { P.el.classList.add('mv'); setTimeout(() => P.el.classList.remove('mv'), 360); layout(); };
  const savePanes = () => { try { sessionStorage.setItem('wimpanes:' + K, JSON.stringify(PANES.filter(P => P.el.querySelector('.th').children.length).map(P =>
    ({th: P.el.querySelector('.th').innerHTML, sel: P.sel, ctx: P.ctx, head: P.head, mode: P.mode, thread: P.thread, sig: P.sig})))); } catch {} };
  const dropPane = P => { P.el.remove(); P.wk.remove(); PANES.splice(PANES.indexOf(P), 1); if (FOCUS === P) FOCUS = null; savePanes(); layout(); };
  function newPane(x) {
    const el = TPL.cloneNode(true), wk = WTPL.cloneNode(true); el.hidden = false; wk.hidden = true; R.append(el); R.append(wk);
    const P = {el, wk, range: x.range || null, doc: x.doc || document, sel: x.sel || '', ctx: x.ctx || '', head: x.head || '', blk: x.blk || null,
      sig: x.sig || sigOf(x.blk), mode: x.mode || 'ask', side: !!x.side, busy: 0, thread: x.thread || '', work: null, status: ''};
    PANES.push(P); theme(); skin(el);
    el.querySelector('.qs').innerHTML = '<b>고른 부분</b>' + esc(P.sel.length > 600 ? P.sel.slice(0, 600) + '…' : P.sel);
    if (x.th) { el.querySelector('.th').innerHTML = x.th; el.querySelectorAll('.live .sp').forEach(n => n.remove()); }
    el.querySelector('.si').textContent = sessLabel(SIC[K]); sessOf(K).then(si => el.querySelector('.si').textContent = sessLabel(si));
    setMode(P, P.mode);
    el.querySelector('form').addEventListener('submit', e => { e.preventDefault(); sendAsk(P); });
    el.addEventListener('keydown', e => { e.stopPropagation();
      if (e.key === 'Escape') { if (P.busy || el.querySelector('.th').children.length) { P.side = true; FOCUS = null; layout(); } else dropPane(P); }
      else if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.target.tagName === 'TEXTAREA') { e.preventDefault(); sendAsk(P); } });
    el.addEventListener('click', e => { const q = e.target.closest('[data-q]')?.dataset.q, m = e.target.closest('[data-m]')?.dataset.m;
      if (m) return setMode(P, m);
      if (q === 'x') return (P.busy ? (P.side = true, FOCUS = null, layout()) : dropPane(P));
      if (q === 'mini') { FOCUS = null; return layout(); }
      if (q === 'side') { P.side = true; return glide(P); }
      if (q === 'float') { P.side = false; FOCUS = P; return glide(P); }
      if (q === 'flip') { SIDE = SIDE === 'left' ? 'right' : 'left'; try { localStorage.setItem('wimaskside', SIDE); } catch {} return glide(P); }
      if (q === 'jump') return jumpChg();
      if (q === 'cont') { setMode(P, 'edit'); P.el.querySelector('textarea').value = '중간에 끊긴 앞 요청을 이어서 마무리해. 이미 쓴 _raw 조사 메모와 고친 원본을 다시 읽고, 남은 절을 다 쓴 뒤 빌드까지 끝내'; return sendAsk(P); }
      if (q === 'reload') { try { sessionStorage.setItem('wimgo', JSON.stringify({a: anchor(), r: ratio()})); } catch {} savePanes(); return location.reload(); }
      if (FOCUS !== P) { FOCUS = P; layout(); } });
    return P;
  }
  const openAsk = p => {
    if (!p) return; $('.chip').hidden = true;
    const blk = blkOf(p.range);
    try { p.odoc?.getSelection().removeAllRanges(); getSelection().removeAllRanges(); R.getSelection?.()?.removeAllRanges(); } catch {}
    // 아직 아무것도 안 보낸 빈 창이 있으면 그 창을 새 자리로 옮기고, 없으면 새 창을 연다(돌고 있는 창은 그 자리에 남는다)
    let P = PANES.find(x => !x.busy && !x.el.querySelector('.th').children.length);
    if (P) { Object.assign(P, {range: p.range || null, doc: p.odoc || document, sel: p.term, ctx: p.ctx, head: p.head, blk, sig: sigOf(blk), side: false});
      P.el.querySelector('.qs').innerHTML = '<b>고른 부분</b>' + esc(p.term.length > 600 ? p.term.slice(0, 600) + '…' : p.term); }
    else P = newPane({range: p.range, doc: p.odoc, sel: p.term, ctx: p.ctx, head: p.head, blk, mode: PANES.at(-1)?.mode});
    FOCUS = P; glide(P); P.el.querySelector('textarea').focus();
  };
  // ---- 화면에 바로 반영: 고친 파일을 다시 받아 이전 파일과 비교하고, 바뀐 가장 작은 덩어리만 화면 DOM에서 바꾼다(창이 여럿이어도 한 줄로) ----
  const loadFile = () => fetch(location.href.split('#')[0], {cache: 'no-store'}).then(r => r.text()).then(t => new DOMParser().parseFromString(t, 'text/html'));
  const kidsOf = el => [...el.childNodes].filter(n => n.nodeType === 1 ? !(n.id === '__wim_reader' || /^(SCRIPT|NOSCRIPT)$/.test(n.tagName)) : n.nodeType === 3 && n.data.trim());
  const sameN = (x, y) => x.nodeType === 3 ? x.data === y.data : x.outerHTML === y.outerHTML;
  const attrsEq = (x, y) => x.attributes.length === y.attributes.length && [...x.attributes].every(t => y.getAttribute(t.name) === t.value);
  const diffT = (a, b, path, ops, d) => {
    const A = kidsOf(a), B = kidsOf(b);
    if (A.length === B.length && A.every((x, i) => x.nodeName === B[i].nodeName)) {
      A.forEach((x, i) => { if (sameN(x, B[i])) return;
        if (x.nodeType === 1 && d < 16 && attrsEq(x, B[i]) && kidsOf(x).length) diffT(x, B[i], [...path, i], ops, d + 1);
        else ops.push({k: 'rep', path: [...path, i], name: x.nodeName, node: B[i]}); });
    } else {
      let s = 0; while (s < A.length && s < B.length && sameN(A[s], B[s])) s++;
      let e = 0; while (e < A.length - s && e < B.length - s && sameN(A[A.length - 1 - e], B[B.length - 1 - e])) e++;
      ops.push({k: 'spl', path, s, del: A.length - s - e, add: B.slice(s, B.length - e), names: A.map(x => x.nodeName)});
    } };
  const nodeAt = (root, path) => { let n = root; for (const i of path) { n = kidsOf(n)[i]; if (!n) return null; } return n; };
  const flashCss = () => { if (document.getElementById('__wimfl')) return; const s = document.createElement('style'); s.id = '__wimfl';
    s.textContent = '@keyframes __wimfl{0%{outline-color:rgba(245,179,1,.95);box-shadow:0 0 0 7px rgba(245,179,1,.35);background-color:rgba(245,179,1,.2)}'
      + '100%{outline-color:rgba(245,179,1,0);box-shadow:0 0 0 0 rgba(245,179,1,0)}}.__wimfl{outline:3px solid transparent;outline-offset:3px;border-radius:6px;animation:__wimfl 3.4s ease-out 1}';
    document.head.append(s); };
  const flash = els => { flashCss(); const out = [];
    for (let el of els) { if (!el?.isConnected) continue; while (el.ownerSVGElement) el = el.ownerSVGElement;
      if (el === document.body || out.some(o => o.contains(el))) continue; out.push(el); }
    out.slice(0, 40).forEach(el => { el.classList.remove('__wimfl'); void el.offsetWidth; el.classList.add('__wimfl'); setTimeout(() => el.classList.remove('__wimfl'), 3600); });
    return out; };
  const showJump = els => { const j = $('.jump'); LASTCHG = els; if (!els.length) return;
    const r = els[0].getBoundingClientRect(); if (r.bottom > 0 && r.top < innerHeight) { j.hidden = true; return; }
    j.textContent = `${r.top > 0 ? '↓' : '↑'} 바뀐 곳 보기 · ${els.length}곳`; j.hidden = false; clearTimeout(j._t); j._t = setTimeout(() => j.hidden = true, 15000); };
  const jumpChg = () => { $('.jump').hidden = true; const el = LASTCHG.find(x => x.isConnected); if (el) { el.scrollIntoView({block: 'center', behavior: 'smooth'}); setTimeout(() => flash([el]), 450); } };
  async function liveApply() {
    let nd; try { nd = await loadFile(); } catch { return -1; }
    if (!BASE) { BASE = nd; return -1; }
    const ops = []; diffT(BASE.body, nd.body, [], ops, 0);
    const os = [...document.head.querySelectorAll('style:not(#__wimfl)')], ns = [...nd.head.querySelectorAll('style')];
    ns.forEach((x, i) => { if (os[i] && os[i].textContent !== x.textContent) os[i].textContent = x.textContent; });
    if (nd.title && nd.title !== document.title) document.title = nd.title;
    if (!ops.length) { BASE = nd; return 0; }
    const acts = ops.map(op => ({op, t: nodeAt(document.body, op.path)}));
    for (const {op, t} of acts) {  // 화면이 파일과 같은 모양인지 먼저 본다(페이지 스크립트가 바꾼 곳이면 새로고침으로)
      if (!t || (op.k === 'rep' ? t.nodeName !== op.name : (kidsOf(t).length !== op.names.length || kidsOf(t).some((x, i) => x.nodeName !== op.names[i])))) { BASE = nd; return -1; } }
    const marks = [];
    for (const {op, t} of acts) {
      if (op.k === 'rep') { const z = document.importNode(op.node, true); t.replaceWith(z); marks.push(z.nodeType === 1 ? z : z.parentElement); }
      else { const L = kidsOf(t), before = L[op.s + op.del] || null; L.slice(op.s, op.s + op.del).forEach(n => n.remove());
        op.add.forEach(y => { const z = document.importNode(y, true); t.insertBefore(z, before); marks.push(z.nodeType === 1 ? z : t); });
        if (!op.add.length) marks.push(t); } }
    BASE = nd; const shown = flash(marks); showJump(shown);
    for (const P of PANES) if (P.blk && !P.blk.isConnected) { const f = findBySig(P.sig); P.blk = f?.el || shown[0] || null; if (P.work) P.work.el = P.blk; }
    layout(); return shown.length;
  }
  async function sendAsk(P) {
    const a = P.el, t = a.querySelector('textarea'), q = t.value.trim(); if (!q) return;
    t.value = ''; const mode = P.mode, id = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    if (mode === 'edit' && !BASE) { try { BASE = await loadFile(); } catch {} }
    const item = document.createElement('div'); Object.assign(item.dataset, {jid: id, mode, q, run: '1'});
    item.innerHTML = `<div class="tq"><i class="${mode === 'edit' ? 'ed' : ''}">${mode === 'edit' ? 'HTML 수정' : '질문'}</i>${esc(q)}</div>`
      + '<div class="tl live"><span class="sp"></span><span class="st">보냄 · 세션 여는 중</span><span class="tm">0초</span></div><div class="tls"></div><div class="ta cur"></div>';
    a.querySelector('.th').append(item); savePanes();
    runItem(P, item, {sel: P.sel, ctx: P.ctx, head: P.head, q, mode, thread: P.thread, blk: P.sig}, false);
  }
  async function runItem(P, item, req, reattach) {
    const a = P.el, th = a.querySelector('.th'), mode = item.dataset.mode, id = item.dataset.jid;
    P.busy++; P.side = true; if (!reattach) FOCUS = P;
    P.work = {label: mode === 'edit' ? '만든 세션이 고치는 중…' : '만든 세션이 읽는 중…', el: P.blk}; glide(P);
    if (reattach) { item.querySelector('.ok')?.remove(); const lv = item.querySelector('.live'); lv.classList.remove('end');
      if (!lv.querySelector('.sp')) lv.insertAdjacentHTML('afterbegin', '<span class="sp"></span>'); item.querySelector('.ta').classList.add('cur'); }
    th.scrollTop = th.scrollHeight;
    const tls = item.querySelector('.tls'), ta = item.querySelector('.ta'), st = item.querySelector('.st'), tm = item.querySelector('.tm'), t0 = Date.now();
    const stage = m => { st.textContent = m; P.status = `${mode === 'edit' ? '수정' : '질문'} · ${m} · ${Math.round((Date.now() - t0) / 1000)}초`; updMini(P); };
    const tick = setInterval(() => { tm.textContent = Math.round((Date.now() - t0) / 1000) + '초'; stage(st.textContent); }, 1000);
    let txt = '', nchg = 0, stale = false, fin = null;
    const line = m => { tls.insertAdjacentHTML('beforeend', `<div class="tl">${esc(m)}</div>`); th.scrollTop = th.scrollHeight; };
    const apply = () => LIVE = LIVE.then(async () => { stage('화면에 반영 중'); const n = await liveApply();
      if (n > 0) { nchg += n; line(`화면에 반영 · ${n}곳`); } else if (n < 0) stale = true; });
    if (reattach) stage('다시 붙는 중');
    await askStream(req, {
      reset: () => { txt = ''; tls.innerHTML = ''; ta.innerHTML = ''; },
      thread: ev => { P.thread = ev.sid; savePanes(); },
      queue: ev => stage(`대기 · 같은 덩어리 수정 ${ev.n}개 먼저`),
      hb: () => {},
      meta: ev => { stage(ev.how === 'cont' ? '같은 창 대화에 이어서' : ev.how === 'fork' ? '만든 세션 맥락 불러오는 중' : '새 세션 여는 중');
        if (ev.how === 'fork') line(`만든 세션 ${String(ev.sid).slice(0, 8)}에서 이 창 전용 갈래를 냄`); else if (ev.how === 'new') line('만든 세션을 못 찾아 새 세션으로'); },
      tool: ev => { stage(`${TOOLK[ev.name] || ev.name} 중`); line(`${TOOLK[ev.name] || ev.name} · ${ev.arg || ''}`); },
      file: () => apply(),
      note: ev => line(ev.msg),
      text: ev => { stage(mode === 'edit' ? '고치며 설명 쓰는 중' : '답 쓰는 중'); txt += ev.d; ta.innerHTML = md(txt); fillMM(ta); if (FOCUS === P) th.scrollTop = th.scrollHeight; },
      done: ev => { fin = ev; }, err: m => { fin = {err: m}; } }, id, reattach);
    if (fin?.changed) apply();  // 마지막 빌드가 감시 틈에 걸렸을 수 있다
    await LIVE; clearInterval(tick); ta.classList.remove('cur'); item.dataset.run = '0';
    const live = item.querySelector('.live'); live.classList.add('end'); live.querySelector('.sp')?.remove();
    if (fin?.err && !fin.sec) { stage('실패'); ta.innerHTML = md(txt) + `<p style="color:var(--k-warn)">[실패] ${esc(fin.err)}</p>`; fillMM(ta); }
    else { const half = !fin?.changed && fin?.src?.length;
      stage(fin?.changed ? (nchg ? '끝 · 화면에 반영됨' : '끝 · 파일 고침') : half ? '멈춤 · 원본만 고침(빌드 안 됨)' : mode === 'edit' ? '끝 · 파일 변화 없음' : '끝');
      if (!txt) ta.innerHTML = '';
      if (fin?.err) item.insertAdjacentHTML('beforeend', `<p style="color:var(--k-warn)">${esc(fin.err)}</p>`);
      if (half) item.insertAdjacentHTML('beforeend', `<div class="ok">조사·원본 수정은 됐지만 빌드 전에 끝났습니다 · ${fin.src.length}개 파일<br><small>${fin.src.map(esc).join(' · ')}</small><button type="button" data-q="cont">이어서 마무리</button></div>`);
      if (fin?.changed) item.insertAdjacentHTML('beforeend', nchg && !stale
        ? `<div class="ok">화면에 바로 반영했습니다 · ${nchg}곳<button type="button" data-q="jump">바뀐 곳 보기</button><button type="button" class="lnk" data-q="reload">새로고침</button></div>`
        : `<div class="ok">HTML이 고쳐졌습니다 · 새로고침해야 보입니다<button type="button" data-q="reload">새로고침해서 보기</button></div>`); }
    P.busy--; if (!P.busy) P.work = null;
    if (FOCUS !== P && PANES.includes(P)) toast(`‘${esc(norm(P.sel).slice(0, 20))}’ ${mode === 'edit' ? '수정' : '답'} 끝`, 6000, [['보기', () => { FOCUS = P; layout(); }]]);
    savePanes(); layout(); th.scrollTop = th.scrollHeight;
    delete SIC[K]; sessOf(K).then(si => a.querySelectorAll('.si').forEach(x => x.textContent = sessLabel(si)));
  }
  $('.jump').addEventListener('click', jumpChg);
  let pRaf = 0; const rePlace = () => { if (!pRaf) pRaf = requestAnimationFrame(() => { pRaf = 0; layout(); }); };
  addEventListener('scroll', rePlace, {passive: true, capture: true}); addEventListener('resize', rePlace);
  const restoreAsk = () => { try { const L = JSON.parse(sessionStorage.getItem('wimpanes:' + K) || '[]');
    for (const v of L) { const f = findBySig(v.sig); const P = newPane({...v, blk: f?.el || null, doc: f?.doc || document, side: true});
      // 새로고침 전에 돌던 요청: 같은 작업 id로 다시 붙어 이어 받는다(서버가 재시작됐어도 기록에서)
      P.el.querySelectorAll('.th > div[data-run="1"]').forEach(it => runItem(P, it, {sel: P.sel, ctx: P.ctx, head: P.head, q: it.dataset.q, mode: it.dataset.mode, thread: P.thread, blk: P.sig}, true)); }
    PANES.forEach(P => fillMM(P.el)); FOCUS = PANES.at(-1) || null; layout(); } catch {} };
  // 개념 카드의 '더 물어보기'도 만든 세션이 있으면 그 세션에 묻는다
  async function askCard(S, q) {
    setBusy(S, true, '만든 세션이 답하는 중…'); setTag(S, '만든 세션에 묻는 중');
    const sec = document.createElement('section'), box = document.createElement('div');
    sec.innerHTML = `<div class="q">↳ ${esc(q)} <small>· 만든 세션</small></div><div class="tls"></div>`; box.className = 'cur'; sec.append(box); S.bd.append(sec);
    if (X === S) sec.scrollIntoView({block: 'start', behavior: 'smooth'});
    const tls = sec.querySelector('.tls'); let txt = '';
    await askStream({k: S.k, sel: S.term, ctx: S.ctx, head: S.head, mode: 'ask', q: `개념 카드 '${S.term}'을 보다가 묻는다: ${q}`}, {
      reset: () => { txt = ''; tls.innerHTML = ''; box.innerHTML = ''; },
      tool: ev => tls.insertAdjacentHTML('beforeend', `<div class="tl">${esc(TOOLK[ev.name] || ev.name)} · ${esc(ev.arg || '')}</div>`),
      text: ev => { txt += ev.d; box.innerHTML = md(txt); fillMM(box); },
      done: () => {}, err: m => { txt += `\n\n[실패] ${m}`; box.innerHTML = md(txt); } });
    box.className = ''; S.last = (S.last ? S.last + '\n' : '') + txt; setTag(S, ''); setBusy(S, false);
  }
  try { JSON.parse(localStorage.getItem(DK) || '[]').filter(x => (x.page || x.k) === K).forEach(x => { const S = session(x); SS.push(S); if (x.st === 'run') run(S); }); } catch {}
  paintDock();
  $('.chip').addEventListener('mousedown', e => e.preventDefault());  // 선택이 풀리지 않게
  $('.chip').addEventListener('click', e => { const c = e.target.closest('[data-c]')?.dataset.c, p = pick || anyPick(); if (c === 'a') openAsk(p); else if (c) explain(p); });
  $('.card form').addEventListener('submit', e => { e.preventDefault(); const i = $('.card input'), q = i.value.trim();
    if (!q || !X || X.busy) return; i.value = ''; const S0 = X;
    sessOf(S0.k).then(si => si?.sid ? askCard(S0, q) : ask(S0, {term: S0.term, ctx: S0.ctx, head: S0.head, q, prev: S0.last}, '↳ ' + q)); });
  const escCard = () => { const lb = $('.card .lb'); if (!lb.hidden) lb.hidden = true; else minimize(); };
  $('.card').addEventListener('keydown', e => { e.stopPropagation(); if (e.key === 'Escape') escCard();
    else if (e.key === 'f' && !e.metaKey && !e.ctrlKey && !/^(INPUT|TEXTAREA)$/.test((e.composedPath()[0] || e.target).tagName)) { e.preventDefault(); setFull(!FULL, true); } });  // 페이지 단축키로 새지 않게
  R.addEventListener('click', e => {
    const t = e.target, x = t.closest('[data-x]')?.dataset.x, lb = $('.card .lb');
    const dx = t.closest('[data-dx]'); if (dx) { e.stopPropagation(); return drop(SS[+dx.dataset.dx]); }
    const tb = t.closest('.tb'); if (tb) { const S = SS[+tb.dataset.s]; if (!S) return; if (S === X) return minimize(); hist.length = 0; return open(S); }
    if (t.classList?.contains('ov')) return minimize();
    if (!X) return;
    if (x === 'close' || x === 'min') minimize();
    else if (x === 'full') setFull(!FULL, true);
    else if (x === 'deep') { if (!X.busy) ask(X, {term: X.term, ctx: X.ctx, head: X.head, mode: 'deep'}, '자세히'); }
    else if (x === 're') run(X, true);
    else if (x === 'back' && hist.length) { const P = hist.pop(); if (SS.includes(P)) open(P); else minimize(); }
    else if (t.closest('.rel')) { const term = t.closest('.rel').dataset.t;
      explain({term, ctx: clip(X.last, term, 800), head: X.term + ' 카드의 같이 볼 용어', inner: true}); }
    else if (t.closest('.lb')) lb.hidden = true;
    else if (t.closest('figure')) { const it = X.gal._items?.[+t.closest('figure').dataset.i]; if (!it) return;
      const big = it.src.replace(/\/(\d+)px-/, '/1280px-');
      lb.innerHTML = `<img src="${esc(big)}" alt=""><div>${esc(it.cap)}<br><a href="https://en.wikipedia.org/wiki/${encodeURIComponent(it.page)}" target="_blank" rel="noopener">위키백과 ${esc(it.page.replace(/_/g, ' '))} →</a></div>`;
      lb.querySelector('img').onerror = function () { if (this.src !== it.src) this.src = it.src; };
      lb.hidden = false; }
  });
  const onEsc = e => { if (e.key === 'Escape' && !$('.ov').hidden) escCard(); };
  addEventListener('keydown', onEsc);

  const toggleDone = () => post('done', {k: K, done: !D?.done_at}).then(j => { take(j); toast(D?.done_at ? '읽음 표시' : '완료 취소', 1500); });
  const jumpMark = dir => {
    const r = ratio(), list = [...M].sort((a, b) => a.ratio - b.ratio);
    const m = dir > 0 ? list.find(x => x.ratio > r + 0.005) : list.reverse().find(x => x.ratio < r - 0.005);
    if (m) goTo(JSON.parse(m.anchor || 'null'), m.ratio);
  };
  let tt;
  function toast(html, ms, actions) {
    const t = $('.toast'); t.innerHTML = '<span>' + html + '</span>'; t.hidden = false;
    (actions || []).forEach(([label, fn]) => { const b = document.createElement('button'); b.textContent = label; b.onclick = () => { fn(); t.hidden = true; }; t.append(b); });
    clearTimeout(tt); if (ms) tt = setTimeout(() => t.hidden = true, ms);
  }

  R.addEventListener('click', e => {
    if (e.target.closest('.rl')) { const rl = $('.rl'), q = rl.getBoundingClientRect(), H = span() + (S ? S.clientHeight : innerHeight), vh = S ? S.clientHeight : innerHeight;
      return scrollToY(Math.max(0, (e.clientY - q.top) / q.height * H - vh / 2)); }
    const a = e.target.closest('[data-a]')?.dataset.a, del = e.target.closest('[data-del]')?.dataset.del, mk = e.target.closest('.mk');
    if (e.target.closest('.fab')) { const p = $('.pan'); p.hidden = !p.hidden; if (!p.hidden) drawPanel(); return; }
    if (del) { e.stopPropagation(); if (confirm('책갈피 삭제?')) post('mark', {k: K, del}).then(take); return; }
    if (a === 'b') addMark(false); else if (a === 'm') addMark(true); else if (a === 'd') toggleDone(); else if (a === 'nu') nextUnread(1); else if (a === 'pu') nextUnread(-1);
    else if (a === 'dev') { const d = prompt('이 기기 이름', DEV); if (d) { setDev(d.trim()); drawPanel(); } }
    else if (mk) { const m = M.find(x => x.id == mk.dataset.id); if (m) goTo(JSON.parse(m.anchor || 'null'), m.ratio); }
  });
  const onKey = e => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
    const t = e.composedPath()[0] || e.target; if (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    if (e.key === 'f' && !$('.ov').hidden) { e.preventDefault(); return setFull(!FULL, true); }
    const f = {a: () => openAsk(anyPick()), e: () => explain(anyPick()), b: () => addMark(false), m: () => addMark(true), d: toggleDone, '[': () => jumpMark(-1), ']': () => jumpMark(1), n: () => nextUnread(1), N: () => nextUnread(-1)}[e.key];
    if (f) { e.preventDefault(); f(); }
  };
  addEventListener('keydown', onKey);
  const seen = new WeakSet();
  const hookFrame = f => { try { if (f.contentDocument) hookDoc(f.contentDocument); } catch {} };
  const hookDoc = d => {
    if (seen.has(d)) return; seen.add(d);
    d.addEventListener('selectionchange', onSel);
    // 다른 문서를 누르면 이전 문서의 선택이 남으므로 지운다(칩을 누를 때는 유지)
    d.addEventListener('pointerdown', e => { if (e.target === host) return;
      for (const x of allDocs()) if (x !== d) x.getSelection()?.removeAllRanges(); }, true);
    d.addEventListener('load', e => { if (e.target.tagName === 'IFRAME') hookFrame(e.target); }, true);  // srcdoc은 늦게 채워진다
    if (d !== document) { d.addEventListener('keydown', onKey); d.addEventListener('keydown', onEsc); }
    d.querySelectorAll('iframe').forEach(hookFrame);
  };
  hookDoc(document);

  const onScroll = () => { dirty = true; lastAct = Date.now(); if (restored) userMoved = true; paint(); rbPos(); };
  ['pointerdown', 'keydown', 'touchstart', 'mousemove', 'wheel'].forEach(ev => addEventListener(ev, () => lastAct = Date.now(), {passive: true}));
  setInterval(() => { if (document.visibilityState === 'visible' && Date.now() - lastAct < 60000) active += 1; }, 1000);
  setInterval(() => { if (dirty || active >= 15) flush(false); }, 2000);
  addEventListener('pagehide', () => flush(true));
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flush(true); });

  // ---------- 덩어리 단위 읽음 ----------
  // 문단·그림·표 같은 덩어리를 내용으로 이름 붙인다(태그+글의 해시). 화면에 충분히 오래 머문 덩어리만 '읽음'으로 확정한다.
  // 내용이 바뀌거나 새로 생긴 덩어리는 이름이 달라지므로 저절로 '안 읽음'이 되고, 첫 방문 뒤에 생긴 것은 '바뀐 곳'(노랑)으로 칠한다.
  // 문서 DOM은 건드리지 않고(화면 반영 비교가 깨지지 않게) 그림자 DOM 위에 줄을 그린다.
  const RBSEL = 'h1,h2,h3,h4,h5,h6,p,li,pre,blockquote,figure,table,dt,dd,img,svg,iframe,video,canvas';
  const RBMEDIA = /^(FIGURE|IMG|SVG|svg|IFRAME|VIDEO|CANVAS|TABLE)$/;
  let ACKP = [], BL = [], RD = new Set(), KN = new Set(), FR = new Set(), RBASE = null, PEND = new Set(), SEENP = [], COV = null, RBVIS = new Map(), RBIO = null, RBT = 0;
  const fnv = s => { let h = 0x811c9dc5; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0).toString(36); };
  const bKey = el => {
    const tg = el.tagName.toUpperCase();
    if (tg === 'IMG') return 'img:' + (el.getAttribute('src') || '').slice(-160) + (el.alt || '');
    if (tg === 'IFRAME') { const sd = el.getAttribute('srcdoc'); return 'if:' + (sd ? fnv(sd) + sd.length : el.getAttribute('src') || ''); }
    const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    const im = el.querySelectorAll ? [...el.querySelectorAll('img')].map(i => (i.getAttribute('src') || '').slice(-80)).join(',') : '';
    return tg + ':' + (t.length > 6000 ? t.slice(0, 3000) + t.length + t.slice(-3000) : t) + im + (!t && !im ? el.outerHTML.slice(0, 4000) : '');
  };
  function rbScan() {
    const all = [...document.body.querySelectorAll(RBSEL)], set = new Set(all), cnt = new Map(), out = [], newSeen = [];
    for (const el of all) {
      let nested = false; for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) if (set.has(p)) { nested = true; break; }
      if (nested) continue;
      const r = el.getBoundingClientRect(); if (r.height < 4 || r.width < 4) continue;
      // 접힌 details 안은 크롬이 크기를 남겨 두고 그리기만 건너뛴다(content-visibility). 보이지 않는 덩어리는 세지도 칠하지도 않는다. 펼치면 다시 센다
      if (el.checkVisibility && !el.checkVisibility({visibilityProperty: true})) continue;
      const k0 = bKey(el); const t = el.tagName.toUpperCase();
      if (!RBMEDIA.test(t) && k0.length < t.length + 3) continue;  // 빈 문단
      const base = t + fnv(k0), n = cnt.get(base) || 0; cnt.set(base, n + 1);
      const h = n ? base + '.' + n : base;  // 똑같은 덩어리가 여럿이면 순번
      const len = (el.textContent || '').replace(/\s+/g, '').length, media = RBMEDIA.test(t) && len < 400;
      out.push({el, h, w: media ? Math.max(250, len) : Math.min(Math.max(len, 30), 3000), need: media ? 2000 : Math.min(Math.max(len * 25, 1200), 7000), dw: 0});
      if (!KN.has(h)) { KN.add(h); newSeen.push(h); if (RBASE && Date.now() / 1000 - RBASE > 120 && !RD.has(h)) FR.add(h); }
    }
    const old = new Map(BL.map(b => [b.h, b.dw])); out.forEach(b => b.dw = old.get(b.h) || 0);  // 다시 셀 때 머문 시간 유지
    // 덩어리 기록이 처음인 문서: 예전 방식(스크롤 최고 깊이)으로 읽은 데까지는 읽음으로 옮겨 준다(고쳐진 뒤면 옮기지 않는다)
    if (!RBASE && !RD.size && newSeen.length && D?.max_ratio > 0.02 && !(D.revised_at > (D.last_open || 0))) {
      const lim = D.max_ratio * span() + (S ? S.clientHeight : innerHeight);
      for (const b of out) if (docY(b.el.getBoundingClientRect()) + 8 < lim) { RD.add(b.h); PEND.add(b.h); } }
    BL = out; if (newSeen.length) { SEENP.push(...newSeen); if (!RBASE) RBASE = Date.now() / 1000; dirty = true; }
    RBIO?.disconnect(); RBVIS.clear();
    // 바뀐 곳(노랑)은 화면에 한 번 들어왔다가 지나가면 지운다(읽음 확정과는 따로, 초록은 여전히 머문 시간으로만)
    const byEl = new Map(BL.map(b => [b.el, b]));
    RBIO = new IntersectionObserver(es => es.forEach(e => {
      const was = RBVIS.get(e.target) > 0; RBVIS.set(e.target, e.isIntersecting ? e.intersectionRect.height : 0);
      const b = byEl.get(e.target);
      if (b && was && !e.isIntersecting && FR.has(b.h) && document.visibilityState === 'visible' && $('.ov').hidden) {
        FR.delete(b.h); ACKP.push(b.h); dirty = true; rbCov(); rbDraw(); }
    }), {threshold: [0, .1, .25, .4, .6, .8, 1]});
    BL.forEach(b => RBIO.observe(b.el));
    rbCov(); rbDraw();
  }
  function rbCov() {
    let tw = 0, rw = 0, nr = 0, nf = 0;
    for (const b of BL) { tw += b.w; if (RD.has(b.h)) { rw += b.w; nr++; } else if (FR.has(b.h)) nf++; }
    const c = {total: BL.length, read: nr, new: nf, pct: tw ? Math.round(rw / tw * 1000) / 1000 : 0};
    if (!COV || c.read !== COV.read || c.total !== COV.total || c.new !== COV.new) { COV = c; dirty = true; }
    paint();
  }
  // 0.5초마다: 화면에 충분히 보이는 안 읽은 덩어리 중 맨 위 것 하나에만 시간을 준다(위에서부터 차례로 읽는다고 본다)
  setInterval(() => {
    if (document.visibilityState !== 'visible' || Date.now() - lastAct > 60000 || !$('.ov').hidden || !BL.length) return;
    let cur = null, ct = Infinity;
    for (const b of BL) {
      if (RD.has(b.h)) continue; const vh = RBVIS.get(b.el); if (!vh) continue;
      const bh = b.el.getBoundingClientRect(); if (vh < Math.min(bh.height * 0.6, innerHeight * 0.45)) continue;
      if (bh.top < ct) { ct = bh.top; cur = b; }
    }
    if (!cur) return;
    cur.dw += 500;
    if (cur.dw >= cur.need) { RD.add(cur.h); PEND.add(cur.h); FR.delete(cur.h); rbCov(); rbDraw(cur.h); }
  }, 500);
  const rbBody = () => { const o = {}; if (PEND.size) { o.reads = [...PEND]; PEND.clear(); } if (SEENP.length) { o.seen = SEENP.splice(0); } if (ACKP.length) { o.acks = ACKP.splice(0); } if (COV) o.cov = COV; return o; };
  const rbLoad = x => { RBASE = x.base || null; RD = new Set(x.read || []); KN = new Set([...(x.read || []), ...(x.known || [])]); FR = new Set(x.fresh || []); };
  // 그리기: 덩어리 왼쪽 여백에 세로 줄(읽음 초록·안 읽음 회색·바뀐 곳 노랑+옅은 칠), 오른쪽 끝에 문서 전체 지도
  const docY = r => r.top - viewTop() + top();
  function rbDraw(justRead) {
    const g = $('.rg i'), rl = $('.rl'); if (!g) return;
    const H = Math.max(1, span() + (S ? S.clientHeight : innerHeight)), RH = rl.clientHeight || innerHeight;
    const runs = [];
    for (const b of BL) {
      const r = b.el.getBoundingClientRect(), st = RD.has(b.h) ? 'r' : FR.has(b.h) ? 'n' : 'u', y0 = docY(r), y1 = y0 + r.height, L = runs.at(-1);
      if (L && L.st === st && y0 - L.y1 < 48 && !(justRead && b.h === justRead)) { L.y1 = Math.max(L.y1, y1); L.x = Math.min(L.x, r.left); L.x2 = Math.max(L.x2, r.right); }
      else runs.push({st, y0, y1, x: r.left, x2: r.right, fresh: justRead && b.h === justRead});
    }
    g.innerHTML = runs.map(u => `<b class="${u.st}${u.fresh ? ' now' : ''}" style="top:${u.y0}px;height:${u.y1 - u.y0}px;left:${Math.max(2, u.x - 12)}px"></b>`
      + (u.st === 'n' ? `<s style="top:${u.y0 - 2}px;height:${u.y1 - u.y0 + 4}px;left:${Math.max(0, u.x - 6)}px;width:${u.x2 - u.x + 12}px"></s>` : '')).join('');
    rl.innerHTML = runs.map(u => `<b class="${u.st}" style="top:${u.y0 / H * RH}px;height:${Math.max(2, (u.y1 - u.y0) / H * RH)}px"></b>`).join('') + '<i></i>';
    rbPos();
  }
  function rbPos() {
    const g = $('.rg i'); if (g) g.style.transform = `translateY(${viewTop() - top()}px)`;
    const v = $('.rl i'); if (v) { const H = Math.max(1, span() + (S ? S.clientHeight : innerHeight)), RH = $('.rl').clientHeight || innerHeight, vh = S ? S.clientHeight : innerHeight;
      v.style.top = (top() / H * RH) + 'px'; v.style.height = Math.max(6, vh / H * RH) + 'px'; }
  }
  // 다음(n)·이전(N) 안 읽은 곳으로. 바뀐 곳이 있으면 그것부터
  function nextUnread(dir) {
    const y = top() + (S ? S.clientHeight : innerHeight) * 0.3, un = BL.filter(b => !RD.has(b.h));
    if (!un.length) return toast('안 읽은 곳이 없습니다', 2000);
    const pick = list => { const ys = list.map(b => ({b, y: docY(b.el.getBoundingClientRect())}));
      return dir > 0 ? (ys.find(o => o.y > y + 4) || ys[0]) : ([...ys].reverse().find(o => o.y < y - 4) || ys.at(-1)); };
    const fr = un.filter(b => FR.has(b.h)), o = pick(fr.length ? fr : un);
    scrollToY(Math.max(0, o.y - (S ? S.clientHeight : innerHeight) * 0.3));
    toast(`${FR.has(o.b.h) ? '바뀐 곳' : '안 읽은 곳'} · 남은 ${un.length}개${fr.length ? ` (바뀐 곳 ${fr.length})` : ''}`, 1800);
  }
  let rbTo = 0; const rbLater = (ms = 700) => { clearTimeout(rbTo); rbTo = setTimeout(rbScan, ms); };
  new MutationObserver(() => rbLater()).observe(document.body, {childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['open', 'src', 'hidden', 'srcdoc']});
  addEventListener('resize', () => rbLater(300));
  try { new ResizeObserver(() => rbLater(500)).observe(document.body); } catch {}

  async function start() {
    document.documentElement.append(host);
    S = findScroller();
    (S || window).addEventListener('scroll', onScroll, {passive: true});
    const j = await fetch(API + 'doc?k=' + encodeURIComponent(K)).then(r => r.json()).catch(() => ({}));
    take(j);
    const h = location.hash.match(/^#__rm=(\d+)$/), prev = D;
    const revised = prev && prev.revised_at && prev.revised_at > (prev.last_open || 0);
    let go = null; try { go = JSON.parse(sessionStorage.getItem('wimgo') || 'null'); sessionStorage.removeItem('wimgo'); } catch {}
    if (go) { goTo(go.a, go.r); setTimeout(() => { if (!userMoved) goTo(go.a, go.r); }, 900); toast('고친 문서를 다시 불러왔습니다', 3000); }
    else if (h) { const m = M.find(x => x.id == h[1]); if (m) goTo(JSON.parse(m.anchor || 'null'), m.ratio); }
    else if (prev && prev.cur_ratio > 0.02 && !location.hash) {
      const a = JSON.parse(prev.cur_anchor || 'null');
      goTo(a, prev.cur_ratio);
      setTimeout(() => { if (!userMoved) goTo(a, prev.cur_ratio); }, 900);  // 늦게 그려지는 그림 대비
      toast(`이어 읽기 · ${pct(prev.cur_ratio)} <span style="color:var(--mu)">(${esc(prev.last_device || '')} ${ago(prev.updated_at)})</span>`, 6000, [['맨 위로', () => scrollToY(0)]]);
    }
    setTimeout(rbScan, 300);
    if (revised) setTimeout(() => { const n = BL.filter(b => FR.has(b.h)).length;
      toast(n ? `문서가 고쳐졌습니다 · 바뀐 곳 ${n}군데는 노란 줄` : '문서가 고쳐졌습니다', 6000, n ? [['바뀐 곳으로 (n)', () => nextUnread(1)]] : []); }, h || prev.cur_ratio > 0.02 ? 6200 : 1500);
    setTimeout(() => { restored = true; }, 1200);
    restoreAsk();
    post('doc', body({open: true, title: document.title})).then(take).catch(() => {});
  }
  if (document.readyState === 'complete') start(); else addEventListener('load', start);
})();
"""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>읽기 기록</title>
<style>
:root{--bg:#f6f6f8;--card:#fff;--fg:#1d1d1f;--mu:#6e6e73;--ac:#2f6fed;--ok:#1f9d55;--ln:#e3e3e8;--tr:#ececf0}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141416;--card:#1f1f22;--fg:#f2f2f5;--mu:#a1a1a8;--ac:#6c9cff;--ok:#3ccf7a;--ln:#33333a;--tr:#2c2c31}}
:root[data-theme="dark"]{--bg:#141416;--card:#1f1f22;--fg:#f2f2f5;--mu:#a1a1a8;--ac:#6c9cff;--ok:#3ccf7a;--ln:#33333a;--tr:#2c2c31}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,system-ui,"Apple SD Gothic Neo",sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:20px;margin:0 0 4px}.mu{color:var(--mu)}
.stats{display:flex;gap:10px;margin:14px 0;flex-wrap:wrap}
.stat{background:var(--card);border:1px solid var(--ln);border-radius:10px;padding:10px 14px;min-width:110px;cursor:pointer}
.stat b{display:block;font-size:20px}.stat.on{border-color:var(--ac);box-shadow:0 0 0 1px var(--ac)}
.ctl{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
input,select{background:var(--card);color:var(--fg);border:1px solid var(--ln);border-radius:8px;padding:7px 10px;font:inherit}
input{flex:1;min-width:180px}
.tabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--mu);padding:6px 4px;margin-right:12px;font:inherit;cursor:pointer}
.tabs button.on{color:var(--fg);border-color:var(--ac)}
h3{font-size:13px;color:var(--mu);margin:20px 0 6px;font-weight:600}
.it{display:grid;grid-template-columns:1fr auto;gap:2px 12px;background:var(--card);border:1px solid var(--ln);border-radius:10px;padding:10px 12px;margin-bottom:6px}
.it a{color:var(--fg);text-decoration:none;font-weight:500;overflow-wrap:anywhere}.it a:hover{color:var(--ac)}
.meta{font-size:12px;color:var(--mu);grid-column:1/-1;display:flex;gap:10px;flex-wrap:wrap}
.track{grid-column:1/-1;height:4px;background:var(--tr);border-radius:2px;position:relative;overflow:hidden}
.track i{position:absolute;left:0;top:0;bottom:0;background:var(--ac);border-radius:2px}.track.done i{background:var(--ok)}
.pct{font-variant-numeric:tabular-nums;font-weight:600;color:var(--ac)}.pct.done{color:var(--ok)}.rv{font-size:11px;font-weight:600;color:#c2410c;border:1px solid currentColor;border-radius:8px;padding:0 5px}
.lan{background:var(--card);border:1px dashed var(--ln);border-radius:10px;padding:10px 12px;margin-top:10px;font-size:12px;overflow-wrap:anywhere}
.empty{padding:30px;text-align:center;color:var(--mu)}
</style></head><body><main>
<h1>읽기 기록</h1><div class="mu">문서 폴더 HTML의 스크롤 깊이·책갈피. 문서 안에서 <b>b</b> 책갈피 · <b>m</b> 메모 · <b>[ ]</b> 이동 · <b>d</b> 읽음.</div>
<div id="lan"></div>
<div class="stats" id="stats"></div>
<div class="tabs"><button data-t="docs" class="on">문서</button><button data-t="marks">책갈피·메모</button></div>
<div class="ctl"><input id="q" placeholder="제목·경로·메모 검색" autocomplete="off">
<select id="sort"><option value="group">폴더순</option><option value="recent">최근 열람</option><option value="pct">진행도</option><option value="new">새 문서</option></select></div>
<div id="out"></div>
</main>
<script>
const DATA = __DATA__;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const href = k => '/' + k.split('/').map(encodeURIComponent).join('/');
const ago = t => { if (!t) return ''; const s = Date.now()/1000 - t;
  return s < 3600 ? Math.max(1, Math.round(s/60)) + '분 전' : s < 86400 ? Math.round(s/3600) + '시간 전' : Math.round(s/86400) + '일 전'; };
const pct = r => Math.round((r || 0) * 100) + '%';
const st = (() => { try { return JSON.parse(localStorage.getItem('reader_dash') || '{}'); } catch { return {}; } })();
let tab = st.tab || 'docs', filt = st.filt || 'all';
const save = () => { try { localStorage.setItem('reader_dash', JSON.stringify({tab, filt, sort: sort.value})); } catch {} };
if (st.sort) sort.value = st.sort;
if (DATA.lan) lan.innerHTML = `<div class="lan">폰·다른 PC(같은 와이파이)에서 이 주소를 한 번 열면 쿠키가 남는다: <b>${esc(DATA.lan)}</b></div>`;
const titleOf = Object.fromEntries(DATA.items.map(i => [i.k, i.title]));

function stats() {
  const n = s => DATA.items.filter(i => s === 'all' || i.state === s || (s === 'marked' && i.marks)).length;
  stats_el.innerHTML = [['all','전체'],['unread','안 읽음'],['reading','읽는 중'],['done','완료'],['marked','책갈피']]
    .map(([k, l]) => `<div class="stat ${filt === k ? 'on' : ''}" data-f="${k}"><b>${n(k)}</b><span class="mu">${l}</span></div>`).join('');
}
const stats_el = document.getElementById('stats');
function docs() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  let xs = DATA.items.filter(i => (filt === 'all' || i.state === filt || (filt === 'marked' && i.marks))
    && (!q || (i.title + ' ' + i.k).toLowerCase().includes(q)));
  const s = sort.value;
  if (s === 'recent') xs.sort((a, b) => (b.last_open || 0) - (a.last_open || 0));
  if (s === 'pct') xs.sort((a, b) => (b.prog || 0) - (a.prog || 0));
  if (s === 'new') xs.sort((a, b) => b.mtime - a.mtime);
  if (!xs.length) return '<div class="empty">해당 문서 없음</div>';
  let out = '', last = '';
  for (const i of xs) {
    const g = s === 'group' ? i.group + ' · ' + i.folder : '';
    if (g && g !== last) { out += `<h3>${esc(g)}</h3>`; last = g; }
    const done = !!i.done_at, r = done ? 1 : (i.prog || 0);
    const meta = [i.last_open ? `${esc(i.last_device || '')} ${ago(i.last_open)}` : '안 열어 봄',
      i.opens ? i.opens + '회' : '', i.active_sec ? Math.round(i.active_sec / 60) + '분' : '',
      i.marks ? '책갈피 ' + i.marks : '', s !== 'group' ? esc(i.folder) : '',
      i.blk_total && !done ? `안 읽은 덩어리 ${i.blk_total - (i.blk_read || 0)}${i.blk_new ? ` · 바뀐 곳 ${i.blk_new}` : ''}` : '',
      i.cur_ratio > 0.02 && !done ? `지금 위치 ${pct(i.cur_ratio)}` : ''].filter(Boolean).join(' · ');
    out += `<div class="it"><a href="${href(i.k)}">${esc(i.title)}</a><span class="pct ${done ? 'done' : ''}">${i.revised ? '<span class="rv">고쳐짐</span> ' : ''}${done ? '완료' : pct(i.prog)}</span>
      <div class="track ${done ? 'done' : ''}"><i style="width:${r * 100}%"></i></div><div class="meta">${meta}</div></div>`;
  }
  return out;
}
function marks() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const xs = DATA.marks.filter(m => !q || ((m.note || '') + ' ' + m.key + ' ' + (titleOf[m.key] || '')).toLowerCase().includes(q));
  if (!xs.length) return '<div class="empty">책갈피 없음. 문서에서 b(책갈피)·m(메모)</div>';
  return xs.map(m => `<div class="it"><a href="${href(m.key)}#__rm=${m.id}">${esc(m.note || '책갈피')}</a><span class="pct">${pct(m.ratio)}</span>
    <div class="meta">${esc(titleOf[m.key] || m.key)} · ${esc(m.device)} · ${ago(m.created_at)}</div></div>`).join('');
}
function draw() {
  stats();
  document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.t === tab));
  sort.style.display = tab === 'docs' ? '' : 'none';
  document.getElementById('out').innerHTML = tab === 'docs' ? docs() : marks();
  save();
}
document.addEventListener('click', e => {
  const f = e.target.closest('[data-f]'), t = e.target.closest('[data-t]');
  if (f) { filt = f.dataset.f; tab = 'docs'; draw(); } if (t) { tab = t.dataset.t; draw(); }
});
document.getElementById('q').addEventListener('input', draw);
sort.addEventListener('change', draw);
addEventListener('pageshow', e => { if (e.persisted) location.reload(); });
draw();
</script></body></html>
"""

if __name__ == "__main__":
    main()
