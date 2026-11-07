# 목록 페이지 파싱과 원문 iframe 페이로드 도우미(bundle_init·build_parts·check_parts가 쓴다).
# 환경변수:
#   BUNDLE_DIR        묶음 폴더(호출 스크립트가 첫 인자로 넣는다)
#   HTML_BRIEF_ROOT   리더 URL 경로의 기준 폴더(기본: 현재 작업 디렉터리)
#   HTML_BRIEF_READER 리더 서버 주소(기본: http://127.0.0.1:8770/, WIM Reader). 서버가 없어도 통합본은 열린다.
#                     "file"로 두면 원문 링크·'원본 따로 열기'가 file:// 경로를 쓴다.
#   HTML_BRIEF_INDEX  묶음 폴더 안 목록 페이지 이름(기본: 00_list_local.html)
# data: 이미지는 중복 제거, 큰 그림은 WebP 재압축, vh는 부모 뷰포트로 고정.
import re, os, sys, json, base64, hashlib, html, mimetypes, urllib.parse

ROOT = os.path.abspath(os.environ.get("HTML_BRIEF_ROOT") or os.getcwd())
BUNDLE = os.environ["BUNDLE_DIR"]  # 호출 스크립트가 첫 인자로 받은 묶음 폴더
# 인자로 온 한글은 NFC, 디스크 이름은 NFD일 수 있어 glob이 못 찾는다 → 디스크 표기로 맞춘다
import unicodedata as _ud
_par, _nm = os.path.split(BUNDLE.rstrip("/"))
BUNDLE = os.path.join(_par, next((x for x in os.listdir(_par) if _ud.normalize("NFC", x) == _ud.normalize("NFC", _nm)), _nm))
SRC = os.path.join(BUNDLE, os.environ.get("HTML_BRIEF_INDEX", "00_list_local.html"))
DATE = (re.search(r"_(\d{6})$", BUNDLE.rstrip("/")) or re.search(r"(\d{6})", __import__("time").strftime("%y%m%d"))).group(1)
_reader = os.environ.get("HTML_BRIEF_READER", "http://127.0.0.1:8770/")
READER = ("file://" + urllib.parse.quote(ROOT) if _reader in ("", "file", "none") else _reader).rstrip("/") + "/"
ESC = "<\\/scr~ipt"

if not os.path.isfile(SRC):
    sys.exit(f"목록 페이지가 없다: {SRC} (HTML_BRIEF_INDEX로 이름을 바꿀 수 있다)")
idx = open(SRC, encoding="utf-8").read()


def card_paths(href):
    """카드 href → (ROOT 기준 상대경로 rel, 리더 URL 뒷부분 href). 리더 URL이면 ROOT 기준, 아니면 묶음 폴더 기준."""
    if href.startswith(READER):
        tail = href[len(READER):]
        return urllib.parse.unquote(tail), tail
    if re.match(r"^https?://", href):
        sys.exit(f"카드 href는 상대 경로나 {READER}… 여야 한다: {href}")
    f = os.path.normpath(os.path.join(BUNDLE, urllib.parse.unquote(href)))
    rel = os.path.relpath(f, ROOT)
    return rel, urllib.parse.quote(rel)


# ---- 목록 페이지 파싱 ----
themes = []
for sec in re.findall(r'<section class="(t\d)">(.*?)</section>', idx, re.S):
    cls, body = sec
    tag = re.search(r'<span class="tag">(T\d)</span>(.*?)<small>(.*?)</small>', body, re.S)
    cards = []
    for href, num, title, desc in re.findall(
        r'<a class="card" href="([^"]+)"><b>(\d+)</b><div><h3>(.*?)</h3><p>(.*?)</p>', body, re.S):
        rel, rhref = card_paths(href)
        cards.append(dict(rel=rel, href=rhref, num=num, title=title, desc=desc))
    gap = re.search(r'<div class="gap">(.*?)</div>', body, re.S)
    if not tag:
        sys.exit(f'<section class="{cls}"> 안에 <span class="tag">TN</span>…<small>…</small> 제목이 없다')
    themes.append(dict(cls=cls, tag=tag.group(1), name=tag.group(2).strip(), small=tag.group(3).strip(),
                       cards=cards, gap=gap.group(1) if gap else ""))
_svg = re.search(r"<svg.*?</svg>", idx, re.S)
svg = _svg.group(0) if _svg else ""
if not themes:
    sys.exit(f"{SRC}: <section class=\"tN\"> 파트가 없다")

# ---- 이미지 사전 ----
images, img_ids = [], {}

def shrink(mime, b64):
    # 110KB 넘는 그림은 긴 변 1600px WebP로 다시 눌러 담는다(화면에서 구분 안 되는 수준)
    if len(b64) < 150_000 or mime == "image/svg+xml" or mime == "image/gif":
        return mime, b64
    from PIL import Image
    import io
    try:
        im = Image.open(io.BytesIO(base64.b64decode(b64)))
        im.load()
    except Exception:
        return mime, b64
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if "A" in im.getbands() or im.mode == "P" else "RGB")
    im.thumbnail((1600, 1600))
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=72, method=4)
    nb = base64.b64encode(buf.getvalue()).decode()
    return ("image/webp", nb) if len(nb) < len(b64) else (mime, b64)

def img_token(mime, b64):
    k = hashlib.sha1(b64.encode()).hexdigest()
    if k not in img_ids:
        img_ids[k] = len(images)
        images.append(shrink(mime, b64))
    return f"wimimg:{img_ids[k]}"

DATA_RE = re.compile(r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)")
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif")

def inline_rel_images(t, doc_dir):
    def sub(m):
        attr, q, url = m.group(1), m.group(2), m.group(3)
        if re.match(r"^(data:|https?:|#|mailto:|//)", url):
            return m.group(0)
        path = urllib.parse.unquote(url.split("#")[0].split("?")[0])
        if not path.lower().endswith(IMG_EXT):
            return m.group(0)
        f = os.path.normpath(os.path.join(doc_dir, path))
        if not os.path.isfile(f):
            return m.group(0)
        mime = mimetypes.guess_type(f)[0] or "image/png"
        if f.lower().endswith(".webp"):
            mime = "image/webp"
        b64 = base64.b64encode(open(f, "rb").read()).decode()
        return f"{attr}={q}{img_token(mime, b64)}{q}"
    return re.sub(r'\b(src|href|xlink:href|data-full|data-src)=(["\'])([^"\']+)\2', sub, t)

def vh_fix(t):
    # iframe 안의 vh는 iframe 높이를 따라 무한히 커지므로 부모 뷰포트 높이로 고정한다
    fix = lambda css: re.sub(r"(\d+(?:\.\d+)?)vh\b", r"calc(\1 * var(--wvh, 1vh))", css)
    t = re.sub(r"(<style[^>]*>)(.*?)(</style>)", lambda m: m.group(1) + fix(m.group(2)) + m.group(3), t, flags=re.S)
    t = re.sub(r'(style="[^"]*")', lambda m: fix(m.group(1)), t)
    return t

INJECT = """<script>(function(){var ID=%s;
function h(){var d=document.documentElement,b=document.body;return Math.max(d.scrollHeight,b?b.scrollHeight:0,d.offsetHeight);}
var last=0;function post(){var x=h();if(Math.abs(x-last)>2){last=x;parent.postMessage({wim:1,id:ID,h:x},'*');}}
new ResizeObserver(post).observe(document.documentElement);window.addEventListener('load',post);setInterval(post,1500);
document.addEventListener('click',function(e){var a=e.target.closest&&e.target.closest('a[href]');if(!a)return;
var raw=a.getAttribute('href');if(raw.charAt(0)==='#'){var el=document.getElementById(decodeURIComponent(raw.slice(1)))||document.getElementsByName(raw.slice(1))[0];
e.preventDefault();if(el){parent.postMessage({wim:1,id:ID,y:el.getBoundingClientRect().top+scrollY},'*');}return;}
if(/^javascript:/.test(raw))return;e.preventDefault();parent.postMessage({wim:1,id:ID,nav:a.href},'*');},true);
})();</script>"""

