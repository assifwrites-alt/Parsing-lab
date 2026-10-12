#!/usr/bin/env python3
"""Dr Harry Lab - Claude bridge (edit wali, pabandiyon ke saath).

Website ke "Claude se madad" box ka message hub se uthata hai, Claude Code CLI (`claude -p`)
ko chalata hai, aur jawab hub par wapas bhejta hai (feed mein "Claude: ...").

Pabandiyan (jaan boojh kar):
  * Claude sirf Read/Grep/Glob kar sakta hai aur sirf m2.py, worker.py, html_parse.py, run_lab.py, doctor.py, lab/*.py EDIT kar sakta hai.
    Bash, Write, web access band hai.
  * Sirf wohi commands chalti hain jo tumhare hub token se aayi hon (hub khud token check karta hai),
    by="web" ya "auto" (worker ki flag report, ghante mein max 4), 1000 characters tak, ek waqt mein ek, ghante mein max 12.
  * Har tabdeeli edits.log mein diff ke saath likhi jati hai aur alag git commit banta hai (rollback aasan).
  * Bridge site deploy nahi karta. Naya worker.zip hub par `setworker` se jata hai (sirf allowed file names, purana backup, log); Colab Cell 1 usay utha leta hai.

Chalana (Termux / PC / Colab; `claude` CLI install + login ho):
  HUB=https://darkslategrey-pigeon-166872.hostingersite.com TOKEN=xxxx python3 bridge.py
"""
import json, os, re, shutil, subprocess, sys, time, urllib.request, zipfile

HUB = os.environ.get("HUB", "").rstrip("/")
TOKEN = os.environ.get("TOKEN", "")
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("LAB_WORK", os.path.join(os.path.expanduser("~"), "drh-bridge-work"))
MAX_LEN, MAX_PER_HOUR, TIMEOUT = 1000, 12, 900
MAX_AUTO_PER_HOUR = 4
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

def ask_claude(cmd, auto=False):
    extra = ""
    m = re.search(r"\[job:([A-Za-z0-9_-]+)\]", cmd)
    if m:
        try: extra = "\n\nHub log (end) for this job:\n" + call("readlog", qs="&id=" + m.group(1)).get("log", "")[-3500:]
        except Exception as e: extra = f"\n(log nahi mila: {e})"
    if auto:
        cmd = ("AUTOMATIC report from the worker (no human wrote this). Decide if the flags show a SYSTEMATIC parser problem "
               "(same flag code on many questions, or 0 questions, or a crash). If yes, find the root cause in m2.py / lab/*.py and make the smallest fix. "
               "If the flags are one-off content problems, or you cannot verify the cause from the evidence, DO NOT edit: just say so (the vision fix / human review handles those). "
               "Never loosen verification checks to make flags disappear.\n" + cmd)
    prompt = (f"Tum Dr Harry Lab (PDF parsing hub) ke maintainer ho. Is folder mein worker.py, run_lab.py, m2.py, lab/ (verify, images, vision_fix, report, pipeline) hain. "
              f"Hub: {HUB}. Latest jobs: {context()}\n\nUser ka message: {cmd}{extra}\n\n"
              "Sirf zarurat par in files mein minimal edit karo (m2.py parser, lab/verify.py checks, lab/images.py, lab/vision_fix.py). Jawab chhota, Roman English mein do: kya mila, kya badla.")
    allowed = ["Read", "Grep", "Glob"] + [f"Edit({f})" for f in EDITABLE]
    r = subprocess.run(claude_cmd(["-p", prompt, "--allowedTools", ",".join(allowed),
                        "--disallowedTools", "Bash,Write,WebFetch,WebSearch,NotebookEdit"]),
                       cwd=WORK, capture_output=True, text=True, timeout=TIMEOUT)
    return (r.stdout or r.stderr or "(koi jawab nahi)").strip()

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
        tail = "Naya worker.zip hub par chadh gaya: Colab Cell 1 dobara chalao, phir Dobara try dabao."
        if not up.get("ok"): tail = f"worker.zip hub ne mana kiya: {up.get('error')}"
    except Exception as e:
        tail = f"worker.zip hub par upload nahi hua ({e}); local worker.zip bana hai."
    return "Badli hui files: " + ", ".join(changed) + ". " + tail

def main():
    setup_workdir()
    if not check_claude():
        print("WARNING: claude CLI theek se nahi chal raha. Termux mein `claude --version` aur `claude` (login) pehle khud chala kar dekho.")
    since = int(time.time() * 1000); runs = []; autos = []; last_hb = 0
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
                try:
                    reply = ask_claude(text, auto); extra = record_edits(text)
                    post(reply + ("\n\n" + extra if extra else ""))
                except Exception as e:
                    post(f"Bridge error: {e}")
        except Exception as e:
            print("poll error:", e)
        time.sleep(4)

if __name__ == "__main__":
    main()
