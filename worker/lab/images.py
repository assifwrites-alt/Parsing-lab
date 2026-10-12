"""Stage 1: classify images extracted for each question as useful clinical media vs UI noise. Originals are never modified;
cropped useful copies go to images_clean/. Decisions are cached in _image_decisions.json."""
import json, os, re
import numpy as np

IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

def _load(path):
    from PIL import Image
    return Image.open(path).convert("RGB")

def _features(path):
    import cv2
    im = np.array(_load(path)); h, w = im.shape[:2]; g = cv2.cvtColor(im, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(g, 80, 160); sat = cv2.cvtColor(im, cv2.COLOR_RGB2HSV)[:, :, 1]
    return {"w": w, "h": h, "std": float(g.std()), "edge": float((edges > 0).mean()), "sat": float(sat.mean()), "dark": float((g < 40).mean())}

def _peer_chart(path):
    try:
        import pytesseract
        t = pytesseract.image_to_string(_load(path))
    except Exception:
        return False
    return len(re.findall(r"\d{1,3}\s?%", t)) >= 3

def _phash(path):
    try:
        import imagehash; return str(imagehash.phash(_load(path)))
    except Exception:
        return None

def autocrop(path, out_path, pad=6):
    im = _load(path); a = np.array(im).astype(int)
    bg = a[0, 0]; diff = (np.abs(a - bg).sum(axis=2) > 40)
    ys, xs = np.where(diff)
    if len(ys) == 0: return False
    box = (max(0, xs.min() - pad), max(0, ys.min() - pad), min(im.width, xs.max() + pad), min(im.height, ys.max() + pad))
    if (box[2] - box[0]) * (box[3] - box[1]) > 0.97 * im.width * im.height: return False
    im.crop(box).save(out_path); return True

def heuristic(f, path):
    w, h = f["w"], f["h"]; ar = max(w / max(h, 1), h / max(w, 1))
    if w * h < 6000 or min(w, h) < 40: return "noise", "too small (icon/button)", 0.95
    if ar > 8: return "noise", f"thin strip (aspect {ar:.1f})", 0.9
    if f["std"] < 6 and f["edge"] < 0.002: return "noise", "blank/flat region", 0.95
    if f["edge"] < 0.004 and f["std"] < 14: return "noise", "near-blank (scratch strip)", 0.7
    if _peer_chart(path): return "noise", "peer-response percentage chart", 0.85
    if w * h >= 60000 and f["edge"] >= 0.01: return "useful", f"large, structured ({w}x{h}, edge {f['edge']:.3f})", 0.85
    return "uncertain", f"ambiguous ({w}x{h}, edge {f['edge']:.3f}, std {f['std']:.0f})", 0.4

def classify_dir(out_dir, budget=None, files=None, repeat_threshold=3):
    """files: list of image paths (default: out_dir/images/*). Returns dict {relpath: decision}."""
    img_dir = os.path.join(out_dir, "images")
    files = files if files is not None else sorted(os.path.join(img_dir, f) for f in os.listdir(img_dir) if f.lower().endswith(IMG_EXT)) if os.path.isdir(img_dir) else []
    cache_f = os.path.join(out_dir, "_image_decisions.json")
    cache = json.load(open(cache_f)) if os.path.exists(cache_f) else {}
    res, hashes = {}, {}
    for p in files:
        rel = os.path.relpath(p, out_dir)
        if rel in cache and cache[rel].get("decision") in ("useful", "noise"): res[rel] = cache[rel]; continue
        try:
            f = _features(p)
        except Exception as e:
            res[rel] = {"decision": "uncertain", "reason": f"unreadable image: {e}", "method": "error"}; continue
        d, why, conf = heuristic(f, p); m = "heuristic"
        res[rel] = {"decision": d, "reason": why, "method": m, "confidence": conf, "size": [f["w"], f["h"]], "hash": _phash(p)}
        q = re.search(r"_q(\d+)_", os.path.basename(p)); res[rel]["question"] = int(q.group(1)) if q else None
    # repeated across questions -> logo/watermark
    by_hash = {}
    for rel, d in res.items():
        if d.get("hash") and d["decision"] != "noise": by_hash.setdefault(d["hash"], []).append(rel)
    for h, rels in by_hash.items():
        qs = {res[r].get("question") for r in rels}
        if len(rels) >= repeat_threshold and len(qs) >= repeat_threshold:
            for r in rels: res[r].update({"decision": "noise", "reason": f"identical image repeated in {len(qs)} questions (logo/watermark)", "method": "repetition"})
    # vision yes/no for uncertain ones
    if budget is not None:
        from . import vision
        for rel, d in res.items():
            if d["decision"] != "uncertain": continue
            t = vision.ask(budget, "image-classify", 'Is this image useful clinical/educational content for a medical exam question (clinical photo, ECG, X-ray, CT, graph, table-as-image, diagram) or UI noise (logo, icon, button, chat widget, watermark, header/footer, peer-response bar chart, scribble, blank)? Answer JSON only: {"useful": true|false, "reason": "..."}', [(os.path.join(out_dir, rel), None)], max_tokens=200)
            j = vision.json_from(t)
            if j is not None and "useful" in j:
                d.update({"decision": "useful" if j["useful"] else "noise", "reason": "vision: " + str(j.get("reason", ""))[:120], "method": "vision"})
    # autocrop + dedupe useful images within a question
    clean = os.path.join(out_dir, "images_clean"); seen = set()
    for rel, d in res.items():
        if d["decision"] != "useful": continue
        key = (d.get("question"), d.get("hash"))
        if d.get("hash") and key in seen: d.update({"decision": "noise", "reason": "duplicate of another image in the same question", "method": "dedupe"}); continue
        seen.add(key)
        os.makedirs(clean, exist_ok=True); dst = os.path.join(clean, os.path.basename(rel))
        try:
            d["clean"] = os.path.relpath(dst, out_dir) if autocrop(os.path.join(out_dir, rel), dst) else rel
        except Exception:
            d["clean"] = rel
    json.dump(res, open(cache_f, "w"), indent=1)
    return res
