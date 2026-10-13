#!/usr/bin/env python3
"""Dr Harry Lab - Claude bridge (edit wali, pabandiyon ke saath).

Website ke "Claude se madad" box ka message hub se uthata hai, Claude Code CLI (`claude -p`)
ko chalata hai, aur jawab hub par wapas bhejta hai (feed mein "Claude: ...").

Pabandiyan (jaan boojh kar):
  * Claude sirf Read/Grep/Glob kar sakta hai aur sirf m2.py, worker.py, html_parse.py, run_lab.py, doctor.py, lab/*.py EDIT kar sakta hai.
    Bash, Write, web access band hai.
  * Sirf wohi commands chalti hain jo tumhare hub token se aayi hon (hub khud token check karta hai),
    by="web" ya "auto"; [fixq:job:qid] = ek sawal ka fix proposal (sirf Read, hub par propose, khud apply nahi) (worker ki flag report, ghante mein max 4), 1000 characters tak, ek waqt mein ek, ghante mein max 12.
  * Har tabdeeli edits.log mein diff ke saath likhi jati hai aur alag git commit banta hai (rollback aasan).
  * Bridge site deploy nahi karta. Naya worker.zip hub par `setworker` se jata hai (sirf allowed file names, purana backup, log); Colab Cell 1 usay utha leta hai.

Chalana (Termux / PC / Colab; `claude` CLI install + login ho):
  HUB=https://darkslategrey-pigeon-166872.hostingersite.com TOKEN=xxxx python3 bridge.py
"""
import json, os, re, shutil, subprocess, sys, time, urllib.parse, urllib.request, zipfile

HUB = os.environ.get("HUB", "").rstrip("/")
TOKEN = os.environ.get("TOKEN", "")
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("LAB_WORK", os.path.join(os.path.expanduser("~"), "drh-bridge-work"))
MAX_LEN, MAX_PER_HOUR, TIMEOUT = 1000, 12, 900
MAX_AUTO_PER_HOUR = 30
AUTO_APPLY = os.environ.get('AUTO_APPLY', '1') != '0'
MAX_FIX_PER_HOUR = 80
SHARE = [d for d in (os.environ.get('LAB_SHARE', '').split(':') if os.environ.get('LAB_SHARE') else [os.path.expanduser('~/storage/shared/DrHarry'), os.path.expanduser('~/storage/shared/Download')]) if d and os.path.isdir(d)]
ATTDIR = os.path.join(os.path.expanduser('~'), 'drh-attach')
EDITABLE = ["m2.py", "worker.py", "html_parse.py", "run_lab.py", "doctor.py", "lab/*.py"]
ZIP_FILES = ["worker.py", "run_lab.py", "m2.py", "html_parse.py", "doctor.py", "make_sample.py", "setup_linux.sh"]
if not HUB or not TOKEN:
    sys.exit("HUB aur TOKEN environment variables set karo")

def call(action, data=None, qs=""):
    req = urllib.request.Request(f"{HUB}/api.php?a={action}{qs}",
        headers={"X-Token": TOKEN, "Content-Type": "application/json"},
        data=None if data is None else json.dumps(data).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

def upload_worker(zip_path):
    req = urllib.request.Request(f"{HUB}/api.php?a=setworker", headers={"X-Token": TOKEN, "Content-Type": "application/octet-stream"}, data=open(zip_path, "rb").read())
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())

def git(*a):
    return subprocess.run(["git", "-C", WORK, *a], capture_output=True, text=True)

def setup_workdir():
    os.makedirs(WORK, exist_ok=True)
    if not os.path.isdir(os.path.join(WORK, ".git")):
        zp = os.path.join(HERE, "worker.zip")
        if not os.path.exists(zp):
            sys.exit("worker.zip repo mein nahi mila; bridge.py repo folder se chalao")
        with zipfile.ZipFile(zp) as z:
            z.extractall(WORK)
        git("init", "-q"); git("config", "user.email", "bridge@drh.local"); git("config", "user.name", "DRH Bridge")
        git("add", "-A"); git("commit", "-qm", "base: worker.zip")
    print("Work folder:", WORK)

def context():
    st = call("state")
    jobs = sorted(st.get("jobs", []), key=lambda j: j.get("updated", 0), reverse=True)[:3]
    return json.dumps([{k: j.get(k) for k in ("id", "name", "status", "pct", "stage", "detail")} for j in jobs], ensure_ascii=False)

def post(text):
    call("cmd", {"text": text[:950], "by": "claude"})

def claude_cmd(args):
    """Run `claude` through a shell: on Termux the launcher can be a script without a shebang (direct exec gives Errno 8 'Exec format error'); a shell runs it fine."""
    sh = shutil.which("bash") or shutil.which("sh") or "sh"
    return [sh, "-c", 'exec claude "$@"', "claude"] + args

def check_claude():
    try:
        r = subprocess.run(claude_cmd(["--version"]), capture_output=True, text=True, timeout=60)
        out = (r.stdout or r.stderr).strip()[:120]
        print("claude CLI:", out, "(rc=%d)" % r.returncode)
        return r.returncode == 0
    except Exception as e:
        print("claude CLI chal nahi raha:", e); return False


def act_label(name, inp):
    f = lambda k: os.path.basename(str(inp.get(k, ""))) if inp.get(k) else ""
    if name == "Read": return "Padh raha hun: " + (f("file_path") or "file")
    if name in ("Edit", "MultiEdit"): return "Edit kar raha hun: " + (f("file_path") or "file")
    if name == "Grep": return "Dhoond raha hun: " + str(inp.get("pattern", ""))[:60]
    if name == "Glob": return "Files dekh raha hun: " + str(inp.get("pattern", ""))[:60]
    return "Kaam: " + name

def run_claude(args, cwd, timeout):
    """Run claude -p with stream-json; post one short '▸ ...' activity line per tool use so the website shows what Claude is doing. Returns the final text."""
    import threading
    p = subprocess.Popen(claude_cmd(args + ["--output-format", "stream-json", "--verbose"]), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timer = threading.Timer(timeout, p.kill); timer.start()
    final, texts, raw, n, last = "", [], [], 0, 0.0
    try:
        for line in p.stdout:
            raw.append(line)
            try: ev = json.loads(line)
            except Exception: continue
            if ev.get("type") == "assistant":
                for blk in (ev.get("message") or {}).get("content") or []:
                    if blk.get("type") == "tool_use" and n < 30 and time.time() - last > 1.5:
                        try: post("▸ " + act_label(blk.get("name", ""), blk.get("input") or {})); n += 1; last = time.time()
                        except Exception: pass
                    elif blk.get("type") == "text": texts.append(blk.get("text", ""))
            elif ev.get("type") == "result" and ev.get("result"):
                final = str(ev["result"])
        p.wait(timeout=30)
    finally:
        timer.cancel()
    err = p.stderr.read() if p.stderr else ""
    return (final or "\n".join(texts) or "".join(raw) or err or "(koi jawab nahi)").strip()

def fetch_attachments(cmd):
    """Download [att:a,b] files from the hub into ATTDIR; return list of local paths. Names are re-checked (no path tricks)."""
    m = re.search(r"\[att:([^\]]+)\]", cmd); paths = []
    if not m: return paths
    os.makedirs(ATTDIR, exist_ok=True)
    for fn in m.group(1).split(",")[:6]:
        fn = fn.strip()
        if not re.fullmatch(r"[\w .()\-]{1,120}", fn) or ".." in fn: continue
        try:
            req = urllib.request.Request(f"{HUB}/api.php?a=attfile&f=" + urllib.parse.quote(fn), headers={"X-Token": TOKEN})
            with urllib.request.urlopen(req, timeout=120) as r: data = r.read()
            p = os.path.join(ATTDIR, fn); open(p, "wb").write(data); paths.append(p)
        except Exception as e:
            print("attachment nahi aayi:", fn, e)
    return paths

def ask_claude(cmd, auto=False, evidence=""):
    extra = ("\n\nEVIDENCE from the hub:\n" + evidence) if evidence else ""
    atts = fetch_attachments(cmd)
    if atts:
        extra += "\n\nUser ne ye files bheji hain (Read tool se kholo, PDF/image dono padh sakte ho): " + ", ".join(atts)
    if SHARE:
        extra += "\nPhone ke ye folders bhi read-only padh sakte ho (user kuch kahe to): " + ", ".join(SHARE)
    m = re.search(r"\[job:([A-Za-z0-9_-]+)\]", cmd)
    if m:
        try: extra += "\n\nHub log (end) for this job:\n" + call("readlog", qs="&id=" + m.group(1)).get("log", "")[-3500:]
        except Exception as e: extra += f"\n(log nahi mila: {e})"
    if auto:
        cmd = ("AUTOMATIC report from the worker (no human wrote this). Decide if the flags show a SYSTEMATIC parser problem "
               "(same flag code on many questions, or 0 questions, or a crash). If yes, find the root cause in m2.py / lab/*.py and make the smallest fix. "
               "If the flags are one-off content problems, or you cannot verify the cause from the evidence, DO NOT edit: just say so (the vision fix / human review handles those). "
               "Never loosen verification checks to make flags disappear. "
               "If you fix a crash/0-question failure, the job is retried automatically after your edit and resumes from the saved progress, so fix the ROOT CAUSE, keep the edit minimal, and say exactly what you changed.\n" + cmd)
    prompt = (f"Tum Dr Harry Lab (PDF parsing hub) ke maintainer ho. Is folder mein worker.py, run_lab.py, m2.py, lab/ (verify, images, vision_fix, report, pipeline) hain. "
              f"Hub: {HUB}. Latest jobs: {context()}\n\nUser ka message: {cmd}{extra}\n\n"
              "Sirf zarurat par in files mein minimal edit karo (m2.py parser, lab/verify.py checks, lab/images.py, lab/vision_fix.py). Jawab chhota, Roman English mein do: kya mila, kya badla.")
    allowed = ["Read", "Grep", "Glob"] + [f"Edit({f})" for f in EDITABLE]
    dirs = []
    for d in [ATTDIR] + SHARE:
        if os.path.isdir(d): dirs += ["--add-dir", d]
    return run_claude(["-p", prompt] + dirs + ["--allowedTools", ",".join(allowed),
                        "--disallowedTools", "Bash,Write,WebFetch,WebSearch,NotebookEdit"], WORK, TIMEOUT)


FIX_PROMPT = ("Tum ek strict MCQ verifier ho. Attached files: source PDF ke page image(s), q.json (parser ne ek sawal ka jo output diya + uske flags) aur (agar ho) page ka text (pdftotext/OCR). "
 "Kaam: image padh kar sawal EXACTLY waisa likho jaisa source mein chhapa hai. Image aakhri authority hai; text file sirf madad hai (OCR galat ho sakta hai). "
 "Rules: sirf wohi likho jo saaf padha ja sake; KABHI andaza mat lagao; source ki spelling theek mat karo; option ids a,b,c,... hon; "
 "percentage wo peer-% hai jo option ke saath chhapi ho (nahi hai to null); sahi jawab wohi hai jo source mark kare (green bar / tick / 'correct answer is X'); "
 "NOTE: sabse zyada peer-% wala option aksar sahi nahi hota, usay sahi jawab mat samajhna. "
 "agar source mein saaf nahi to correct_answer_id null aur confidence low. explanation markdown mein (**bold**, > quote, lists) waisi hi jaisi source mein hai, poori. "
 "Jo hissa padha na jaye uska zikr notes mein karo. Jawab mein SIRF ek JSON object do, koi markdown fence ya extra text nahi: "
 '{"stem":"","options":[{"id":"a","text":"","percentage":null}],"correct_answer_id":"a","explanation":"","confidence":"high|medium|low","notes":""}')

def get_file(jid, name):
    try:
        req = urllib.request.Request(f"{HUB}/api.php?a=file&id={jid}&name=" + urllib.parse.quote(name), headers={"X-Token": TOKEN})
        with urllib.request.urlopen(req, timeout=120) as r: return r.read()
    except Exception:
        return None

def _nt(s): return re.sub(r"\W+", " ", str(s or "").lower()).strip()
def _sim(a, b):
    import difflib
    return difflib.SequenceMatcher(None, _nt(a)[:3000], _nt(b)[:3000]).ratio()

def agrees(parsed, prop):
    """True only if Claude's independent re-read of the page equals the parser output (so the flag was a false alarm)."""
    try:
        if prop.get("confidence") != "high" or not prop.get("correct_answer_id"): return False
        po, qo = parsed.get("options") or [], prop.get("options") or []
        if len(po) != len(qo) or any(str(a.get("id")).lower() != str(b.get("id")).lower() for a, b in zip(po, qo)): return False
        if str(parsed.get("correct_answer_id") or "").lower() != str(prop.get("correct_answer_id") or "").lower(): return False
        if _sim(parsed.get("stem") or parsed.get("text"), prop.get("stem")) < 0.97: return False
        if any(_sim(a.get("text"), b.get("text")) < 0.97 for a, b in zip(po, qo)): return False
        if parsed.get("explanation") and prop.get("explanation") and _sim(parsed["explanation"], prop["explanation"]) < 0.9: return False
        return True
    except Exception:
        return False

def do_fix(job, qid, paths, parsed=None, spot=False):
    """Claude (read-only) re-reads the page images and posts a PROPOSAL to the hub. Returns the proposal dict or None."""
    out = run_claude(["-p", FIX_PROMPT + "\nFiles: " + ", ".join(paths), "--add-dir", ATTDIR,
                      "--allowedTools", "Read", "--disallowedTools", "Bash,Write,Edit,WebFetch,WebSearch,NotebookEdit"], WORK, 420)
    m2 = re.search(r"\{.*\}", out, re.S)
    try: prop = json.loads(m2.group(0)) if m2 else None
    except Exception: prop = None
    if not isinstance(prop, dict) or not prop.get("options"):
        post(f"Fix tayyar nahi hua ({qid}): Claude ka jawab JSON nahi tha: {out[:300]}"); return None
    strict = any((f.get("code") if isinstance(f, dict) else f) in STRICT for f in ((parsed or {}).get("flags") or []))   # notes/images/links cannot be confirmed by a stem/options re-read
    same = bool(parsed) and agrees(parsed, prop) and not strict
    if spot and same:
        post(f"Spot-check Q{parsed.get('number')}: PDF se match ✓"); return prop
    applied = False
    if not same and prop.get("confidence") == "high" and prop.get("correct_answer_id") and AUTO_APPLY and not spot and not strict:
        out2 = run_claude(["-p", FIX_PROMPT + "\nFiles: " + ", ".join(paths), "--add-dir", ATTDIR, "--allowedTools", "Read", "--disallowedTools", "Bash,Write,Edit,WebFetch,WebSearch,NotebookEdit"], WORK, 420)
        m3 = re.search(r"\{.*\}", out2, re.S)
        try: prop2 = json.loads(m3.group(0)) if m3 else None
        except Exception: prop2 = None
        if isinstance(prop2, dict) and prop2.get("options") and agrees(prop, prop2) and not spot:   # two independent reads of the page match each other
            call("propose", {"job": job, "qid": qid, "proposal": prop, "apply": True, "note": "2 alag padhai ek jaisi (high confidence)"}); applied = True
            post(f"Q{parsed.get('number') if parsed else qid}: do baar padhne par ek jaisa, fix apply kar diya.")
    if applied: return prop
    call("propose", {"job": job, "qid": qid, "proposal": prop, "agrees": same and not spot})
    post(f"{'Dobara padh kar confirm hua' if same else 'Fix tayyar'} ({qid}): confidence {prop.get('confidence','?')}." + ("" if same else " Viewer mein Accept ya Reject karo."))
    return prop

ASSET_PROMPT = (FIX_PROMPT.replace('Jawab mein SIRF ek JSON object do, koi markdown fence ya extra text nahi: ', '') .split('{"stem"')[0] +
 "EXTRA KAAM (notes, images, links): attached image files (figure/diagram jo parser ne nikali) khud Read karke dekho. "
 "images = sirf wo files jo is sawal ke figure/diagram hain (file ka naam EXACTLY wahi jo neeche list mein hai); bekar/dusre sawal ki/decorative files mat rakho. "
 "notes = page par jo 'Notes' section hai (heading + poora body markdown mein); agar source mein notes nahi hain to notes null aur notes_in_source false. "
 "explanation/notes mein image link sirf ![](images/<file ka naam>) form mein, aur sirf unhi files ke liye jo list mein hain; toota hua link hatao ya sahi file se badlo. "
 "Jawab mein SIRF ek JSON object do, koi markdown fence ya extra text nahi: "
 '{"stem":"","options":[{"id":"a","text":"","percentage":null}],"correct_answer_id":"a","explanation":"","notes":{"heading":"","body_markdown":""},"notes_in_source":true,"images":[{"file":"","reason":""}],"confidence":"high|medium|low"}')

def _read_json(out):
    m = re.search(r"\{.*\}", out, re.S)
    try: d = json.loads(m.group(0)) if m else None
    except Exception: d = None
    return d if isinstance(d, dict) and d.get("options") else None

def assets_agree(a, b):
    """Two independent reads of the page give the same notes, image set, answer and options."""
    try:
        if not agrees(a, b): return False
        if bool(a.get("notes_in_source", True)) != bool(b.get("notes_in_source", True)): return False
        if a.get("notes_in_source", True) and _sim((a.get("notes") or {}).get("body_markdown"), (b.get("notes") or {}).get("body_markdown")) < 0.9: return False
        return sorted(os.path.basename(str(i.get("file"))) for i in a.get("images") or []) == sorted(os.path.basename(str(i.get("file"))) for i in b.get("images") or [])
    except Exception: return False

def do_asset_fix(jid, q, paths, names):
    """Claude looks at the page, the extracted image files and the notes itself; two matching reads -> applied automatically."""
    n = q.get("number"); cand = [nm for nm in sorted(names) if f"_q{n}_" in nm][:8]
    for k in (q.get("images") or []):
        b0 = os.path.basename(str(k.get("file")))
        if b0 in names and b0 not in cand: cand.append(b0)
    full = {os.path.basename(f["name"]): f["name"] for f in call("files", qs="&id=" + jid).get("files", [])}
    imgp = []
    for nm in cand[:8]:
        d = get_file(jid, full.get(nm, nm))
        if d: fp = os.path.join(ATTDIR, f"{jid}_{nm}"); open(fp, "wb").write(d); imgp.append((nm, fp))
    msg = ASSET_PROMPT + "\nFiles: " + ", ".join(paths) + "\nAvailable image files (name -> path): " + "; ".join(f"{a} -> {b}" for a, b in imgp) + ("" if imgp else "(koi image file nahi mili)")
    args = ["-p", msg, "--add-dir", ATTDIR, "--allowedTools", "Read", "--disallowedTools", "Bash,Write,Edit,WebFetch,WebSearch,NotebookEdit"]
    r1 = _read_json(run_claude(args, WORK, 480)); r2 = _read_json(run_claude(args, WORK, 480)) if r1 else None
    if r1 and r2 and r1.get("confidence") == "high" and r2.get("confidence") == "high" and assets_agree(r1, r2):
        r1["images"] = [{"file": full[os.path.basename(str(i.get("file")))], "reason": str(i.get("reason", ""))[:200]} for i in r1.get("images") or [] if os.path.basename(str(i.get("file"))) in full]
        call("propose", {"job": jid, "qid": str(q.get("id")), "proposal": r1, "apply": True, "note": "notes/images/links: 2 alag padhai ek jaisi"})
        post(f"Q{n}: notes/images/links Claude ne dekh kar theek kiye (2 padhai ek jaisi)."); return r1
    if r1: call("propose", {"job": jid, "qid": str(q.get("id")), "proposal": r1, "agrees": False}); post(f"Q{n}: do padhai alag/kam yaqeen, proposal viewer mein hai.")
    return r1

def handle_fixq(text):
    m = re.search(r"\[fixq:([A-Za-z0-9_-]+):([A-Za-z0-9_-]+)\]", text)
    job, qid = m.group(1), m.group(2)
    atts = fetch_attachments(text)
    if not atts:
        post(f"Fix {qid}: attachments nahi mili."); return
    parsed = None
    for p in atts:
        if p.endswith(".json"):
            try: parsed = json.load(open(p, encoding="utf-8"))
            except Exception: pass
    do_fix(job, qid, atts, parsed)

def bad_questions(jid):
    raw = get_file(jid, "student_view.json")
    if not raw: return None, []
    try: qs = json.loads(raw.decode("utf-8")).get("questions", [])
    except Exception: return None, []
    try: rv = call("review", qs="&id=" + jid).get("review") or {}
    except Exception: rv = {}
    seen = {}
    for q in qs: seen[q.get("number")] = seen.get(q.get("number"), 0) + 1
    for q in qs:
        r = rv.get(str(q.get("id"))) if isinstance(rv, dict) else None
        if r and r.get("status"): q["status"] = r["status"]; q["reviewed"] = True
        if not str(q.get("status", "")).startswith("verified"): continue
    template_check(jid, qs)
    return qs, [q for q in qs if not str(q.get("status", "")).startswith(("verified", "approved", "edited", "claude_"))]

STRICT = ("model_notes", "model_images", "model_ref")

def _has(v): return bool(str(v or "").strip())
def _notes_ok(n): return isinstance(n, dict) and _has(n.get("heading")) and _has(n.get("body_markdown"))

def golden(qs, names=None):
    """The most complete, cleanly verified question = the MODEL every other question is compared with."""
    best, bs = None, -1
    for q in qs:
        if not str(q.get("status", "")).startswith("verified") or q.get("flags"): continue
        o = q.get("options") or []; ids = [str(x.get("id", "")).lower() for x in o]; sm = q.get("source_match") or {}
        if names is not None and any(not im.get("file") or os.path.basename(im["file"]) not in names for im in q.get("images") or []): continue
        if len(o) != 5 or str(q.get("correct_answer_id") or "").lower() not in ids or not _has(q.get("stem")) or not _has(q.get("explanation")) or not _notes_ok(q.get("notes")): continue
        sc = len(q.get("images") or []) * 3 + (2 if (sm.get("stem") or 0) >= 95 and (sm.get("options_min") or 0) >= 95 else 0) + (1 if any(isinstance(x.get("percentage"), (int, float)) for x in o) else 0) + min(len(str(q.get("explanation"))), 2000) / 2000
        if sc > bs: best, bs = q, sc
    return best

def template_check(jid, qs):
    """Flag every question (not yet reviewed) that deviates from the model: exactly 5 options, stem, answer in options, explanation, notes, images present, image/reference links resolvable."""
    try: names = {os.path.basename(f["name"]) for f in call("files", qs="&id=" + jid).get("files", [])}
    except Exception: names = None
    g = golden(qs, names)
    n = max(1, len(qs)); pct = lambda f: sum(1 for q in qs if f(q)) / n
    need_exp = bool(g) and pct(lambda q: _has(q.get("explanation"))) >= 0.6
    need_notes = bool(g) and pct(lambda q: _notes_ok(q.get("notes"))) >= 0.6
    seen = {}
    for q in qs: seen[q.get("number")] = seen.get(q.get("number"), 0) + 1
    if g: print("agent: model sawal = Q%s" % g.get("number"))
    for q in qs:
        if q.get("reviewed") or not str(q.get("status", "")).startswith("verified") or str(q.get("status")).startswith("verified ("): continue
        o = q.get("options") or []; ids = [str(x.get("id", "")).lower() for x in o]; bad = []
        if len(o) != 5: bad.append(("model_options", f"options {len(o)} hain, model ke mutabiq 5 hone chahiye"))
        if len(str(q.get("stem") or "").strip()) < 10: bad.append(("model_stem", "stem khali/bahut chhota"))
        if str(q.get("correct_answer_id") or "").lower() not in ids: bad.append(("model_answer", "sahi jawab options mein nahi"))
        if seen.get(q.get("number"), 0) > 1: bad.append(("model_number", "question number duplicate"))
        if need_exp and not _has(q.get("explanation")): bad.append(("model_explanation", "explanation nahi (model sawal mein hai)"))
        if need_notes and not _notes_ok(q.get("notes")): bad.append(("model_notes", "notes (heading+body) nahi (model sawal mein hain)"))
        if names is not None:
            for im in q.get("images") or []:
                if not im.get("file") or os.path.basename(im["file"]) not in names: bad.append(("model_images", "image file result mein nahi: " + str(im.get("file"))))
            txt = str(q.get("explanation") or "") + " " + str((q.get("notes") or {}).get("body_markdown") or "")
            for ref in re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", txt):
                if not ref.startswith("http") and os.path.basename(ref) not in names: bad.append(("model_ref", "markdown image link toota hua: " + ref))
        if bad:
            q["status"] = "flagged"; q["flags"] = list(q.get("flags") or []) + [{"code": c, "detail": d} for c, d in bad]

def evidence_for(jid, bad):
    """Concrete evidence for a systematic problem: two sample flagged questions + their source page text."""
    L = []
    for q in bad[:2]:
        L.append(f"--- sample Q{q.get('number')} flags={q.get('flags')} source_match={q.get('source_match')} pages={q.get('pages')}")
        L.append("stem: " + str(q.get("stem"))[:300]); L.append("explanation(start): " + str(q.get("explanation"))[:300])
        for p in (q.get("pages") or [])[:3]:
            t = get_file(jid, f"raw_text/page_{int(p):03d}.txt")
            L.append(f"[source text page {p}]\n" + (t.decode("utf-8", "replace")[:1800] if t else "(raw_text nahi mila)"))
    return "\n".join(L)

def auto_questions(jid, bad, spot=False):
    """Per-question auto fix: page images + page text go to Claude; confirmed re-reads are marked verified, differences wait for Accept."""
    os.makedirs(ATTDIR, exist_ok=True); done = 0; diffs = 0
    for q in bad[:12]:
        fixes[:] = [t for t in fixes if time.time() - t < 3600]
        if len(fixes) >= MAX_FIX_PER_HOUR: post("Fix ki ghante ki had poori; baqi sawal thori der baad (ya viewer se)."); break
        fixes.append(time.time()); paths = []
        for p in (q.get("pages") or [])[:2]:
            for nm, ext in ((f"_pages/page_{int(p):03d}.png", "png"), (f"raw_text/page_{int(p):03d}.txt", "txt")):
                data = get_file(jid, nm)
                if data:
                    fp = os.path.join(ATTDIR, f"{jid}_q{q.get('number')}_p{p}.{ext}"); open(fp, "wb").write(data); paths.append(fp)
        if not any(x.endswith(".png") for x in paths): continue
        qj = os.path.join(ATTDIR, f"{jid}_q{q.get('number')}.json"); json.dump({k: q.get(k) for k in ("number", "stem", "options", "correct_answer_id", "explanation", "flags", "pages")}, open(qj, "w"), ensure_ascii=False, indent=1); paths.append(qj)
        post(f"▸ Q{q.get('number')} dobara padh raha hun")
        try:
            if not spot and any((f.get("code") if isinstance(f, dict) else f) in STRICT for f in (q.get("flags") or [])):
                pr = do_asset_fix(jid, q, paths, {os.path.basename(f["name"]) for f in call("files", qs="&id=" + jid).get("files", [])})
            else: pr = do_fix(jid, str(q.get("id")), paths, q, spot)
            if pr:
                done += 1
                if spot and not agrees(q, pr): diffs += 1
        except Exception as e:
            post(f"Q{q.get('number')} fix error: {e}")
    return (done, diffs) if spot else done

def record_edits(cmd):
    """Agar files badli hain: log, commit, worker.zip rebuild. Return summary ya ''."""
    import fnmatch
    ok = lambda f: any(fnmatch.fnmatch(f, p) for p in EDITABLE)
    changed = [f for f in git("diff", "--name-only").stdout.split() if f]
    bad = [f for f in changed if not ok(f)]
    if bad:
        git("checkout", "--", *bad)
        changed = [f for f in changed if ok(f)]
    if not changed:
        return ""
    for f in [f for f in changed if f.endswith(".py")]:   # never ship a file that does not even compile
        r = subprocess.run([sys.executable, "-m", "py_compile", os.path.join(WORK, f)], capture_output=True, text=True)
        if r.returncode != 0:
            git("checkout", "--", *changed)
            return "Claude ki edit mein syntax error tha, wapas kar diya (worker.zip nahi badla): " + (r.stderr or "")[-200:]
    diff = git("diff").stdout
    with open(os.path.join(WORK, "edits.log"), "a", encoding="utf-8") as fh:
        fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} | {cmd[:200]}\n{diff}\n")
    git("add", "-A"); git("commit", "-qm", "claude: " + cmd[:60])
    out = os.path.join(HERE, "worker.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ZIP_FILES:
            p = os.path.join(WORK, f)
            if os.path.exists(p):
                z.write(p, f)
        lab = os.path.join(WORK, "lab")
        for fn in sorted(os.listdir(lab)) if os.path.isdir(lab) else []:
            if fn.endswith(".py"):
                z.write(os.path.join(lab, fn), "lab/" + fn)
    try:
        up = upload_worker(out)
        tail = "Naya worker.zip hub par chadh gaya: worker khud update ho jayega."
        if not up.get("ok"): tail = f"worker.zip hub ne mana kiya: {up.get('error')}"
    except Exception as e:
        tail = f"worker.zip hub par upload nahi hua ({e}); local worker.zip bana hai."
    return "Badli hui files: " + ", ".join(changed) + ". " + tail

AGENT_FILE = os.path.join(WORK, "agent_state.json")

def agent_state():
    try: return json.load(open(AGENT_FILE))
    except Exception: return {}

def agent_save(st):
    try: json.dump(st, open(AGENT_FILE, "w"), indent=1)
    except Exception: pass

def agent_set(jid, state, note=""):
    try: call("agent", {"id": jid, "state": state, "note": note[:380]})
    except Exception as e: print("agent state post fail:", e)

def agent_audit(jid, force=False):
    """DR HARRY AGENT, one pass on a finished job. Returns evidence text if a SYSTEMATIC code fix is needed (job will be re-run), else None (job state set: clean | needs_you)."""
    from collections import Counter
    st = agent_state(); js = st.setdefault(jid, {"rounds": 0, "log": []}); post(f"AGENT: job {jid} ka audit shuru (round {js['rounds'] + 1}).")
    agent_set(jid, "working", "DR HARRY Agent output check/fix kar raha hai")
    qs_all, bad = bad_questions(jid)
    if qs_all is None:
        post("AGENT: student_view.json nahi mili."); agent_set(jid, "needs_you", "student_view.json nahi mili"); return None
    summary = f"{len(qs_all)} sawal, {len(bad)} flagged."
    force = force or bool(js.get("worse"))
    if js.pop("awaiting", False) and len(bad) > js.get("prev_bad", 10 ** 9):   # the code fix made things worse: undo it
        try:
            call("rollbackworker"); g = git("log", "-1", "--format=%s").stdout
            if g.startswith("claude:"): git("reset", "--hard", "HEAD~1")
            post(f"AGENT: parser fix se flags {js['prev_bad']} se {len(bad)} ho gaye (bura), fix wapas (rollback) kar diya."); js["worse"] = True; force = True
        except Exception as e: post(f"AGENT: rollback nahi hua ({e})")
        agent_save(st)
    if bad:
        top, n = Counter(f.get("code") if isinstance(f, dict) else f for q in bad for f in q.get("flags", [])).most_common(1)[0]
        if len(bad) >= 6 and n >= 0.5 * len(bad) and js["rounds"] < 3 and not force:
            js["rounds"] += 1; js["prev_bad"] = len(bad); summary += f" Systematic flag '{top}' ({n}) -> parser fix."
            js["log"].append({"ts": int(time.time()), "summary": summary}); agent_save(st)
            return f"{len(bad)}/{len(qs_all)} questions are flagged and {n} of them share the flag '{top}': a SYSTEMATIC problem in the parser/verifier, not individual content errors.\n" + evidence_for(jid, bad) + ("\nEarlier agent attempts on this job (do not repeat a fix that did not help):\n" + "\n".join(x["summary"] for x in js["log"][-4:]) if js["log"] else "")
        for _ in range(3):   # per-question re-reads in chunks until nothing is left or no progress
            before = len(bad)
            auto_questions(jid, bad[:40])
            qs_all, bad = bad_questions(jid)
            if not bad or len(bad) >= before: break
        summary += f" Baad mein {len(bad)} flagged."
    ok = [q for q in qs_all if q not in bad]
    pick = ok[::max(1, len(ok) // 4)][:4] if ok else []
    if pick:
        done, diffs = auto_questions(jid, pick, spot=True)
        summary += f" Spot-check {done}: {diffs} mein farq." if diffs else f" Spot-check {done}: sab PDF se match."
    js["log"].append({"ts": int(time.time()), "summary": summary}); js["log"] = js["log"][-20:]; agent_save(st)
    qs_all, bad = bad_questions(jid)
    if not bad:
        agent_set(jid, "clean", "Sab sawal verified: output tayyar. " + summary); post("AGENT: output CLEAN, tayyar. " + summary)
    else:
        nums = ", ".join("Q" + str(q.get("number")) for q in bad[:15])
        agent_set(jid, "needs_you", f"{len(bad)} sawal agent verify nahi kar saka ({nums}); source mein saaf nahi ya padhai alag aayi. " + summary); post("AGENT: " + str(len(bad)) + " sawal verify nahi hue: " + nums)
    return None

fixes = []

def main():
    setup_workdir()
    if not check_claude():
        print("WARNING: claude CLI theek se nahi chal raha. Termux mein `claude --version` aur `claude` (login) pehle khud chala kar dekho.")
    since = int(time.time() * 1000); runs = []; autos = []; requeues = {}; last_hb = 0
    print("Bridge chal raha hai. Ctrl+C se band.")
    while True:
        try:
            if time.time() - last_hb > 15:
                call("bridgehb", qs="&host=" + os.uname().nodename[:30]); last_hb = time.time()
            for c in call("cmds", qs=f"&since={since}").get("cmds", []):
                since = max(since, c["ts"])
                if c.get("by") not in ("web", "auto"):
                    continue
                auto = c.get("by") == "auto"
                if c.get("by") == "web" and "[fixq:" in str(c.get("text", "")):
                    fixes[:] = [t for t in fixes if time.time() - t < 3600]
                    if len(fixes) >= MAX_FIX_PER_HOUR:
                        post("Fix ki ghante ki had (30) poori, thori der baad."); continue
                    fixes.append(time.time()); print("fix:", str(c.get("text"))[:80])
                    try: handle_fixq(str(c.get("text", ""))[:MAX_LEN])
                    except Exception as e: post(f"Fix error: {e}")
                    continue
                text = str(c.get("text", ""))[:MAX_LEN]
                runs = [t for t in runs if time.time() - t < 3600]
                if auto:
                    autos[:] = [t for t in autos if time.time() - t < 3600]
                    if len(autos) >= MAX_AUTO_PER_HOUR:
                        print("auto limit; skip"); continue
                    autos.append(time.time())
                elif len(runs) >= MAX_PER_HOUR:
                    post("Ghante ki had (12) poori ho gayi, thori der baad likho."); continue
                runs.append(time.time())
                print("cmd:", text); post("Auto report mili, dekh raha hun..." if auto else "Dekh raha hun...")
                evidence = ""
                ag = re.search(r"\[agent:([A-Za-z0-9_-]+)\]", text)
                mj0 = re.search(r"\[job:([A-Za-z0-9_-]+)\]", text)
                if ag:
                    auto = True; text = f"[job:{ag.group(1)}] AGENT audit (manual) " + text[:300]; mj0 = re.search(r"\[job:([A-Za-z0-9_-]+)\]", text)
                if auto and mj0 and (ag or (" flagged" in text and " fail" not in text)):
                    try: evidence = agent_audit(mj0.group(1))
                    except Exception as e:
                        post(f"AGENT error: {e}"); continue
                    if evidence is None: continue
                try:
                    reply = ask_claude(text, auto, evidence); extra = record_edits(text)
                    post(reply + ("\n\n" + extra if extra else ""))
                    mj = re.search(r"\[job:([A-Za-z0-9_-]+)\]", text)
                    if auto and mj and not (extra.startswith("Badli hui files") and "hub par chadh gaya" in extra):
                        if " fail" in text: agent_set(mj.group(1), "needs_you", "Parser fail hua aur agent code fix nahi kar saka: " + reply[:250])
                        else: agent_audit(mj.group(1), force=True)   # no code fix possible -> fix the questions one by one instead
                    if auto and extra.startswith("Badli hui files") and "hub par chadh gaya" in extra and mj:
                        jid = mj.group(1); requeues[jid] = requeues.get(jid, 0) + 1
                        if requeues[jid] <= 3:
                            call("requeue", qs="&id=" + jid); _st = agent_state(); _st.setdefault(jid, {"rounds": 0, "log": []})["awaiting"] = True; agent_save(_st)
                            post("Job dobara line mein daal di: worker naya code utha kar wahin se resume karega (koshish %d/3)." % requeues[jid])
                        else:
                            post("Is job par 3 auto-koshish ho chuki; baqi sawal ek-ek karke theek kar raha hun."); agent_audit(jid, force=True)
                except Exception as e:
                    post(f"Bridge error: {e}")
        except Exception as e:
            print("poll error:", e)
        time.sleep(4)

if __name__ == "__main__":
    main()
