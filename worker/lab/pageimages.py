"""Stage 1 (independent of m2): find every embedded image on every page with its exact bounding box, crop it tightly from the
rendered page, and map it to a question by position (below 'Question N of M', stem vs explanation by the 'Explanation' heading).
Classification (useful / noise) is done afterwards by lab.images. Needs PyMuPDF; otherwise returns [] and logs why."""
import os, re

MARK = re.compile(r"Question\s+(\d+)\s+of\s+(\d+)", re.I)

def extract(pdf, out_dir, dpi=200, max_pages=None):
    try:
        import pymupdf as fitz
    except Exception:
        try: import fitz
        except Exception: return [], "pymupdf missing: lab image-boundary detection skipped"
    d = os.path.join(out_dir, "images_lab"); os.makedirs(d, exist_ok=True)
    doc = fitz.open(pdf); res = []; cur = None; counts = {}
    scale = dpi / 72.0
    for pno in range(min(len(doc), max_pages or len(doc))):
        page = doc[pno]
        marks, expl = [], []
        for b in page.get_text("blocks"):
            t = (b[4] or "").strip(); m = MARK.match(t)
            if m: marks.append((b[1], int(m.group(1)), int(m.group(2))))
            elif re.match(r"(>\s*)?explanation\b", t, re.I): expl.append(b[1])
        marks.sort()
        infos = []
        try: infos = page.get_image_info(xrefs=True)
        except Exception: pass
        for im in sorted(infos, key=lambda x: x["bbox"][1]):
            x0, y0, x1, y1 = im["bbox"]
            if x1 - x0 < 2 or y1 - y0 < 2: continue
            above = [m for m in marks if m[0] <= y0 + 1]
            header = (not above) and y1 < page.rect.height * 0.12   # banner/logo strip above the first question marker
            if above: cur = (above[-1][1], above[-1][2])   # latest marker above the image on this page
            elif marks and not cur: cur = (marks[0][1], marks[0][2])
            q = None if header else (cur[0] if cur else None)
            # image before the first marker of the page belongs to the previous question (continuation), already `cur`
            place = "explanation" if any(e <= y0 + 1 and (not above or e >= above[-1][0]) for e in expl) else "stem"
            counts[(q, pno)] = counts.get((q, pno), 0) + 1
            name = f"lab_q{q if q is not None else 0}_p{pno + 1}_{counts[(q, pno)]}.png"
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=fitz.Rect(x0, y0, x1, y1), alpha=False)
                pix.save(os.path.join(d, name))
            except Exception as e:
                continue
            res.append({"file": os.path.join("images_lab", name), "page": pno + 1, "bbox": [round(v, 1) for v in (x0, y0, x1, y1)],
                        "question": q, "set_total": cur[1] if cur else None, "placement": "header" if header else place})
    return res, None
