"""Dr Harry Lab pipeline: analyse source -> parse (m2.py) -> verify every question -> classify images -> vision-fix flagged -> reports.
  python -m lab.pipeline <pdf> <out_dir> [--limit N] [--mode auto|text|amedex] [--no-vision] [--skip-parse]
Re-running is cheap: OCR text, image decisions and vision answers are cached in out_dir."""
import glob, json, os, subprocess, sys, time
from . import images as imgmod, pageimages, report, vision, vision_fix
from .source import Source, render_pages
from .verify import verify_all

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def progress(label, done, total, unit="", state="run", detail=""):
    d = os.environ.get("M2_AMX_JOBS")
    if not d: return
    os.makedirs(d, exist_ok=True)
    json.dump({"label": label, "done": done, "total": total, "unit": unit, "state": state, "detail": detail, "ts": time.time()}, open(os.path.join(d, "lab_" + label.split()[0].lower() + ".json"), "w"))

def find_key(out_dir):
    c = sorted(glob.glob(os.path.join(out_dir, "*_answer_key.json")))
    return c[0] if c else None

def run(pdf, out_dir, limit=None, mode="auto", use_vision=True, skip_parse=False):
    t0 = time.time(); os.makedirs(out_dir, exist_ok=True); notes = []
    stem = os.path.splitext(os.path.basename(pdf))[0]
    src = Source(pdf, out_dir)
    kind = src.kind(); platform = src.platform()
    if mode == "auto": mode = "text" if (src.layer_chars or 0) >= 400 else "amedex"
    print(f"[lab] source: {src.n} pages, type={kind}, platform={platform}, mode={mode}", flush=True)
    if os.environ.get("LAB_RENDER_ALL") == "1" or src.n <= 150:
        progress("Pages render", 0, src.n, "pages"); render_pages(pdf, out_dir); progress("Pages render", src.n, src.n, "pages", "ok")
    if not skip_parse:
        cmd = [sys.executable, "-I", "-u", os.path.join(HERE, "m2.py"), pdf] + (["--amedex"] if mode == "amedex" else []) + ([f"--limit={limit}"] if limit else []) + [out_dir]
        rc = subprocess.call(cmd)
        if rc != 0: raise RuntimeError(f"m2.py failed (rc={rc}, mode={mode})")
    kf = find_key(out_dir)
    if not kf: raise RuntimeError("m2.py produced no answer_key.json")
    data = json.load(open(kf)); qs = data.get("questions", [])
    if not qs: raise RuntimeError(f"0 questions parsed (mode={mode}, type={kind}, platform={platform}); format not matched")
    budget = vision.Budget(out_dir)
    # Stage 1: image boundaries + classification BEFORE verification, so verification knows which images each question has
    lab_imgs, why = ([], None)
    if kind in ("text-layer", "mixed"):
        lab_imgs, why = pageimages.extract(pdf, out_dir)
        if why: notes.append(why)
    img_dir = os.path.join(out_dir, "images")
    files = ([os.path.join(img_dir, f) for f in sorted(os.listdir(img_dir)) if f.lower().endswith(imgmod.IMG_EXT)] if os.path.isdir(img_dir) else []) + [os.path.join(out_dir, i["file"]) for i in lab_imgs]
    dec = imgmod.classify_dir(out_dir, budget if use_vision and budget.available() else None, files=files)
    meta_by_file = {i["file"]: i for i in lab_imgs}
    for rel, d in dec.items():
        m = meta_by_file.get(rel)
        if m: d.update({"question": m["question"], "page": m["page"], "bbox": m["bbox"], "placement": m["placement"]})
    import json as _j; _j.dump(dec, open(os.path.join(out_dir, "_image_decisions.json"), "w"), indent=1)
    for q in qs:   # attach useful/uncertain images so verification can check 'image referenced but missing'
        q["images"] = [{"file": d.get("clean") or rel} for rel, d in dec.items() if d.get("question") == q.get("number") and d["decision"] in ("useful", "uncertain")]
    progress("Image check", len(dec), len(dec), "images", "ok")
    print(f"[lab] verifying {len(qs)} questions against the source", flush=True)
    progress("Verify questions", 0, len(qs), "questions"); results = verify_all(qs, src); progress("Verify questions", len(qs), len(qs), "questions", "ok")
    flagged = [k for k, v in results.items() if v["status"] == "flagged"]
    log = []
    if flagged:
        print(f"[lab] {len(flagged)} flagged; vision fix={'on' if use_vision else 'off'}", flush=True)
        if use_vision:
            progress("Vision fix", 0, len(flagged), "questions")
            qs = vision_fix.fix_flagged(qs, results, src, budget, log)
            progress("Vision fix", len(flagged), len(flagged), "questions", "ok")
        else:
            for k in flagged: results[k].update({"status": "needs_human", "note": "vision fix disabled (--no-vision)"})
        if use_vision and budget.error and not budget.calls and not budget.cache_hits:
            notes.append(f"vision fix could not run: {budget.error}; flagged questions are marked needs_human")
    notes += src.warnings[:10]
    meta = {"parser_version": data.get("parser_version"), "mode": mode, "platform": platform, "source_type": kind, "pipeline": "lab-1"}
    key, view = report.build_views(stem, qs, results, dec, meta)
    json.dump(key, open(os.path.join(out_dir, "answer_key.json"), "w"), indent=1, ensure_ascii=False)
    sv = json.loads(json.dumps(view)); json.dump(sv, open(os.path.join(out_dir, "student_view.json"), "w"), indent=1, ensure_ascii=False)
    json.dump({"flagged": {q["number"]: q["flags"] for q in view["questions"] if q["flags"]}, "fix_log": log}, open(os.path.join(out_dir, "_flags.json"), "w"), indent=1, ensure_ascii=False)
    open(os.path.join(out_dir, "_flags_report.md"), "w", encoding="utf-8").write(report.flags_report(stem, view, results))
    if os.path.exists(os.path.join(out_dir, "viewer.html")): os.replace(os.path.join(out_dir, "viewer.html"), os.path.join(out_dir, "viewer_m2.html"))
    open(os.path.join(out_dir, "viewer.html"), "w", encoding="ascii").write(report.viewer(stem, view, out_dir))
    open(os.path.join(out_dir, "run_summary.md"), "w", encoding="utf-8").write(report.run_summary(stem, view, meta, budget, dec, time.time() - t0, notes))
    st = {}
    for q in view["questions"]: st[q["status"]] = st.get(q["status"], 0) + 1
    print(f"[lab] done: {len(qs)} questions {st}", flush=True)
    return {"total": len(qs), "status": st}

def main(argv):
    a = [x for x in argv if not x.startswith("--")]; opt = {}
    i = 0
    while i < len(argv):
        if argv[i] in ("--limit", "--mode"): opt[argv[i][2:]] = argv[i + 1]; a = [x for x in a if x != argv[i + 1]]; i += 1
        i += 1
    if len(a) < 2: sys.exit(__doc__)
    r = run(a[0], a[1], limit=int(opt["limit"]) if "limit" in opt else None, mode=opt.get("mode", os.environ.get("LAB_MODE", "auto")), use_vision="--no-vision" not in argv, skip_parse="--skip-parse" in argv)
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
