#!/usr/bin/env python3
"""Dr Harry Lab worker: pulls uploaded PDFs from the website hub, parses them with m2.py, sends progress
and the result zip back. Run it on any Linux machine (Hostinger VPS, Google Colab, a PC) - not on the phone.

  HUB_URL=https://yoursite.com/lab  LAB_TOKEN=...  python3 worker.py

Needs: python3, poppler-utils, tesseract-ocr, mupdf-tools, pillow, numpy (see setup_linux.sh)."""
import os, sys, time, json, glob, subprocess, urllib.request, urllib.parse, urllib.error, shutil, zipfile, socket, re

HUB = os.environ.get("HUB_URL", "").rstrip("/")
TOKEN = os.environ.get("LAB_TOKEN", "")
NAME = re.sub(r"[^A-Za-z0-9_-]", "", os.environ.get("WORKER_NAME", socket.gethostname())) or "worker"
HERE = os.path.dirname(os.path.abspath(__file__))
M2 = os.environ.get("M2_PATH", os.path.join(HERE, "m2.py"))
WORK = os.environ.get("LAB_WORK", os.path.join(os.path.expanduser("~"), "lab_work"))
CHUNK = 4 * 1024 * 1024
MAX_SECONDS = int(os.environ.get("LAB_MAX_SECONDS", str(4 * 3600)))

if not HUB or not TOKEN:
    sys.exit("HUB_URL aur LAB_TOKEN set karo (README dekho)")


def call(action, params=None, body=None, raw=False, timeout=120, tries=4):
    q = {"a": action}
    if params: q.update(params)
    url = HUB + "/api.php?" + urllib.parse.urlencode(q)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
            req.add_header("X-Token", TOKEN)
            if isinstance(body, (bytes, bytearray)) and action not in ("progress", "fail", "hb", "cmd"):
                req.add_header("Content-Type", "application/octet-stream")
            elif body is not None:
                req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                return (data, dict(r.headers)) if raw else json.loads(data.decode("utf-8"))
        except Exception as e:  # network blip / server busy -> retry
            last = e
            time.sleep(2 + i * 3)
    raise RuntimeError(f"hub call {action} fail: {last}")


def hb():
    try:
        du = shutil.disk_usage(WORK if os.path.isdir(WORK) else "/")
        info = f"disk free {du.free // (1 << 30)} GB"
        call("hb", body=json.dumps({"name": NAME, "host": socket.gethostname(), "info": info}).encode(), tries=1)
    except Exception:
        pass


def download_pdf(job, dest):
    jid = job["id"]
    size = int(job.get("size") or 0)
    off = 0
    with open(dest, "wb") as f:
        while True:
            data, hdr = call("getpdf", {"id": jid, "off": off, "len": CHUNK}, raw=True, timeout=300)
            total = int(hdr.get("X-Total-Size") or hdr.get("x-total-size") or size or 0)
            f.write(data); off += len(data)
            if len(data) == 0 or (total and off >= total):
                break
    if size and os.path.getsize(dest) != size:
        raise RuntimeError(f"download adhoora: {os.path.getsize(dest)} / {size}")


def read_jobs(jobs_dir):
    out = []
    for p in sorted(glob.glob(os.path.join(jobs_dir, "*.json"))):
        try:
            j = json.load(open(p))
            out.append({"label": j.get("label", ""), "done": j.get("done", 0), "total": j.get("total", 0), "unit": j.get("unit", ""),
                        "detail": (j.get("detail") or "")[:160], "state": j.get("state", ""), "ts": j.get("ts", 0)})
        except Exception:
            pass
    return out


def overall(jobs, prev):
    tot = sum(float(j["total"]) for j in jobs if j["total"])
    done = sum(min(float(j["done"]), float(j["total"])) for j in jobs if j["total"])
    pct = 100.0 * done / tot if tot else 0.0
    return max(prev, min(pct, 99.0))


def upload_result(jid, zpath):
    size = os.path.getsize(zpath)
    total = max(1, (size + CHUNK - 1) // CHUNK)
    with open(zpath, "rb") as f:
        for idx in range(total):
            call("result", {"id": jid, "idx": idx, "total": total}, body=f.read(CHUNK), timeout=300)


def sh(cmd, timeout=90):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace").stdout or ""
    except Exception:
        return ""


def probe(pdf):
    """What kind of PDF is this? Text layer (normal question PDF) or screenshots (Amedex app, needs OCR)."""
    info = sh(["pdfinfo", pdf])
    m = re.search(r"Pages:\s+(\d+)", info)
    txt = sh(["pdftotext", "-f", "1", "-l", "5", "-layout", pdf, "-"])
    imgs = [l for l in sh(["pdfimages", "-list", "-f", "1", "-l", "5", pdf]).splitlines()[2:] if l.strip()]
    return {"pages": int(m.group(1)) if m else 0, "text_chars_p1_5": len(re.sub(r"\s+", "", txt)),
            "images_p1_5": len(imgs), "sample": re.sub(r"\n{3,}", "\n\n", txt)[:900]}


def pick_mode(pr):
    forced = os.environ.get("LAB_MODE", "auto").lower()
    if forced in ("amedex", "text"):
        return forced
    return "text" if pr["text_chars_p1_5"] >= 400 else "amedex"


def hub_log(jid, header, log_path):
    try:
        tail = open(log_path, "rb").read()[-8000:].decode("utf-8", "replace") if os.path.exists(log_path) else ""
        call("log", {"id": jid}, body=(header + "\n---- run.log (end) ----\n" + tail).encode("utf-8"), tries=1)
    except Exception:
        pass


def count_questions(out_dir):
    best = None
    for p in glob.glob(os.path.join(out_dir, "*_answer_key.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
            n = d.get("total_questions", len(d.get("questions", [])))
            best = n if best is None else max(best, n)
        except Exception:
            pass
    return best


def auto_report(jid, pname, text):
    """Tell the hub (and so the Claude bridge) about a problem worth fixing. Never raises."""
    try:
        call("cmd", body=json.dumps({"text": f"[job:{jid}] {text}"[:990], "by": "auto"}).encode(), tries=2)
    except Exception:
        pass


def flag_summary(outd):
    """(total, flagged, text) from student_view.json: top flag codes and a few examples."""
    try:
        qs = json.load(open(os.path.join(outd, "student_view.json"), encoding="utf-8")).get("questions", [])
    except Exception:
        return 0, 0, ""
    bad = [q for q in qs if not str(q.get("status", "")).startswith("verified")]
    codes = {}
    for q in bad:
        for f in q.get("flags", []):
            c = f.get("code", f) if isinstance(f, dict) else str(f)
            codes[c] = codes.get(c, 0) + 1
    top = ", ".join(f"{c}x{n}" for c, n in sorted(codes.items(), key=lambda t: -t[1])[:6])
    ex = "; ".join(f"Q{q.get('number')}:{(q['flags'][0].get('code') if isinstance(q['flags'][0], dict) else q['flags'][0])}" for q in bad[:6] if q.get("flags"))
    return len(qs), len(bad), f"top flags: {top}. e.g. {ex}"


def process(job):
    jid, pname = job["id"], job["name"]
    base = os.path.join(WORK, jid)
    shutil.rmtree(base, ignore_errors=True)
    for d in ("in", "out", "w", "jobs"):
        os.makedirs(os.path.join(base, d), exist_ok=True)
    pdf = os.path.join(base, "in", pname)
    prog = lambda stage, pct, jobs=None, detail="": call("progress", body=json.dumps({"id": jid, "stage": stage, "pct": pct, "jobs": jobs or [], "detail": detail}).encode(), tries=2)
    try:
        prog("File download ho rahi hai", 0)
        download_pdf(job, pdf)
        is_html0 = pname.lower().endswith((".html", ".htm", ".mhtml", ".mht"))
        pr = probe(pdf) if not is_html0 else {"pages": 0, "text_chars_p1_5": 0, "images_p1_5": 0, "sample": ""}
        opts = job.get("opts") or {}
        mode = "html" if is_html0 else (opts["mode"] if opts.get("mode") in ("text", "amedex") else pick_mode(pr))
        header = (f"opts={opts} file={pname} size={os.path.getsize(pdf)} mode={mode} pages={pr['pages']} text_chars(p1-5)={pr['text_chars_p1_5']} "
                  f"images(p1-5)={pr['images_p1_5']}\n---- text sample (page 1-5) ----\n{pr['sample']}")
        hub_log(jid, header, os.path.join(base, "run.log"))
        if mode == "amedex" and not shutil.which("tesseract"):
            raise RuntimeError("blocked: tesseract missing on this worker (OCR jobs need tesseract-ocr). Run setup.sh or install tesseract-ocr, then Dobara try.")
        env = dict(os.environ, M2_AMX_JOBS=os.path.join(base, "jobs"), M2_AMX_WORK=os.path.join(base, "w"), M2_AMX_STATUS=os.path.join(base, "status.json"))
        log = open(os.path.join(base, "run.log"), "wb")
        t0 = time.time(); pct = 0.0; last_sent = 0
        is_html = pname.lower().endswith((".html", ".htm", ".mhtml", ".mht"))
        cmd = [sys.executable, "-I", "-u", os.path.join(HERE, "html_parse.py"), pdf, os.path.join(base, "out")] if is_html else [sys.executable, "-u", os.path.join(HERE, "run_lab.py"), pdf, os.path.join(base, "out"), "--mode", mode] + (["--limit", str(int(opts["limit"]))] if opts.get("limit") else []) + (["--no-vision"] if opts.get("vision") is False else [])
        proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        while proc.poll() is None:
            time.sleep(3)
            jobs = read_jobs(os.path.join(base, "jobs"))
            pct = overall(jobs, pct)
            run = [j for j in jobs if j["state"] == "run"]
            stage = (max(run, key=lambda j: j["ts"])["label"] if run else "parsing chal rahi hai")
            if time.time() - last_sent > 4:
                prog(stage, pct, jobs); last_sent = time.time()
                hub_log(jid, header, os.path.join(base, "run.log"))
            if time.time() - t0 > MAX_SECONDS:
                proc.kill(); raise RuntimeError("time limit (4 ghante) paar ho gayi")
        log.close()
        if proc.returncode != 0:
            tail = open(os.path.join(base, "run.log"), "rb").read()[-1100:].decode("utf-8", "replace")
            hub_log(jid, header, os.path.join(base, "run.log"))
            raise RuntimeError(f"m2.py rc={proc.returncode} (mode={mode}): {tail}")
        nq = count_questions(os.path.join(base, "out"))
        if nq == 0:
            kinds = {}
            for line in open(os.path.join(base, "run.log"), encoding="utf-8", errors="replace"):
                m = re.match(r"page \d+/\d+: (\w+)", line)
                if m: kinds[m.group(1)] = kinds.get(m.group(1), 0) + 1
            hub_log(jid, header, os.path.join(base, "run.log"))
            raise RuntimeError(f"0 sawal bane (mode={mode}, pages={pr['pages']}, text_chars={pr['text_chars_p1_5']}, page kinds={kinds}). Is PDF ka format {mode} parser se match nahi hua; Claude se fix karwao.")
        try:
            shutil.copy(os.path.join(base, "run.log"), os.path.join(base, "out", "run_log.txt"))
        except Exception:
            pass
        prog("result zip ban rahi hai", 99.5, read_jobs(os.path.join(base, "jobs")))
        zpath = os.path.join(base, "result.zip")
        outd = os.path.join(base, "out")
        keep_pages = set()   # all page images if the total is small enough, else only for questions that still need a human
        all_pages = glob.glob(os.path.join(outd, "_pages", "*.png"))
        if sum(os.path.getsize(x) for x in all_pages) <= 150 * 1024 * 1024:
            keep_pages = {os.path.basename(x) for x in all_pages}
        else:
          try:
            for q in json.load(open(os.path.join(outd, "student_view.json"), encoding="utf-8")).get("questions", []):
                if not str(q.get("status", "")).startswith("verified"):
                    keep_pages.update(f"page_{int(p):03d}.png" for p in q.get("pages", []))
          except Exception:
            pass
        SKIP = ("_work", "_pages_hi")   # raw OCR (_ocr, ocr_debug), raw_text, images, caches and decisions all stay in the zip
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(outd):
                dirs[:] = [d for d in dirs if d not in SKIP]
                for fn in files:
                    full = os.path.join(root, fn); rel = os.path.relpath(full, outd)
                    if rel.startswith("_pages" + os.sep) and fn not in keep_pages: continue
                    z.write(full, rel)
        prog("result upload ho raha hai", 99.8)
        upload_result(jid, zpath)
        tot, nbad, ftxt = flag_summary(outd)
        if nbad:
            auto_report(jid, pname, f"AUTO {pname}: {nbad}/{tot} sawal flagged. {ftxt}")
        print(f"[{time.strftime('%H:%M:%S')}] done: {pname}", flush=True)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] FAIL {pname}: {e}", flush=True)
        if "0 sawal" in str(e) or "rc=" in str(e):
            auto_report(jid, pname, f"AUTO {pname} fail: {str(e)[:700]}")
        try:
            call("fail", body=json.dumps({"id": jid, "error": str(e)[:1450]}).encode(), tries=2)
        except Exception:
            pass
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run_doctor():
    """Run doctor.py at startup and report the result to the hub (shown on the System health page)."""
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "doctor.py"), "--json"], capture_output=True, text=True, timeout=300)
        rep = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {"ok": False, "checks": [{"name": "doctor", "status": "FAIL", "detail": (r.stderr or r.stdout)[-200:]}]}
    except Exception as e:
        rep = {"ok": False, "checks": [{"name": "doctor", "status": "FAIL", "detail": str(e)[:200]}]}
    bad = [c["name"] for c in rep["checks"] if c["status"] == "FAIL"]
    print("doctor:", "READY" if rep["ok"] else f"NOT READY, failing: {bad} (jobs needing these are blocked, not faked)", flush=True)
    try:
        call("doctor", {"worker": NAME}, body=json.dumps(rep).encode(), tries=1)
    except Exception:
        pass


def main():
    os.makedirs(WORK, exist_ok=True)
    run_doctor()
    print(f"worker '{NAME}' hub={HUB} work={WORK}", flush=True)
    last_hb = 0
    while True:
        if time.time() - last_hb > 20:
            hb(); last_hb = time.time()
        try:
            r = call("next", {"worker": NAME}, tries=2)
        except Exception as e:
            print("hub nahi mila:", e, flush=True); time.sleep(15); continue
        job = r.get("job")
        if job:
            print(f"[{time.strftime('%H:%M:%S')}] job: {job['name']}", flush=True)
            process(job)
        else:
            time.sleep(5)


if __name__ == "__main__":
    main()
