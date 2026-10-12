"""Stage 0: source analysis. Page images (ground truth), per-page text (text layer or cached OCR), type/platform detection."""
import os, re, shutil, subprocess

def _run(cmd, timeout=600):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:  # missing binary or timeout
        return 127, "", str(e)

def page_count(pdf):
    rc, out, _ = _run(["pdfinfo", pdf], 60)
    m = re.search(r"Pages:\s+(\d+)", out or "")
    if m: return int(m.group(1))
    try:
        import pypdf; return len(pypdf.PdfReader(pdf).pages)
    except Exception:
        return 0

def render_pages(pdf, out_dir, dpi=200, first=None, last=None):
    """Render pages to out_dir/_pages/page_NNN.png (skips pages already rendered). Returns list of paths."""
    d = os.path.join(out_dir, "_pages"); os.makedirs(d, exist_ok=True)
    n = page_count(pdf); first = first or 1; last = min(last or n, n)
    todo = [p for p in range(first, last + 1) if not os.path.exists(os.path.join(d, f"page_{p:03d}.png"))]
    if todo:
        lo, hi = min(todo), max(todo)
        rc, _, err = _run(["pdftoppm", "-r", str(dpi), "-f", str(lo), "-l", str(hi), "-png", pdf, os.path.join(d, "tmp")], 1800)
        for f in os.listdir(d):
            m = re.match(r"tmp-0*(\d+)\.png$", f)
            if m: os.replace(os.path.join(d, f), os.path.join(d, f"page_{int(m.group(1)):03d}.png"))
    return [os.path.join(d, f"page_{p:03d}.png") for p in range(first, last + 1) if os.path.exists(os.path.join(d, f"page_{p:03d}.png"))]

def _ocr(img_path, cache):
    if os.path.exists(cache):
        return open(cache, encoding="utf-8").read()
    try:
        import pytesseract; from PIL import Image
        txt = pytesseract.image_to_string(Image.open(img_path), lang="eng")
    except Exception as e:
        return ""  # caller treats empty as unreadable; never invent text
    os.makedirs(os.path.dirname(cache), exist_ok=True); open(cache, "w", encoding="utf-8").write(txt)
    return txt

class Source:
    def __init__(self, pdf, out_dir):
        self.pdf, self.out = pdf, out_dir
        self.n = page_count(pdf); self._txt = {}
        self.warnings = []
        self.layer_chars = None

    def text_layer(self, p):
        rc, out, _ = _run(["pdftotext", "-layout", "-f", str(p), "-l", str(p), self.pdf, "-"], 120)
        return out if rc == 0 else ""

    def page_text(self, p):
        if p in self._txt: return self._txt[p]
        t = self.text_layer(p)
        if len(t.strip()) < 30:
            imgs = render_pages(self.pdf, self.out, first=p, last=p)
            t = _ocr(imgs[0], os.path.join(self.out, "_ocr", f"page_{p:03d}.txt")) if imgs else ""
            if not t: self.warnings.append(f"page {p}: no text layer and OCR unavailable/empty")
        self._txt[p] = t; return t

    def kind(self):
        """text-layer | screenshot | mixed, from the first pages."""
        k = min(self.n, 5); chars = sum(len(self.text_layer(p).strip()) for p in range(1, k + 1))
        self.layer_chars = chars
        return "text-layer" if chars >= 400 else ("screenshot" if chars < 60 else "mixed")

    def platform(self):
        t = " ".join(self.page_text(p) for p in range(1, min(self.n, 3) + 1)).lower()
        for name, pats in (("PassMedicine", ["passmedicine", "question \\d+ of \\d+"]), ("PasTest", ["pastest"]), ("BMJ", ["bmj onexamination", "bmj"]),
                           ("eMedici", ["emedici"]), ("Amedex", ["amedex"])):
            if any(re.search(p, t) for p in pats): return name
        return "unknown"

def norm(s):
    s = (s or "").lower()
    s = re.sub(r"[‘’]", "'", s); s = re.sub(r"[“”]", '"', s)
    s = re.sub(r"[^a-z0-9%<>=.+\-/ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()
