"""Build a single self-contained HTML viewer for the vault.

`m3 wiki generate --html` writes `wiki.html` alongside the Markdown pages. Open it
in any browser — no server, no network, no dependencies. All pages are embedded as
a JSON blob; a small vanilla-JS renderer turns the Markdown into HTML and rewrites
`foo.md` / `foo.md#sec` links into in-page navigation (hash routing), so clicking
through the vault works entirely offline. Fully local: nothing leaves the machine.
"""
from __future__ import annotations

import json


def build_html(pages: dict[str, str], *, title: str = "m3 Wiki") -> str:
    """Return a complete, self-contained HTML document embedding all pages."""
    # Embed the vault as JSON. json.dumps with ensure_ascii keeps it ASCII-safe;
    # escape "</" so a page containing "</script>" can't break out of the tag.
    blob = json.dumps(pages, ensure_ascii=True).replace("</", "<\\/")
    return _TEMPLATE.replace("__TITLE__", _esc(title)).replace("__PAGES_JSON__", blob)


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# The viewer. A deliberately small, dependency-free Markdown subset renderer —
# enough for what the generator emits (headings, lists, links, code, blockquotes,
# tables, hr, bold/italic/code spans, images). Not a general Markdown engine.
_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg:#0f1417; --surface:#161d21; --ink:#e8ebe6; --soft:#a9b2ac; --faint:#78817a;
    --line:#243035; --accent:#45c3af; --accent-soft:#14312e; --code:#101619;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f6f4ef; --surface:#fff; --ink:#1a1f1c; --soft:#4c554e; --faint:#78817a;
            --line:#e0dccf; --accent:#178f7d; --accent-soft:#d6ebe6; --code:#f1efe8; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); line-height:1.6;
    font:16px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
  .app { display:grid; grid-template-columns:300px 1fr; min-height:100vh; }
  aside { border-right:1px solid var(--line); background:var(--surface);
    padding:18px 16px; overflow-y:auto; height:100vh; position:sticky; top:0; }
  aside h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em;
    color:var(--faint); margin:18px 0 8px; }
  aside a { display:block; color:var(--soft); text-decoration:none; padding:3px 6px;
    border-radius:5px; font-size:13.5px; white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis; }
  aside a:hover { background:var(--accent-soft); color:var(--accent); }
  aside a.active { background:var(--accent-soft); color:var(--accent); font-weight:600; }
  main { padding:34px 44px; max-width:900px; overflow-x:auto; }
  .search { width:100%; padding:8px 10px; margin-bottom:10px; border:1px solid var(--line);
    border-radius:7px; background:var(--bg); color:var(--ink); font-size:13px; }
  h1,h2,h3 { line-height:1.25; }
  h1 { font-size:1.9rem; } h2 { font-size:1.4rem; margin-top:1.8em;
    border-bottom:1px solid var(--line); padding-bottom:.2em; } h3 { font-size:1.1rem; }
  a { color:var(--accent); text-underline-offset:2px; }
  code { background:var(--code); padding:1px 5px; border-radius:4px; font-size:.88em;
    font-family:ui-monospace,Consolas,monospace; }
  pre { background:var(--code); padding:14px 16px; border-radius:8px; overflow-x:auto;
    border:1px solid var(--line); }
  pre code { background:none; padding:0; }
  blockquote { border-left:3px solid var(--accent); margin:1em 0; padding:.4em 1em;
    background:var(--surface); border-radius:0 6px 6px 0; color:var(--soft); }
  ul,ol { padding-left:1.4em; } li { margin:.25em 0; }
  table { border-collapse:collapse; width:100%; margin:1em 0; font-size:.92em; }
  th,td { border:1px solid var(--line); padding:7px 11px; text-align:left; }
  th { background:var(--surface); }
  hr { border:0; border-top:1px solid var(--line); margin:1.6em 0; }
  img { vertical-align:middle; }
  .miss { color:var(--faint); font-style:italic; }
  @media (max-width:760px){ .app{grid-template-columns:1fr;} aside{height:auto;position:static;} }
</style>
</head>
<body>
<div class="app">
  <aside>
    <input class="search" id="q" placeholder="Filter pages…" autocomplete="off">
    <div id="nav"></div>
  </aside>
  <main id="view">Loading…</main>
</div>
<script id="data" type="application/json">__PAGES_JSON__</script>
<script>
const PAGES = JSON.parse(document.getElementById('data').textContent);
const keys = Object.keys(PAGES).sort();

// --- tiny markdown subset renderer ---------------------------------------
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function inline(s){
  // images ![alt](src) — but our pages use raw <img> html; leave those alone.
  s = s.replace(/`([^`]+)`/g, (m,c)=>'<code>'+esc(c)+'</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
  // links [text](href)
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m,t,h)=>'<a href="'+h+'">'+t+'</a>');
  return s;
}
function render(md){
  const raw = md.split('\n');
  let out=[], i=0;
  // strip YAML frontmatter
  if(raw[0]==='---'){ let j=1; while(j<raw.length && raw[j]!=='---') j++; i=j+1; }
  let inCode=false, code=[];
  let listType=null, listBuf=[];
  function flushList(){ if(listType){ out.push('<'+listType+'>'+listBuf.join('')+'</'+listType+'>'); listBuf=[]; listType=null; } }
  let tbl=[];
  function flushTbl(){
    if(!tbl.length) return;
    let rows = tbl.filter(r=>!/^\s*\|?[\s:|-]+\|?\s*$/.test(r));
    let html='<table>';
    rows.forEach((r,ri)=>{
      let cells=r.replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
      let tag = ri===0?'th':'td';
      html+='<tr>'+cells.map(c=>'<'+tag+'>'+inline(esc(c))+'</'+tag+'>').join('')+'</tr>';
    });
    out.push(html+'</table>'); tbl=[];
  }
  for(; i<raw.length; i++){
    let line=raw[i];
    if(line.startsWith('```')){ if(inCode){ out.push('<pre><code>'+esc(code.join('\n'))+'</code></pre>'); code=[]; inCode=false; } else { flushList(); flushTbl(); inCode=true; } continue; }
    if(inCode){ code.push(line); continue; }
    if(/^\s*\|.*\|/.test(line)){ flushList(); tbl.push(line); continue; } else flushTbl();
    if(/^#{1,6}\s/.test(line)){ flushList(); let lvl=line.match(/^#+/)[0].length; let txt=line.replace(/^#+\s/,'');
      let id=txt.toLowerCase().replace(/<[^>]+>/g,'').replace(/[^\w\s-]/g,'').trim().replace(/\s+/g,'-');
      out.push('<h'+lvl+' id="'+id+'">'+inline(txt)+'</h'+lvl+'>'); continue; }
    if(/^>\s?/.test(line)){ flushList(); out.push('<blockquote>'+inline(esc(line.replace(/^>\s?/,'')))+'</blockquote>'); continue; }
    if(/^(-{3,}|\*{3,})$/.test(line.trim())){ flushList(); out.push('<hr>'); continue; }
    let m;
    if(m=line.match(/^(\s*)[-*]\s+(.*)/)){ if(listType!=='ul'){flushList(); listType='ul';} listBuf.push('<li>'+inline(esc(m[2]))+'</li>'); continue; }
    if(m=line.match(/^(\s*)\d+\.\s+(.*)/)){ if(listType!=='ol'){flushList(); listType='ol';} listBuf.push('<li>'+inline(esc(m[2]))+'</li>'); continue; }
    if(line.trim()===''){ flushList(); continue; }
    flushList();
    // allow raw <img ...> (logo) through, escape everything else
    if(/^\s*<img\s/i.test(line)) out.push('<p>'+line+'</p>');
    else out.push('<p>'+inline(esc(line))+'</p>');
  }
  flushList(); flushTbl();
  if(inCode) out.push('<pre><code>'+esc(code.join('\n'))+'</code></pre>');
  return out.join('\n');
}

// --- routing -------------------------------------------------------------
function resolve(from, href){
  // strip anchor
  let [path, anchor] = href.split('#');
  if(!path) return {key:from, anchor}; // same-page anchor
  // resolve relative to `from`'s directory
  let base = from.split('/').slice(0,-1);
  path.split('/').forEach(p=>{ if(p==='..') base.pop(); else if(p!=='.') base.push(p); });
  return {key: base.join('/'), anchor};
}
let current = 'index.md';
function show(key, anchor){
  const md = PAGES[key];
  const view = document.getElementById('view');
  if(md===undefined){ view.innerHTML='<p class="miss">No such page: '+esc(key)+'</p>'; return; }
  current = key;
  view.innerHTML = render(md);
  // rewrite in-vault links to hash routes; leave http(s) + img srcs alone
  view.querySelectorAll('a[href]').forEach(a=>{
    const href=a.getAttribute('href');
    if(/^https?:/.test(href)) { a.target='_blank'; return; }
    a.addEventListener('click', e=>{ e.preventDefault(); const r=resolve(current, href);
      location.hash = encodeURIComponent(r.key)+(r.anchor?('@'+r.anchor):''); });
  });
  document.querySelectorAll('#nav a').forEach(a=>a.classList.toggle('active', a.dataset.key===key));
  if(anchor){ const el=view.querySelector('#'+CSS.escape(anchor)); if(el) el.scrollIntoView(); }
  else { view.scrollTo?.(0,0); window.scrollTo(0,0); }
}
function route(){
  const h=decodeURIComponent(location.hash.replace(/^#/,''));
  if(!h){ show('index.md'); return; }
  const [key,anchor]=h.split('@'); show(key, anchor);
}
window.addEventListener('hashchange', route);

// --- sidebar -------------------------------------------------------------
function title(key){ const m=PAGES[key].match(/^title:\s*(.+)$/m); return (m?m[1]:key).replace(/^["']|["']$/g,''); }
function buildNav(filter){
  const groups={'':[],'topics/':[],'sources/':[]};
  keys.forEach(k=>{ const g=k.startsWith('topics/')?'topics/':k.startsWith('sources/')?'sources/':''; groups[g].push(k); });
  const nav=document.getElementById('nav'); nav.innerHTML='';
  const label={'':'Pages','topics/':'Topics','sources/':'Sources'};
  Object.keys(groups).forEach(g=>{
    let items=groups[g].filter(k=>!filter || title(k).toLowerCase().includes(filter) || k.toLowerCase().includes(filter));
    if(!items.length) return;
    const h=document.createElement('h2'); h.textContent=label[g]+' ('+items.length+')'; nav.appendChild(h);
    items.forEach(k=>{ const a=document.createElement('a'); a.textContent=title(k); a.dataset.key=k;
      a.href='#'+encodeURIComponent(k); a.classList.toggle('active',k===current); nav.appendChild(a); });
  });
}
document.getElementById('q').addEventListener('input', e=>buildNav(e.target.value.toLowerCase().trim()));
buildNav('');
route();
</script>
</body>
</html>
"""
