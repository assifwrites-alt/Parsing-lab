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
MAX_AUTO_PER_HOUR = 4
MAX_FIX_PER_HOUR = 30
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
    same = bool(parsed) and agrees(parsed, prop)
    if spot and same:
        post(f"Spot-check Q{parsed.get('number')}: PDF se match ✓"); return prop
    call("propose", {"job": job, "qid": qid, "proposal": prop, "agrees": same and not spot})
    post(f"{'Dobara padh kar confirm hua' if same else 'Fix tayyar'} ({qid}): confidence {prop.get('confidence','?')}." + ("" if same else " Viewer mein Accept ya Reject karo."))
    return prop

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
    return qs, [q for q in qs if not str(q.get("status", "")).startswith(("verified", "approved", "edited", "claude_"))]

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
            pr = do_fix(jid, str(q.get("id")), paths, q, spot)
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

def agent_audit(jid):
    """DR HARRY AGENT, one pass on a finished job. Returns evidence text if a SYSTEMATIC code fix is needed, else None (everything handled here)."""
    from collections import Counter
    st = agent_state(); js = st.setdefault(jid, {"rounds": 0, "log": []}); post(f"AGENT: job {jid} ka audit shuru (round {js['rounds'] + 1}).")
    qs_all, bad = bad_questions(jid)
    if qs_all is None:
        post("AGENT: student_view.json nahi mili, audit nahi ho saka."); return None
    ok = [q for q in qs_all if q not in bad]
    summary = f"{len(qs_all)} sawal, {len(bad)} flagged."
    evidence = None
    if bad:
        top, n = Counter(f.get("code") if isinstance(f, dict) else f for q in bad for f in q.get("flags", [])).most_common(1)[0]
        if len(bad) >= 6 and n >= 0.5 * len(bad) and js["rounds"] < 3:
            js["rounds"] += 1
            evidence = f"{len(bad)}/{len(qs_all)} questions are flagged and {n} of them share the flag '{top}': a SYSTEMATIC problem in the parser/verifier, not individual content errors.\n" + evidence_for(jid, bad)
            summary += f" Systematic flag '{top}' ({n}) -> parser fix try."
        else:
            done = auto_questions(jid, bad); summary += f" {done} flagged sawal PDF se dobara padhe."
    if evidence is None:
        # spot-check: verified questions bhi PDF se milao (output match)
        pick = ok[::max(1, len(ok) // 4)][:4] if ok else []
        if pick:
            done, diffs = auto_questions(jid, pick, spot=True)
            summary += f" Spot-check {done} verified sawal: {diffs} mein farq (viewer mein proposal)." if diffs else f" Spot-check {done} verified sawal: sab PDF se match."
        js["log"].append({"ts": int(time.time()), "summary": summary}); js["log"] = js["log"][-20:]; agent_save(st)
        post("AGENT khatam: " + summary)
    else:
        js["log"].append({"ts": int(time.time()), "summary": summary}); agent_save(st)
    return evidence

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
                    if auto and extra.startswith("Badli hui files") and "hub par chadh gaya" in extra and mj:
                        jid = mj.group(1); requeues[jid] = requeues.get(jid, 0) + 1
                        if requeues[jid] <= 3:
                            call("requeue", qs="&id=" + jid)
                            post("Job dobara line mein daal di: worker naya code utha kar wahin se resume karega (koshish %d/3)." % requeues[jid])
                        else:
                            post("Is job par 3 auto-koshish ho chuki; ab aap dekho (Dobara try ya Claude ko likho).")
                except Exception as e:
                    post(f"Bridge error: {e}")
        except Exception as e:
            print("poll error:", e)
        time.sleep(4)

if __name__ == "__main__":
    main()
