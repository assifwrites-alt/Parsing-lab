#!/usr/bin/env python3
"""Dr Harry Lab - Claude bridge (edit wali, pabandiyon ke saath).

Website ke "Claude se madad" box ka message hub se uthata hai, Claude Code CLI (`claude -p`)
ko chalata hai, aur jawab hub par wapas bhejta hai (feed mein "Claude: ...").

Pabandiyan (jaan boojh kar):
  * Claude sirf Read/Grep/Glob kar sakta hai aur sirf m2.py, worker.py, html_parse.py EDIT kar sakta hai.
    Bash, Write, web access band hai.
  * Sirf wohi commands chalti hain jo tumhare hub token se aayi hon (hub khud token check karta hai),
    by="web", 900 characters tak, ek waqt mein ek, ghante mein max 12.
  * Har tabdeeli edits.log mein diff ke saath likhi jati hai aur alag git commit banta hai (rollback aasan).
  * Bridge deploy nahi karta. worker.zip naya banta hai; deploy tum isi chat mein "deploy karo" keh kar karwate ho.

Chalana (Termux / PC / Colab; `claude` CLI install + login ho):
  HUB=https://darkslategrey-pigeon-166872.hostingersite.com TOKEN=xxxx python3 bridge.py
"""
import json, os, shutil, subprocess, sys, time, urllib.request, zipfile

HUB = os.environ.get("HUB", "").rstrip("/")
TOKEN = os.environ.get("TOKEN", "")
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("LAB_WORK", os.path.join(os.path.expanduser("~"), "drh-bridge-work"))
MAX_LEN, MAX_PER_HOUR, TIMEOUT = 900, 12, 900
EDITABLE = ["m2.py", "worker.py", "html_parse.py"]
if not HUB or not TOKEN:
    sys.exit("HUB aur TOKEN environment variables set karo")

def call(action, data=None, qs=""):
    req = urllib.request.Request(f"{HUB}/api.php?a={action}{qs}",
        headers={"X-Token": TOKEN, "Content-Type": "application/json"},
        data=None if data is None else json.dumps(data).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
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

def ask_claude(cmd):
    prompt = (f"Tum Dr Harry Lab (PDF parsing hub) ke maintainer ho. Is folder mein worker.py, m2.py, html_parse.py hain. "
              f"Hub: {HUB}. Latest jobs: {context()}\n\nUser ka message: {cmd}\n\n"
              "Sirf zarurat par in teen files mein minimal edit karo. Jawab chhota, Roman English mein do: kya mila, kya badla.")
    allowed = ["Read", "Grep", "Glob"] + [f"Edit({f})" for f in EDITABLE]
    r = subprocess.run(["claude", "-p", prompt, "--allowedTools", ",".join(allowed),
                        "--disallowedTools", "Bash,Write,WebFetch,WebSearch,NotebookEdit"],
                       cwd=WORK, capture_output=True, text=True, timeout=TIMEOUT)
    return (r.stdout or r.stderr or "(koi jawab nahi)").strip()

def record_edits(cmd):
    """Agar files badli hain: log, commit, worker.zip rebuild. Return summary ya ''."""
    changed = [f for f in git("diff", "--name-only").stdout.split() if f]
    bad = [f for f in changed if f not in EDITABLE]
    if bad:
        git("checkout", "--", *bad)
        changed = [f for f in changed if f in EDITABLE]
    if not changed:
        return ""
    diff = git("diff").stdout
    with open(os.path.join(WORK, "edits.log"), "a", encoding="utf-8") as fh:
        fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} | {cmd[:200]}\n{diff}\n")
    git("add", "-A"); git("commit", "-qm", "claude: " + cmd[:60])
    out = os.path.join(HERE, "worker.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ["worker.py", "m2.py", "html_parse.py", "setup_linux.sh"]:
            p = os.path.join(WORK, f)
            if os.path.exists(p):
                z.write(p, f)
    return "Badli hui files: " + ", ".join(changed) + ". Naya worker.zip bana; deploy ke liye Claude chat mein 'deploy karo' likho."

def main():
    setup_workdir()
    since = int(time.time() * 1000); runs = []
    print("Bridge chal raha hai. Ctrl+C se band.")
    while True:
        try:
            for c in call("cmds", qs=f"&since={since}").get("cmds", []):
                since = max(since, c["ts"])
                if c.get("by") != "web":
                    continue
                text = str(c.get("text", ""))[:MAX_LEN]
                runs = [t for t in runs if time.time() - t < 3600]
                if len(runs) >= MAX_PER_HOUR:
                    post("Ghante ki had (12) poori ho gayi, thori der baad likho."); continue
                runs.append(time.time())
                print("cmd:", text); post("Dekh raha hun...")
                try:
                    reply = ask_claude(text); extra = record_edits(text)
                    post(reply + ("\n\n" + extra if extra else ""))
                except Exception as e:
                    post(f"Bridge error: {e}")
        except Exception as e:
            print("poll error:", e)
        time.sleep(4)

if __name__ == "__main__":
    main()
