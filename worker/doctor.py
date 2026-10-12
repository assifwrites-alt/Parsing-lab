#!/usr/bin/env python3
"""Dr Harry Lab doctor: PASS/FAIL checks + a tiny end-to-end smoke test. Exit code != 0 if a REQUIRED check fails.
Runs at startup, in setup.sh and before every deploy. Never prints secrets.  Use --json for machine-readable output."""
import importlib, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
R = []  # (name, status PASS/FAIL/WARN, detail, required)

def add(name, ok, detail="", required=True):
    R.append((name, "PASS" if ok else ("FAIL" if required else "WARN"), detail, required))

def cmd_ok(binary, required=True, note=""):
    p = shutil.which(binary); add(f"binary {binary}", bool(p), p or ("missing" + (f" ({note})" if note else "")), required)
    return bool(p)

def main():
    add("python >= 3.9", sys.version_info >= (3, 9), sys.version.split()[0])
    mods = [("pypdf", True), ("pdfplumber", True), ("pytesseract", True), ("cv2", True), ("PIL", True), ("numpy", True), ("imagehash", True), ("skimage", True),
            ("rapidfuzz", True), ("anthropic", True), ("requests", True), ("bs4", True), ("lxml", True), ("dotenv", True), ("pytest", False), ("pymupdf", False)]
    for m, req in mods:
        try:
            importlib.import_module(m); add(f"import {m}", True, "", req)
        except Exception as e:
            add(f"import {m}", False, ("optional, fallback pypdf+poppler" if m == "pymupdf" else str(e)[:80]), req)
    for b in ("pdftoppm", "pdftohtml", "pdftotext", "pdfinfo"): cmd_ok(b)
    t = cmd_ok("tesseract", True, "OCR jobs would be 'blocked: tesseract missing'")
    if t:
        langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout
        add("tesseract eng data", "eng" in langs, "eng" if "eng" in langs else "install tesseract-ocr-eng")
    cmd_ok("gs", False); cmd_ok("convert", False); cmd_ok("git", False); cmd_ok("curl", False)
    add("ANTHROPIC_API_KEY present", bool(os.environ.get("ANTHROPIC_API_KEY")), "set" if os.environ.get("ANTHROPIC_API_KEY") else "not set: vision fix disabled, flagged questions stay needs_human", False)
    work = os.environ.get("LAB_WORK", os.path.join(os.path.expanduser("~"), "lab_work"))
    try:
        os.makedirs(work, exist_ok=True); p = os.path.join(work, ".w"); open(p, "w").write("x"); os.remove(p); add("write access (uploads/outputs)", True, work)
    except Exception as e:
        add("write access (uploads/outputs)", False, str(e)[:80])
    free = shutil.disk_usage(work if os.path.isdir(work) else "/").free / 2 ** 30
    add("disk space >= 2 GB", free >= 2, f"{free:.1f} GB free")
    # smoke test: build sample -> render 1 page -> OCR -> parse -> verify
    try:
        sys.path.insert(0, os.path.join(HERE, "..", "tests"))
        from lab.source import Source, render_pages, _ocr
        from lab.verify import verify_all
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "smoke.pdf")
            try:
                import make_sample; make_sample.build(pdf)
            except Exception:
                from reportlab.pdfgen import canvas
                c = canvas.Canvas(pdf)
                for i in range(1, 6):
                    c.drawString(50, 780, f"Question {i} of 5"); c.drawString(50, 760, "Which drug is first line?"); c.drawString(60, 740, "A. Aspirin   10%"); c.drawString(60, 725, "B. Insulin   90%")
                    c.drawString(50, 700, "Explanation"); c.drawString(50, 685, "The correct answer is B."); c.showPage()
                c.save()
            png = render_pages(pdf, d, first=1, last=1); add("smoke: render page", len(png) == 1, os.path.basename(png[0]) if png else "no png")
            if t and png:
                txt = _ocr(png[0], os.path.join(d, "o.txt")); add("smoke: OCR", "question" in txt.lower(), f"{len(txt)} chars")
            out = os.path.join(d, "out"); rc = subprocess.run([sys.executable, "-I", os.path.join(HERE, "m2.py"), pdf, out], capture_output=True, text=True, timeout=300).returncode
            keys = [f for f in os.listdir(out) if f.endswith("_answer_key.json")] if os.path.isdir(out) else []
            qs = json.load(open(os.path.join(out, keys[0])))["questions"] if keys else []
            add("smoke: parse (m2.py)", rc == 0 and len(qs) >= 2, f"rc={rc}, {len(qs)} questions")
            if qs:
                res = verify_all(qs, Source(pdf, out)); ok = sum(1 for v in res.values() if v["status"] == "verified")
                add("smoke: verify", ok == len(qs), f"{ok}/{len(qs)} verified")
    except Exception as e:
        add("smoke test", False, f"{type(e).__name__}: {str(e)[:100]}")
    failed = [r for r in R if r[1] == "FAIL"]
    if "--json" in sys.argv:
        print(json.dumps({"ok": not failed, "checks": [{"name": n, "status": s, "detail": d, "required": q} for n, s, d, q in R]}))
    else:
        for n, s, d, q in R: print(f"[{s}] {n}" + (f" - {d}" if d else ""))
        print(f"\n{'READY' if not failed else 'NOT READY'}: {sum(1 for r in R if r[1]=='PASS')} pass, {len(failed)} fail, {sum(1 for r in R if r[1]=='WARN')} warn")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
