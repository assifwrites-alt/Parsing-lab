"""Stage 5: outputs - answer_key.json, student_view.json, _flags.json, _flags_report.md, viewer.html (self-contained, ASCII-safe), run_summary.md."""
import base64, html, json, os, re, time
from .verify import _strip_opt

def _imgs_for(q, decisions):
    n = q.get("number"); out = []
    for rel, d in decisions.items():
        if d.get("question") == n and d["decision"] == "useful": out.append({"file": d.get("clean") or rel, "reason": d["reason"]})
        elif d.get("question") == n and d["decision"] == "uncertain": out.append({"file": rel, "reason": d["reason"], "uncertain": True})
    return out

def build_views(source, qs, results, decisions, meta):
    key, view = [], []
    for i, q in enumerate(qs):
        r = results.get(q.get("id") or str(i), {"status": "flagged", "flags": []})
        imgs = _imgs_for(q, decisions)
        flags = list(r.get("flags", []))
        if any(x.get("uncertain") for x in imgs): flags.append({"code": "image_uncertain", "detail": "image could not be classified; review"})
        st = r["status"]
        if st == "verified" and flags: st = "flagged"
        opts = [{"id": o.get("id"), "text": _strip_opt(o.get("text")), "percentage": o.get("percentage")} for o in q.get("options", [])]
        base = {"id": q.get("id"), "number": q.get("number"), "stem": q.get("text"), "options": opts, "correct_answer_id": q.get("correct_answer_id"),
                "explanation": q.get("explanation"), "notes": q.get("notes"), "images": imgs, "status": st, "flags": flags,
                "pages": r.get("info", {}).get("pages", []), "answer_signals": r.get("info", {}).get("answer_signals", {}), "source_match": r.get("info", {}).get("match")}
        if r.get("note"): base["note"] = r["note"]
        if r.get("page_images"): base["page_images"] = r["page_images"]
        key.append(base); view.append(base)
    head = {"source_pdf": source, **meta}
    return {**head, "questions": key}, {**head, "questions": view}

def flags_report(source, view, results):
    bad = [q for q in view["questions"] if q["status"] not in ("verified", "verified (vision-fixed)")]
    L = [f"# Flags report: {source}", "", f"{len(bad)} of {len(view['questions'])} questions need attention.", ""]
    for q in bad:
        L += [f"## Q{q['number']} (pages {q['pages']}) - {q['status']}", ""]
        for f in q["flags"]: L.append(f"- **{f['code']}**: {f.get('detail', '')}")
        if q.get("note"): L.append(f"- note: {q['note']}")
        m = q.get("source_match")
        if m: L.append(f"- source match: stem {m['stem']}%, options min {m['options_min']}%, explanation {m['explanation']}%")
        L += ["", "Parsed stem: " + (q["stem"] or "")[:300], ""]
        for o in q["options"]: L.append(f"  - {o['id']}) {o['text'][:120]}" + (" **[parsed correct]**" if o["id"] == q["correct_answer_id"] else ""))
        if q.get("page_images"): L.append(f"\nPage image: `{', '.join(q['page_images'])}`")
        L.append("")
    return "\n".join(L)

def viewer(source, view, out_dir):
    def emb(rel):
        p = os.path.join(out_dir, rel)
        if not os.path.exists(p): return ""
        ext = os.path.splitext(p)[1].lower().lstrip("."); ext = "jpeg" if ext == "jpg" else ext
        return f"data:image/{ext};base64," + base64.b64encode(open(p, "rb").read()).decode()
    data = json.loads(json.dumps(view))
    for q in data["questions"]:
        for im in q["images"]: im["data"] = emb(im["file"])
        for k in ("page_images",): q.pop(k, None)
    blob = json.dumps(data, ensure_ascii=True).replace("</", "<\\/")
    return VIEWER.replace("__TITLE__", html.escape(source)).replace("__DATA__", blob)

VIEWER = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>:root{--bg:#f4f6fb;--c:#fff;--ink:#0f172a;--mut:#64748b;--ln:#e2e8f0;--ok:#059669;--okb:#d1fae5;--w:#b45309;--wb:#fef3c7;--b:#dc2626;--bb:#fee2e2}
@media(prefers-color-scheme:dark){:root{--bg:#0a0f1e;--c:#111936;--ink:#e6ebf8;--mut:#8d9bbd;--ln:#222c52;--ok:#34d399;--okb:#0f3a2c;--w:#fbbf24;--wb:#3b2d0c;--b:#f87171;--bb:#431a1a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,sans-serif}.w{max-width:820px;margin:0 auto;padding:12px}
.bar{position:sticky;top:0;background:var(--c);border-bottom:1px solid var(--ln);padding:10px 12px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;z-index:5}
.card{background:var(--c);border:1px solid var(--ln);border-radius:14px;padding:14px;margin:12px 0}.opt{border:1px solid var(--ln);border-radius:10px;padding:8px 12px;margin:6px 0;display:flex;gap:10px}
.opt.ok{border-color:var(--ok);background:var(--okb)}.pct{margin-left:auto;color:var(--mut)}.b{display:inline-block;border-radius:99px;padding:2px 10px;font-size:12px;font-weight:700}
.verified{background:var(--okb);color:var(--ok)}.flagged{background:var(--wb);color:var(--w)}.needs_human{background:var(--bb);color:var(--b)}
.fl{background:var(--wb);color:var(--w);border-radius:8px;padding:6px 10px;margin-top:8px;font-size:13px}img{max-width:100%;border-radius:8px;margin:6px 0}
.ex{white-space:pre-wrap;color:var(--mut);margin-top:8px}button,select,label{font:inherit}button{border:1px solid var(--ln);background:var(--c);color:var(--ink);border-radius:8px;padding:6px 12px;cursor:pointer}</style></head>
<body><div class="bar"><b>__TITLE__</b><span id="sum"></span><label><input type="checkbox" id="hide"> hide flagged</label><button id="rv">show answers</button></div><div class="w" id="list"></div>
<script>var D=__DATA__,show=false;function E(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})}
function cls(s){return s=="needs_human"?"needs_human":(s.indexOf("verified")==0?"verified":"flagged")}
function draw(){var h=document.getElementById("hide").checked,n=0,o="";D.questions.forEach(function(q){var c=cls(q.status);if(h&&c!="verified")return;n++;
o+='<div class="card"><div><b>Q'+E(q.number)+'</b> <span class="b '+c+'">'+E(q.status)+'</span></div><p>'+E(q.stem)+'</p>';
q.images.forEach(function(i){if(i.data)o+='<img src="'+i.data+'" alt="question image">'});
q.options.forEach(function(p){var ok=show&&p.id==q.correct_answer_id;o+='<div class="opt'+(ok?' ok':'')+'"><b>'+E(String(p.id).toUpperCase())+'</b><span>'+E(p.text)+'</span>'+(show&&p.percentage!=null?'<span class="pct">'+p.percentage+'%</span>':'')+'</div>'});
if(show&&q.explanation)o+='<div class="ex">'+E(q.explanation)+'</div>';
q.flags.forEach(function(f){o+='<div class="fl">'+E(f.code)+': '+E(f.detail||"")+'</div>'});o+='</div>'});
document.getElementById("list").innerHTML=o;var v=D.questions.filter(function(q){return cls(q.status)=="verified"}).length;document.getElementById("sum").textContent=v+"/"+D.questions.length+" verified, showing "+n}
document.getElementById("hide").onchange=draw;document.getElementById("rv").onclick=function(){show=!show;this.textContent=show?"hide answers":"show answers";draw()};draw();</script></body></html>"""

def run_summary(source, view, meta, budget, img_dec, secs, notes):
    st = {}
    for q in view["questions"]: st[q["status"]] = st.get(q["status"], 0) + 1
    ic = {}
    for d in img_dec.values(): ic[d["decision"]] = ic.get(d["decision"], 0) + 1
    L = [f"# Run summary: {source}", "", f"- parser: {meta.get('parser_version')} | mode: {meta.get('mode')} | platform: {meta.get('platform')} | source type: {meta.get('source_type')}",
         f"- total questions: {len(view['questions'])}", ] + [f"- {k}: {v}" for k, v in sorted(st.items())]
    L += [f"- images: {ic or 'none found'}", f"- vision: {budget.calls} calls, {budget.cache_hits} cache hits, ~${budget.usd:.3f} (cap {budget.max_calls} calls / ${budget.max_usd})",
          f"- time: {secs:.0f}s"]
    if budget.error: L.append(f"- vision note: {budget.error}")
    if notes: L += ["", "## Warnings"] + [f"- {n}" for n in notes]
    return "\n".join(L) + "\n"
