# /// script
# dependencies = ["pillow"]
# ///
"""파트(T1, T2 …)마다 한 장: html-brief로 빌드한 정리 층 + 그 파트 원문 전문(iframe) 부록.
usage: uv run <skill>/scripts/bundle/build_parts.py <묶음폴더> [T1 T3 …]
각 <묶음폴더>/_build/parts/TN 조각을 build.py로 다시 빌드한 뒤 원문을 붙여 <묶음폴더>/TN_<이름>_통합_<날짜>_local.html을 쓴다.
"""
import re, os, sys, json, glob, subprocess, urllib.parse
if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
    sys.exit(__doc__)
os.environ["BUNDLE_DIR"] = os.path.abspath(sys.argv[1])
sys.path.insert(0, os.path.dirname(__file__))
from merge_lib import *  # noqa: F401,F403  themes, images, img_token, inline_rel_images, vh_fix, INJECT, ESC, READER, ROOT, DATA_RE, BUNDLE, DATE

B = BUNDLE
PARTS = f"{B}/_build/parts"
S = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
only = set(sys.argv[2:])

def out_name(th):
    # 묶음 폴더에 TN_<이름> 바로가기 폴더가 있으면 그 이름, 없으면 주제명
    d = [os.path.basename(x)[3:] for x in glob.glob(f"{B}/{th['tag']}_*") if os.path.isdir(x)]
    name = d[0] if d else re.sub(r"[^\w가-힣]+", "_", re.sub(r"<[^>]+>", "", th["name"])).strip("_")
    return f'{th["tag"]}_{name}_통합_{DATE}_local.html'

# 문서 번호 → 들어 있는 파트 파일(다른 파트로 가는 링크용)
num2file = {c["num"]: out_name(th) for th in themes for c in th["cards"]}

CSS = """
.navp{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 18px;font-size:13px}
.navp a{padding:2px 9px;border:1px solid var(--line,#ddd);border-radius:12px;text-decoration:none;color:inherit}
.navp a.cur{font-weight:700;border-color:currentColor}
a.ref{font-size:12px;text-decoration:none;white-space:nowrap}
.orig{margin:60px 0 0;border-top:3px solid currentColor;padding-top:6px}
.doc{margin:30px 0 0}
.dh{position:sticky;top:0;z-index:5;background:var(--bg,#fff);border-bottom:1px solid var(--line,#ddd);padding:8px 0}
.dh h3{margin:0;font-size:17px;display:inline}.dh p{margin:2px 0 0;font-size:13px;opacity:.75}.dh a.o{float:right;font-size:12px}
.doc iframe{display:block;width:calc(100vw - 40px);max-width:1180px;position:relative;left:50%;transform:translateX(-50%);border:0;height:900px;background:transparent}
.ld{font-size:13px;opacity:.7;padding:8px 0}
"""

LOADER = r"""<script>(function(){
var lines=null,blobs={};
function imgURL(n){if(!lines)lines=document.getElementById('imgs').textContent.split('\n');
 if(blobs[n])return blobs[n];var p=lines[n],sp=p.indexOf(' '),mime=p.slice(0,sp),b=atob(p.slice(sp+1)),u=new Uint8Array(b.length);
 for(var i=0;i<b.length;i++)u[i]=b.charCodeAt(i);return blobs[n]=URL.createObjectURL(new Blob([u],{type:mime}));}
// 정리 층 그림(wimimg 토큰) 복원
document.querySelectorAll('[src^="wimimg:"],[href^="wimimg:"]').forEach(function(el){['src','href'].forEach(function(a){var v=el.getAttribute(a);if(v&&v.indexOf('wimimg:')===0)el.setAttribute(a,imgURL(+v.slice(7)));});});
var frames=[].slice.call(document.querySelectorAll('iframe[data-n]')),byN={},done={};
frames.forEach(function(f){byN[f.dataset.n]=f;});
function vh(){return (innerHeight/100)+'px';}
function load(n){if(done[n])return done[n];var f=byN[n];
 done[n]=new Promise(function(res){
  var src=document.getElementById('doc'+n).textContent.replace(/<\\\/scr~ipt/g,'</scr'+'ipt').replace(/wimimg:(\d+)/g,function(_,k){return imgURL(+k);});
  src=src.replace(/<head([^>]*)>/i,function(m){return m+'<style>:root{--wvh:'+vh()+'}</style>';});
  f.onload=function(){var l=f.previousElementSibling;if(l&&l.className==='ld')l.remove();res();};
  f.srcdoc=src;});
 return done[n];}
function go(n){var s=document.getElementById('d'+n);if(!s)return false;s.scrollIntoView();load(n).then(function(){setTimeout(function(){s.scrollIntoView();},400);});return true;}
window.addEventListener('message',function(e){var d=e.data;if(!d||!d.wim)return;var f=byN[d.id];if(!f)return;
 if(d.h)f.style.height=d.h+'px';
 if(d.y!=null){scrollTo({top:f.getBoundingClientRect().top+scrollY+d.y-60,behavior:'smooth'});}
 if(d.nav){var u=d.nav.split('#')[0],k=URLMAP[u]||URLMAP[decodeURI(u)];
  if(k&&byN[k]){go(k);}else if(k){location.href=encodeURIComponent(NUM2FILE[k])+'#d'+k;}else{window.open(d.nav,'_blank');}}});
document.addEventListener('click',function(e){var a=e.target.closest('a[href^="#d"]');if(!a)return;var n=a.getAttribute('href').slice(2);
 if(byN[n]){e.preventDefault();history.replaceState(null,'','#d'+n);go(n);}else if(NUM2FILE[n]){e.preventDefault();location.href=encodeURIComponent(NUM2FILE[n])+'#d'+n;}});
if(/^#d\d+$/.test(location.hash))go(location.hash.slice(2));
var io=new IntersectionObserver(function(es){es.forEach(function(x){if(x.isIntersecting)load(x.target.dataset.n);});},{rootMargin:'2000px 0px'});
frames.forEach(function(f){io.observe(f);});
(async function(){for(var i=0;i<frames.length;i++){await load(frames[i].dataset.n);await new Promise(function(r){setTimeout(r,150);});}})();
addEventListener('resize',function(){frames.forEach(function(f){try{f.contentDocument.documentElement.style.setProperty('--wvh',vh());}catch(_){}});});
})();</script>"""

for th in themes:
    if only and th["tag"] not in only:
        continue
    images.clear(); img_ids.clear()
    pdir = f"{PARTS}/{th['tag']}"
    r = subprocess.run([sys.executable, f"{S}/build.py", pdir], capture_output=True, text=True, cwd=ROOT)
    m = re.search(r"정본 (.+)", r.stdout)
    if not m:
        print(th["tag"], "build.py 실패\n", r.stdout[-800:], r.stderr[-800:], file=sys.stderr); continue
    page = open(m.group(1).strip(), encoding="utf-8").read()
    page = inline_rel_images(page, pdir)
    page = DATA_RE.sub(lambda mm: img_token(mm.group(1), re.sub(r"\s", "", mm.group(2))), page)

    # 원문 페이로드
    secs, payload = [], []
    for c in th["cards"]:
        f = f"{ROOT}/{c['rel']}"
        t = open(f, encoding="utf-8", errors="replace").read()
        assert ESC not in t
        doc_dir = os.path.dirname(f)
        t = DATA_RE.sub(lambda mm: img_token(mm.group(1), re.sub(r"\s", "", mm.group(2))), t)
        t = inline_rel_images(t, doc_dir)
        t = vh_fix(t)
        base = f'<base href="{READER}{urllib.parse.quote(os.path.relpath(doc_dir, ROOT))}/" target="_blank">'
        t = re.sub(r"(<head[^>]*>)", lambda mm: mm.group(1) + base, t, count=1, flags=re.I) if re.search(r"<head[^>]*>", t, re.I) else base + t
        inj = INJECT % json.dumps(c["num"])
        t = re.sub(r"</body>", lambda mm: inj + "</body>", t, count=1, flags=re.I) if re.search(r"</body>", t, re.I) else t + inj
        t = re.sub(r"</script", ESC, t, flags=re.I)
        payload.append(f'<script type="text/plain" id="doc{c["num"]}">{t}</script>')
        secs.append(f'<section class="doc" id="d{c["num"]}"><div class="dh"><a class="o" href="{READER}{c["href"]}" target="_blank">원본 따로 열기 ↗</a>'
                    f'<h3>[{c["num"]}] {c["title"]}</h3><p>{c["desc"]}</p></div><div class="ld">불러오는 중…</div>'
                    f'<iframe title="{c["title"]}" data-n="{c["num"]}"></iframe></section>')

    nav = '<nav class="navp">' + "".join(
        f'<a href="{urllib.parse.quote(out_name(t2))}"{" class=cur" if t2 is th else ""}>{t2["tag"]} {t2["name"].split("·")[0].strip()}</a>' for t2 in themes
    ) + '<a href="#p01-orig">↓ 원문 전문</a></nav>'
    toc = "".join(f'<li><a href="#d{c["num"]}">[{c["num"]}] {c["title"]}</a></li>' for c in th["cards"])
    appendix = (f'<h2 id="p01-orig" class="orig">원문 전문 ({len(th["cards"])}개)</h2>'
                f'<p>위 정리의 [NN] 태그가 가리키는 원문 전체다. 줄이거나 고치지 않았다.</p><ul class="src">{toc}</ul>' + "".join(secs))

    page = page.replace("</style>", CSS + "</style>", 1)
    page = re.sub(r"(<main>\s*)", lambda mm: mm.group(1) + nav, page, count=1)
    page = page.replace("</main>", appendix + "</main>", 1)
    urlmap = {}
    for t2 in themes:
        for c in t2["cards"]:
            urlmap[READER + c["href"]] = c["num"]; urlmap[READER + urllib.parse.quote(c["rel"])] = c["num"]
    tail = ("".join(payload) + '<script type="text/plain" id="imgs">' + "\n".join(f"{mm} {b}" for mm, b in images) + "</script>"
            + "<script>var URLMAP=" + json.dumps(urlmap, ensure_ascii=False) + ";var NUM2FILE=" + json.dumps(num2file, ensure_ascii=False) + ";</script>" + LOADER)
    page = page.replace("</body>", tail + "</body>", 1)
    out = f"{B}/{out_name(th)}"
    open(out, "w", encoding="utf-8").write(page)
    print(th["tag"], os.path.basename(out), round(os.path.getsize(out) / 1e6, 1), "MB", file=sys.stderr)
