#!/usr/bin/env python3
"""
PDF QBank Parser
=================
Konverts PassMedicine-jaisi question-page PDFs ko clean structured JSON mein.

Kya karta hai:
  1. PDF ke andar embedded screenshots (images) nikaalta hai
  2. Har image par OCR (tesseract) chalata hai
  3. Noise (headers, timestamps, nav buttons, footer URLs, toolbar icons, etc.) hata deta hai
  4. Question, options, %, correct answer, explanation, notes (headings/subheadings/
     bullets), mnemonic box aur diagram image ko structure karta hai
  5. Do JSON files banata hai:
       - <name>_answer_key.json   -> full data (%, correct answer, explanation sab kuch)
       - <name>_student_view.json -> attempt se pehle wala view (% aur correct answer chhupe)
  6. Diagram/graph jaisi non-text images ko images/ folder mein save karta hai

Usage:
    python3 pdf_qbank_parser.py path/to/file.pdf [output_dir]

Requirements:
    Desktop/Linux:
        pip install pymupdf --break-system-packages
        tesseract-ocr, pillow, numpy, scipy (autocrop ke liye)
    Termux (Android):
        pkg install mupdf-tools tesseract python
        pip install pillow numpy scipy
        (PyMuPDF ki zaroorat nahi -- script khud mutool istemal kar lega)
"""

import sys
import os
import re
import json


def _atomic_write_json_global(path, data):
    # Shared safe-write helper: likhta hai ek .tmp file mein pehle, phir
    # os.replace se ek hi atomic step mein asal file update hoti hai.
    # Beech mein Ctrl+C/crash/phone ka Termux kill karna ho, to purani
    # (theek) file hi bachi rehti hai -- kabhi bhi 0-byte/adhoori JSON
    # save nahi hoti. Poore file mein har JSON-save isi se hona chahiye.
    tmp_path = str(path) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)
import time
import subprocess
import shutil
import hashlib
from pathlib import Path

# --- ANSI colors + banner (Termux/terminal output ko colorful banane ke liye) ---
class C:
    GREEN = "\033[1;32m"
    CYAN = "\033[1;36m"
    YELLOW = "\033[1;33m"
    MAGENTA = "\033[1;35m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def acquire_wake_lock():
    """
    Android screen lock hone par Doze mode Termux ka network access
    suspend kar deta hai -- isi wajah se internet wapas aane ke baad bhi
    download retry reconnect nahi ho pata. termux-wake-lock (agar Termux:API
    installed hai) CPU aur network ko screen-off ke dauran bhi active
    rakhta hai. Agar command mojood nahi hai to chup-chap ignore karte hain.
    """
    try:
        subprocess.run(["termux-wake-lock"], capture_output=True, timeout=5)
    except Exception:
        pass


def print_banner():
    banner = r"""
 ____  ____    _   _    _    ____  ______   __
|  _ \|  _ \  | | | |  / \  |  _ \|  _ \ \ / /
| | | | |_) | | |_| | / _ \ | |_) | |_) \ V /
| |_| |  _ <  |  _  |/ ___ \|  _ <|  _ < | |
|____/|_| \_\ |_| |_/_/   \_\_| \_\_| \_\|_|
"""
    print(f"{C.GREEN}{C.BOLD}{banner}{C.RESET}")


try:
    import fitz  # PyMuPDF
    HAVE_FITZ = True
except ImportError:
    try:
        import pymupdf as fitz
        HAVE_FITZ = True
    except ImportError:
        HAVE_FITZ = False

# ---- PyMuPDF na ho (jaise Termux) to pure-python fallback: pypdf + poppler(pdftotext) ----
_PDFGEO_SRC = r'''# -*- coding: utf-8 -*-
"""
pdfgeo -- PyMuPDF (fitz) ka chhota pure-Python replacement, sirf utna jitna m2.py ko chahiye.
Termux jaise jagah jahan `pip install pymupdf` build nahi hota.

Dependencies:  pypdf (pure python: `pip install pypdf`)  +  poppler (`pkg install poppler` -> pdftotext)

Provide karta hai (fitz-style):  open(path) -> Doc;  len(doc); doc[i] -> Page
Page:  .rect  get_drawings()  get_text("words"|"dict")  get_links()  get_textbox(rect)
       find_tables(strategy="lines") -> obj.tables[*].extract()
Rect(x0,y0,x1,y1)
"""
import math
import re
import subprocess
import xml.etree.ElementTree as ET


class Rect:
    def __init__(self, x0=0, y0=0, x1=0, y1=0):
        if not isinstance(x0, (int, float)):       # Rect(tuple/Rect)
            x0, y0, x1, y1 = list(x0)[:4]
        self.x0, self.y0, self.x1, self.y1 = float(x0), float(y0), float(x1), float(y1)

    width = property(lambda s: s.x1 - s.x0)
    height = property(lambda s: s.y1 - s.y0)

    def __iter__(self):
        return iter((self.x0, self.y0, self.x1, self.y1))

    def __repr__(self):
        return "Rect(%.1f, %.1f, %.1f, %.1f)" % (self.x0, self.y0, self.x1, self.y1)


# ---------------------------------------------------------------- content stream
def _mul(a, b):          # a x b  (PDF matrices, row-vector convention)
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def _pt(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _num(o):
    try:
        return float(o)
    except Exception:
        return 0.0


class _Page:
    def __init__(self, doc, idx, pypage):
        self.doc, self.idx, self.pp = doc, idx, pypage
        mb = [float(v) for v in (pypage.get("/CropBox") or pypage.mediabox)]
        self._ox, self._oy = min(mb[0], mb[2]), min(mb[1], mb[3])
        self._w, self._h = abs(mb[2] - mb[0]), abs(mb[3] - mb[1])
        self.rect = Rect(0, 0, self._w, self._h)
        self._draw = None
        self._words = None
        self._lines = None

    # ---------- drawings
    def _to_top(self, x, y):
        return (x - self._ox, self._h - (y - self._oy))

    def get_drawings(self):
        if self._draw is not None:
            return self._draw
        out = []
        try:
            from pypdf.generic import ContentStream
            cs = ContentStream(self.pp.get_contents(), self.doc.reader)
            res = self.pp.get("/Resources")
            self._run(cs.operations, res, (1, 0, 0, 1, 0, 0), out, 0)
        except Exception:
            pass
        self._draw = out
        return out

    def _run(self, ops, res, ctm0, out, depth):
        from pypdf.generic import ContentStream
        ctm = ctm0
        stack = []
        fill, stroke = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        path = []        # list of subpaths: [(op, pts)]
        cur = None
        pts_all = []
        segs = []        # line segments (stroke candidates) in user->device coords
        lw = 1.0

        def col(operator, a):
            n = [_num(v) for v in a]
            if operator in ("g", "G") and len(n) == 1:
                return (n[0], n[0], n[0])
            if operator in ("rg", "RG") and len(n) == 3:
                return tuple(n)
            if operator in ("k", "K") and len(n) == 4:
                c, m, y, k = n
                return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
            if operator in ("sc", "scn", "SC", "SCN"):
                if len(n) == 1:
                    return (n[0], n[0], n[0])
                if len(n) == 3:
                    return tuple(n)
                if len(n) == 4:
                    c, m, y, k = n
                    return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
            return None

        def flush(do_fill, do_stroke):
            nonlocal path, segs
            if not path:
                return
            xs, ys = [], []
            for p in path:
                for (x, y) in p["pts"]:
                    X, Y = self._to_top(*_pt(ctm, x, y))
                    xs.append(X); ys.append(Y)
            if xs:
                r = Rect(min(xs), min(ys), max(xs), max(ys))
                sg = []
                for p in path:
                    pp = [self._to_top(*_pt(ctm, x, y)) for (x, y) in p["pts"]]
                    if p.get("rect"):
                        a, b, c, d = pp
                        sg += [(a, b), (b, c), (c, d), (d, a)]
                    elif p.get("lines"):
                        for i in range(len(pp) - 1):
                            sg.append((pp[i], pp[i + 1]))
                lwd = lw * math.sqrt(abs(ctm[0] * ctm[3] - ctm[1] * ctm[2]))
                out.append({"rect": r, "fill": fill if do_fill else None,
                            "color": stroke if do_stroke else None,
                            "type": ("f" if do_fill else "") + ("s" if do_stroke else ""),
                            "segs": sg, "lw": lwd,
                            "isrect": len(path) == 1 and bool(path[0].get("rect"))})
            path = []

        for operands, operator in ops:
            op = operator.decode() if isinstance(operator, bytes) else str(operator)
            if op == "q":
                stack.append((ctm, fill, stroke, lw))
            elif op == "Q":
                if stack:
                    ctm, fill, stroke, lw = stack.pop()
            elif op == "cm" and len(operands) == 6:
                ctm = _mul([_num(v) for v in operands], ctm)
            elif op == "w" and operands:
                lw = _num(operands[0])
            elif op in ("rg", "g", "k", "sc", "scn"):
                c = col(op, operands)
                if c:
                    fill = c
            elif op in ("RG", "G", "K", "SC", "SCN"):
                c = col(op, operands)
                if c:
                    stroke = c
            elif op == "re" and len(operands) == 4:
                x, y, w, h = [_num(v) for v in operands]
                path.append({"pts": [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], "rect": True})
                cur = None
            elif op == "m" and len(operands) == 2:
                cur = {"pts": [(_num(operands[0]), _num(operands[1]))], "lines": True}
                path.append(cur)
            elif op == "l" and len(operands) == 2:
                if cur is None:
                    cur = {"pts": [], "lines": True}; path.append(cur)
                cur["pts"].append((_num(operands[0]), _num(operands[1])))
            elif op == "c" and len(operands) == 6:
                if cur is None:
                    cur = {"pts": [], "lines": False}; path.append(cur)
                v = [_num(t) for t in operands]
                cur["pts"] += [(v[0], v[1]), (v[2], v[3]), (v[4], v[5])]
                cur["lines"] = False
            elif op in ("v", "y") and len(operands) == 4:
                if cur is None:
                    cur = {"pts": [], "lines": False}; path.append(cur)
                v = [_num(t) for t in operands]
                cur["pts"] += [(v[0], v[1]), (v[2], v[3])]
                cur["lines"] = False
            elif op == "h":
                if cur is not None and cur.get("lines") and cur["pts"]:
                    cur["pts"].append(cur["pts"][0])
            elif op in ("f", "F", "f*"):
                flush(True, False)
            elif op in ("B", "B*", "b", "b*"):
                flush(True, True)
            elif op in ("S", "s"):
                flush(False, True)
            elif op == "n":
                path = []
            elif op == "Do" and operands and depth < 4 and res is not None:
                try:
                    xo = res.get("/XObject")
                    xo = xo.get_object() if xo is not None else None
                    obj = xo[operands[0]].get_object() if xo is not None else None
                    if obj is not None and obj.get("/Subtype") == "/Form":
                        m = obj.get("/Matrix")
                        mm = tuple(_num(v) for v in m) if m else (1, 0, 0, 1, 0, 0)
                        sub = ContentStream(obj, self.doc.reader)
                        r2 = obj.get("/Resources") or res
                        self._run(sub.operations, r2, _mul(mm, ctm), out, depth + 1)
                except Exception:
                    pass

    # ---------- text (poppler)
    def _load_text(self):
        if self._words is not None:
            return
        words, lines = [], []
        try:
            xml = subprocess.run(
                ["pdftotext", "-bbox-layout", "-f", str(self.idx + 1), "-l", str(self.idx + 1),
                 self.doc.path, "-"], capture_output=True, text=True, errors="replace").stdout
            xml = re.sub(r"<!DOCTYPE[^>]*>", "", xml)
            xml = xml.replace(' xmlns="http://www.w3.org/1999/xhtml"', "")
            root = ET.fromstring(xml)
            bno = 0
            for lno_global, blk in enumerate(root.iter("block")):
                for lno, ln in enumerate(blk.iter("line")):
                    ws = []
                    for w in ln.iter("word"):
                        t = w.text or ""
                        b = (float(w.get("xMin")), float(w.get("yMin")), float(w.get("xMax")), float(w.get("yMax")))
                        ws.append(b + (t, lno_global, lno, len(ws)))
                    if not ws:
                        continue
                    words += ws
                    x0, y0 = float(ln.get("xMin")), float(ln.get("yMin"))
                    x1, y1 = float(ln.get("xMax")), float(ln.get("yMax"))
                    lines.append(((x0, y0, x1, y1), " ".join(w[4] for w in ws), ws))
        except Exception:
            pass
        self._words, self._lines = words, lines

    def get_text(self, kind="text"):
        self._load_text()
        if kind == "words":
            return list(self._words)
        if kind == "dict":
            blocks = []
            for bb, txt, ws in self._lines:
                h = bb[3] - bb[1]
                # poppler bbox height ~ 1.1-1.25 x font size
                size = h / FONT_H_RATIO
                blocks.append({"lines": [{"bbox": bb, "spans": [{"text": txt, "size": size}]}]})
            return {"blocks": blocks}
        return "\n".join(t for _, t, _ in self._lines)

    def get_textbox(self, rect):
        self._load_text()
        r = Rect(rect)
        got = []
        for w in self._words:
            cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            if r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1:
                got.append(w)
        got.sort(key=lambda w: (round(w[1] / 4), w[0]))
        return " ".join(w[4] for w in got)

    # ---------- links
    def get_links(self):
        out = []
        try:
            for a in self.pp.get("/Annots") or []:
                a = a.get_object()
                if a.get("/Subtype") != "/Link":
                    continue
                act = a.get("/A")
                uri = None
                if act is not None:
                    act = act.get_object()
                    uri = act.get("/URI")
                if not uri:
                    continue
                r = [float(v) for v in a["/Rect"]]
                x0, x1 = sorted([r[0], r[2]])
                y0, y1 = sorted([r[1], r[3]])
                ax0, ay1 = self._to_top(x0, y0)
                ax1, ay0 = self._to_top(x1, y1)
                out.append({"uri": str(uri), "from": Rect(ax0, ay0, ax1, ay1), "kind": 2})
        except Exception:
            pass
        return out

    # ---------- tables
    def find_tables(self, strategy="lines"):
        return _TableFinder(self)


FONT_H_RATIO = 1.33   # poppler line height / font size (calibrated vs PyMuPDF)


class _TableFinder:
    def __init__(self, page):
        self.tables = []
        try:
            self._build(page)
        except Exception:
            self.tables = []

    def _build(self, page):
        H, V = [], []   # (y, x0, x1) , (x, y0, y1)
        for d in page.get_drawings():
            r = d["rect"]
            if d.get("fill") is not None and d.get("isrect") and not d.get("color"):
                if r.height <= 2.5 and r.width > 8:
                    H.append((r.y0 + r.height / 2, r.x0, r.x1))
                elif r.width <= 2.5 and r.height > 8:
                    V.append((r.x0 + r.width / 2, r.y0, r.y1))
            if d.get("color") is not None:
                for (a, b) in d.get("segs", []):
                    if abs(a[1] - b[1]) < 0.6 and abs(a[0] - b[0]) > 8:
                        H.append(((a[1] + b[1]) / 2, min(a[0], b[0]), max(a[0], b[0])))
                    elif abs(a[0] - b[0]) < 0.6 and abs(a[1] - b[1]) > 8:
                        V.append(((a[0] + b[0]) / 2, min(a[1], b[1]), max(a[1], b[1])))
        if len(H) < 2 or len(V) < 2:
            return
        # union-find on intersecting edges
        n = len(H) + len(V)
        par = list(range(n))

        def f(i):
            while par[i] != i:
                par[i] = par[par[i]]; i = par[i]
            return i
        tol = 2.0
        for i, (y, a0, a1) in enumerate(H):
            for j, (x, b0, b1) in enumerate(V):
                if a0 - tol <= x <= a1 + tol and b0 - tol <= y <= b1 + tol:
                    par[f(i)] = f(len(H) + j)
        comps = {}
        for i in range(n):
            comps.setdefault(f(i), []).append(i)
        for idxs in comps.values():
            hs = [H[i] for i in idxs if i < len(H)]
            vs = [V[i - len(H)] for i in idxs if i >= len(H)]
            ys = _cluster([e[0] for e in hs])
            xs = _cluster([e[0] for e in vs])
            if len(ys) < 3 or len(xs) < 2:      # kam az kam 2 rows x 1+ col boundaries
                continue
            t = _Table(page, ys, xs, hs, vs)
            if t.ok:
                self.tables.append(t)
        self.tables.sort(key=lambda t: (t.bbox[1], t.bbox[0]))


def _cluster(vals, tol=2.0):
    vals = sorted(vals)
    out = []
    for v in vals:
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


class _Table:
    def __init__(self, page, ys, xs, hs, vs):
        self.page, self.ys, self.xs, self.hs, self.vs = page, ys, xs, hs, vs
        self.bbox = (xs[0], ys[0], xs[-1], ys[-1])
        self.ok = len(ys) >= 3 and len(xs) >= 3 or (len(ys) >= 3 and len(xs) >= 2)

    def _has_v(self, x, y0, y1):
        for (vx, a, b) in self.vs:
            if abs(vx - x) < 2.5 and a - 2 <= y0 + 1 and b + 2 >= y1 - 1:
                return True
        return False

    def _has_h(self, y, x0, x1):
        for (hy, a, b) in self.hs:
            if abs(hy - y) < 2.5 and a - 2 <= x0 + 1 and b + 2 >= x1 - 1:
                return True
        return False

    def extract(self):
        page = self.page
        page._load_text()
        ys, xs = self.ys, self.xs
        nr, nc = len(ys) - 1, len(xs) - 1
        # cell-owner map for merged cells
        owner = [[None] * nc for _ in range(nr)]
        for r in range(nr):
            for c in range(nc):
                if owner[r][c] is not None:
                    continue
                # extend right while no vertical edge
                c2 = c
                while c2 + 1 < nc and not self._has_v(xs[c2 + 1], ys[r], ys[r + 1]):
                    c2 += 1
                r2 = r
                while r2 + 1 < nr and not self._has_h(ys[r2 + 1], xs[c], xs[c2 + 1]):
                    r2 += 1
                for rr in range(r, r2 + 1):
                    for cc in range(c, c2 + 1):
                        owner[rr][cc] = (r, c, r2, c2)
        texts = {}
        for w in page._words:
            cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            if not (xs[0] <= cx <= xs[-1] and ys[0] <= cy <= ys[-1]):
                continue
            c = next((i for i in range(nc) if xs[i] <= cx <= xs[i + 1]), None)
            r = next((i for i in range(nr) if ys[i] <= cy <= ys[i + 1]), None)
            if r is None or c is None:
                continue
            o = owner[r][c]
            texts.setdefault((o[0], o[1]), []).append(w)
        rows = []
        for r in range(nr):
            row = []
            for c in range(nc):
                o = owner[r][c]
                if (o[0], o[1]) != (r, c):
                    row.append(None)
                    continue
                ws = texts.get((r, c), [])
                ws.sort(key=lambda w: (round(((w[1] + w[3]) / 2) / 4), w[0]))
                lines, lastk = [], None
                for w in ws:
                    k = round(((w[1] + w[3]) / 2) / 4)
                    if lastk is None or abs(((w[1] + w[3]) / 2) - lastk) > 4:
                        lines.append([w[4]])
                        lastk = (w[1] + w[3]) / 2
                    else:
                        lines[-1].append(w[4])
                row.append("\n".join(" ".join(l) for l in lines))
            rows.append(row)
        return rows


class Doc:
    def __init__(self, path):
        from pypdf import PdfReader
        self.path = path
        self.reader = PdfReader(path)
        self._n = len(self.reader.pages)
        self._cache = {}

    def __len__(self):
        return self._n

    def __getitem__(self, i):
        if i < 0:
            i += self._n
        if i not in self._cache:
            if len(self._cache) > 40:
                self._cache.clear()
            self._cache[i] = _Page(self, i, self.reader.pages[i])
        return self._cache[i]

    def close(self):
        pass


def open(path):
    return Doc(path)


# =====================================================================================
#  OCR backend -- image-only PDFs (har page ek screenshot) ke liye.
#  Needs: poppler (pdftoppm, pdfinfo) + tesseract + Pillow.   Termux: pkg install tesseract poppler
#  Output cache hota hai (~/.m2_ocr/<pdf>_<size>/), isliye dobara chalane pe OCR skip.
# =====================================================================================
import json as _json
import os as _os
import shutil as _shutil
import builtins as _b
_bopen = _b.open

OCR_DPI = 130
_BULLET_CHARS = set("•*e°o©®¢«»·~●○◦∙éè")


def ocr_available():
    return bool(_shutil.which("tesseract") and _shutil.which("pdftoppm"))


def pdf_num_pages(pdf):
    try:
        out = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        n = int(m.group(1)) if m else 0
        _mx = int(_os.environ.get("M2_MAX_PAGES", "0") or 0) if "_os" in globals() else int(__import__("os").environ.get("M2_MAX_PAGES", "0") or 0)
        return min(n, _mx) if _mx > 0 else n
    except Exception:
        return 0


def pdf_is_image_only(pdf):
    try:
        t = subprocess.run(["pdftotext", "-l", "4", pdf, "-"], capture_output=True, text=True).stdout
        return len(re.findall(r"[A-Za-z]{3,}", t)) < 30
    except Exception:
        return False


def _ocr_dir(pdf):
    st = _os.stat(pdf)
    base = _os.path.splitext(_os.path.basename(pdf))[0]
    d = _os.path.join(_os.path.expanduser("~"), ".m2_ocr", "%s_%d_%s" % (re.sub(r"[^A-Za-z0-9_.-]", "_", base), st.st_size, OCR_CACHE_VER))
    _os.makedirs(d, exist_ok=True)
    return d


def _green_bars(im, scale):
    """Green vertical answer-bar (left margin) ke y-ranges, PDF points mein."""
    from PIL import ImageChops
    W, H = im.size
    r, g, b = im.convert("RGB").split()
    mr = r.point(lambda v: 255 if v < 80 else 0)
    mg = g.point(lambda v: 255 if 80 < v < 190 else 0)
    mb = b.point(lambda v: 255 if v < 80 else 0)
    mask = ImageChops.multiply(ImageChops.multiply(mr, mg), mb)
    strip = mask.crop((int(W * 0.12), 0, int(W * 0.24), H)).resize((1, H), 2)   # BOX
    px = list(strip.getdata())
    bars, start = [], None
    for y, v in enumerate(px + [0]):
        on = v > 10
        if on and start is None:
            start = y
        elif not on and start is not None:
            if y - start >= 8:
                bars.append((start * scale, y * scale))
            start = None
    return bars



def _parse_tsv(tsv, scale):
    words = []
    for ln in tsv.split("\n")[1:]:
        c = ln.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        try:
            conf = float(c[10])
            if conf < 0:
                continue
            x, y, w, h = int(c[6]), int(c[7]), int(c[8]), int(c[9])
        except ValueError:
            continue
        words.append([x * scale, y * scale, (x + w) * scale, (y + h) * scale, c[11],
                      int(c[2]) * 1000 + int(c[3]) * 100 + int(c[4]), conf])
    return words


def _pct_strip_words(im, png, scale, words, env):
    """Right-side percentage column ko enlarge+darken karke digits-only OCR; jo % main OCR mein na mile wo add."""
    from PIL import ImageOps
    W, H = im.size
    x0 = int(W * 0.72)
    crop = im.convert("L").crop((x0, 0, int(W * 0.90), H))
    crop = ImageOps.autocontrast(crop, cutoff=1)
    crop = crop.point(lambda v: 0 if v < 215 else 255)      # halka grey % bhi kala ho jaye
    crop = crop.resize((crop.width * 3, crop.height * 3))
    cp = png + ".strip.png"
    crop.save(cp)
    tsv = subprocess.run(["tesseract", cp, "stdout", "-l", "eng", "--psm", "6", "-c",
                          "tessedit_char_whitelist=0123456789%", "tsv"],
                         capture_output=True, text=True, errors="replace", env=env).stdout
    try:
        _os.remove(cp)
    except Exception:
        pass
    have = [w for w in words if re.match(r"^\d{1,3}%$", w[4]) and w[0] > W * scale * 0.7]
    out = []
    for ln in tsv.split("\n")[1:]:
        c = ln.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        t = c[11].strip()
        if not re.match(r"^\d{1,3}%?$", t):
            continue
        try:
            x, y, w, h = int(c[6]), int(c[7]), int(c[8]), int(c[9])
        except ValueError:
            continue
        if not t.endswith("%"):
            t += "%"
        if int(t[:-1]) > 100:
            continue
        yy0, yy1 = y / 3.0 * scale, (y + h) / 3.0 * scale
        cy = (yy0 + yy1) / 2
        if any(abs(((o[1] + o[3]) / 2) - cy) < (yy1 - yy0) * 0.8 for o in have):
            continue
        xx0 = (x0 + x / 3.0) * scale
        # sirf chhoti option-lines ke saath jodo (right edge < 76% width), prose lines ke saath nahi
        ln_w = [o for o in words if o[1] - 2 <= cy <= o[3] + 2]
        same = [o for o in ln_w if o[0] < W * scale * 0.7]
        if not same or max(o[2] for o in ln_w) > W * scale * 0.76:
            continue
        if re.search(r"\d%$", max(same, key=lambda o: o[2])[4]):
            continue
        out.append([xx0, yy0, xx0 + (w / 3.0) * scale, yy1, t, 99000 + len(out), 50.0])
    return out


def _refine_pcts(im, words, scale, env):
    """Option-% ("11%" ko "1%" padh lena) -- har % token ko akela crop karke bade scale par dobara padho."""
    from PIL import ImageOps
    import tempfile
    W, H = im.size
    L = im.convert("L")
    P = [w for w in words if re.match(r"^\d{1,3}%$", w[4]) and w[0] > W * scale * 0.62]
    if not (2 <= len(P) <= 9):
        return
    for w in P:
        bx = (max(0, int(w[0] / scale) - 14), max(0, int(w[1] / scale) - 5), min(W, int(w[2] / scale) + 10), min(H, int(w[3] / scale) + 5))
        if bx[2] - bx[0] < 6 or bx[3] - bx[1] < 4:
            continue
        c = ImageOps.autocontrast(L.crop(bx), cutoff=1).point(lambda v: 0 if v < 200 else 255)
        c = ImageOps.expand(c.resize((c.width * 6, c.height * 6), 1), border=30, fill=255)
        fd, p = tempfile.mkstemp(suffix=".png")
        _os.close(fd)
        c.save(p)
        try:
            out = subprocess.run(["tesseract", p, "stdout", "-l", "eng", "--psm", "7", "-c",
                                  "tessedit_char_whitelist=0123456789%"], capture_output=True, text=True,
                                 errors="replace", env=env).stdout.strip()
        finally:
            try:
                _os.remove(p)
            except Exception:
                pass
        t = re.sub(r"\s+", "", out)
        if re.match(r"^\d{1,3}$", t):
            t += "%"
        if re.match(r"^\d{1,3}%$", t) and int(t[:-1]) <= 100 and t != w[4] and len(t) > len(w[4]) and t.endswith(w[4]):
            w[4] = t                       # sirf "lamba, usi ka extension" (1% -> 11%) accept


def _recover_rows(im, png, scale, words, env):
    """Option rows jinka % OCR se gir gaya (cluster ke upar/neeche): indent se pehchano, % crop se dobara padho."""
    from PIL import ImageOps
    W, H = im.size
    Wp = W * scale
    d = {"w": Wp, "h": H * scale, "words": words}
    cl = _pct_block(d)
    if not cl or len(cl) < 3:
        return []
    cs = [c for c, _ in cl]
    diffs = sorted(cs[i + 1] - cs[i] for i in range(len(cs) - 1))
    pitch = diffs[len(diffs) // 2]
    groups = {}
    for w in words:
        if w[5] < 99000 and not re.match(r"^\d{1,3}%$", w[4]):
            groups.setdefault(w[5], []).append(w)
    G = []
    for ws in groups.values():
        ws = sorted(ws, key=lambda w: w[0])
        while ws and len(ws[0][4]) <= 2 and not re.search(r"[A-Za-z0-9]", ws[0][4]):
            ws = ws[1:]
        if not ws:
            continue
        G.append(((min(w[1] for w in ws) + max(w[3] for w in ws)) / 2, ws[0][0], max(w[2] for w in ws)))
    def grp_near(c):
        best = None
        for gc, gx0, gx1 in G:
            if abs(gc - c) <= pitch * 0.5 and gx1 < Wp * 0.72 and (best is None or abs(gc - c) < abs(best[0] - c)):
                best = (gc, gx0, gx1)
        return best
    x0s = []
    for c in cs:
        g = grp_near(c)
        if g:
            x0s.append(g[1])
    if len(x0s) < 2:
        return []
    x0s.sort()
    optx = x0s[len(x0s) // 2]
    pctx = sorted(e[1][0] for e in cl)[len(cl) // 2]
    out = []

    def read_pct(yc):
        y0 = max(0, int((yc - pitch * 0.45) / scale)); y1 = min(H, int((yc + pitch * 0.45) / scale))
        x0 = int(W * 0.66); x1 = int(W * 0.96)
        crop = im.convert("L").crop((x0, y0, x1, y1))
        crop = ImageOps.autocontrast(crop, cutoff=1).point(lambda v: 0 if v < 215 else 255)
        crop = crop.resize((crop.width * 5, crop.height * 5))
        cp = png + ".row.png"
        crop.save(cp)
        t = subprocess.run(["tesseract", cp, "stdout", "-l", "eng", "--psm", "7", "-c",
                            "tessedit_char_whitelist=0123456789%"], capture_output=True, text=True,
                           errors="replace", env=env).stdout
        try:
            _os.remove(cp)
        except Exception:
            pass
        m = re.search(r"(\d{1,3})", t)
        return (m.group(1) + "%") if m and int(m.group(1)) <= 100 else "0%"

    for direction in (-1, 1):
        cur = cs[0] if direction < 0 else cs[-1]
        for _ in range(3):
            cand = None
            for gc, gx0, gx1 in G:
                dd = (gc - cur) * direction
                if 0.5 * pitch <= dd <= 1.6 * pitch and gx1 < Wp * 0.72 and optx - 2 * scale <= gx0 <= optx + 5 * scale:
                    if cand is None or dd < (cand[0] - cur) * direction:
                        cand = (gc, gx0, gx1)
            if not cand:
                break
            t = read_pct(cand[0])
            out.append([pctx, cand[0] - 3, pctx + 12, cand[0] + 3, t, 99500 + len(out), 40.0])
            cur = cand[0]
    return out



# ---------------------------------------------------------------- image-style layer (tables / boxes / teal / bullets)
OCR_CACHE_VER = "v10"


def _runs(rowbytes, minlen, gap=2):
    b = rowbytes
    for _ in range(2):
        b = re.sub(b"\xff\x00{1,%d}\xff" % gap, lambda m: b"\xff" * len(m.group(0)), b)
    return [(m.start(), m.end()) for m in re.finditer(b"\xff+", b) if m.end() - m.start() >= minlen]


def _find_grids(im, excl=None):
    """Page image se bordered tables (>=3 horizontal lines) aur single-row boxes (2 lines). Pixel coords."""
    from PIL import ImageChops
    W, H = im.size
    L = im.convert("L")
    core = L.point(lambda v: 255 if 100 <= v <= 240 else 0)

    def _thin(dx, dy):
        a = ImageChops.subtract(ImageChops.offset(L, dx, dy), L).point(lambda v: 255 if v >= 5 else 0)
        b = ImageChops.subtract(ImageChops.offset(L, -dx, -dy), L).point(lambda v: 255 if v >= 5 else 0)
        return ImageChops.multiply(ImageChops.multiply(a, b), core)

    m = _thin(0, 4)          # horizontal thin lines (shaded fills nahi)
    mv = _thin(4, 0)         # vertical thin lines
    prof = m.resize((1, H), 2).tobytes()
    minrun = int(W * 0.17)
    segs = []
    for yy in range(H):
        if prof[yy] * W / 255.0 >= minrun * 0.9:
            row = m.crop((0, yy, W, yy + 1)).tobytes()
            for (a, b) in _runs(row, minrun):
                segs.append((yy, a, b))
    lines = []
    for s in sorted(segs):
        for ln in lines:
            if s[0] - ln["y1"] <= 2 and abs(s[1] - ln["x0"]) <= 6 and abs(s[2] - ln["x1"]) <= 6:
                ln["y1"] = s[0]
                break
        else:
            lines.append({"y0": s[0], "y1": s[0], "x0": s[1], "x1": s[2]})
    lines.sort(key=lambda l: l["y0"])
    if excl:
        lines = [l for l in lines if not any(e0 <= l["y0"] <= e1 for (e0, e1) in excl)]

    def _vert_bridge(x0, x1, ya, yb):
        # Bade gap (lamba, text-heavy row jaise 'Appearance') ko bhi same
        # table maanta hai AGAR us gap ke dauraan table ke left/right
        # border par continuous vertical line mile -- matlab row sirf
        # lambi hai, ye do alag tables nahi hain.
        ya, yb = max(0, ya), min(H, yb)
        n = yb - ya
        if n < 4:
            return True
        # Photos/diagram boxes ka apna frame bhi continuous vertical
        # border deta hai (stacked photos isi wajah se warna merge ho
        # jaate), lekin unka content dense/busy hota hai -- asli table
        # row sparse text hoti hai (zyadatar white background). Dark-
        # pixel density se photo vs text-row differentiate karte hain.
        region = L.crop((x0, ya, x1, yb))
        dark_frac = sum(region.histogram()[:200]) / max(1, region.width * region.height)
        if dark_frac > 0.35:
            return False
        for x in (x0, x1):
            best = 0
            for xc in range(max(0, x - 3), min(W, x + 4)):
                on = mv.crop((xc, ya, xc + 1, yb)).tobytes().count(b"\xff")
                if on > best:
                    best = on
            if best >= 0.8 * n:
                return True
        return False

    tabs = []
    for ln in lines:
        yc = (ln["y0"] + ln["y1"]) // 2
        for t in tabs:
            dy = yc - t["hs"][-1]
            if dy <= 0 or abs(ln["x0"] - t["x0"]) > 8 or abs(ln["x1"] - t["x1"]) > 8:
                continue
            if dy <= H * 0.13 or (dy <= H * 0.75 and _vert_bridge(t["x0"], t["x1"], t["hs"][-1], yc)):
                t["hs"].append(yc)
                break
        else:
            tabs.append({"x0": ln["x0"], "x1": ln["x1"], "hs": [yc]})
    tables, boxes = [], []
    for t in tabs:
        y0, y1 = t["hs"][0], t["hs"][-1]
        if excl and any(y0 < e1 and y1 > e0 for (e0, e1) in excl):
            continue
        if len(t["hs"]) == 2:
            if t["x1"] - t["x0"] >= W * 0.35 and 20 <= y1 - y0 <= H * 0.22:
                boxes.append((t["x0"], y0, t["x1"], y1))
            continue
        if len(t["hs"]) < 3:
            continue
        x0, x1 = t["x0"], t["x1"]
        cp = mv.crop((x0 - 2, y0, x1 + 3, y1 + 1)).resize((x1 - x0 + 5, 1), 2).tobytes()
        vs = []
        for i, v in enumerate(cp):
            if v >= 255 * 0.5:
                xx = x0 - 2 + i
                if vs and xx - vs[-1][1] <= 2:
                    vs[-1][1] = xx
                else:
                    vs.append([xx, xx])
        vs = [(a + b) // 2 for a, b in vs]
        if len(vs) < 2:
            vs = [x0, x1]
        else:
            if vs[0] - x0 > 6:
                vs.insert(0, x0)
            if x1 - vs[-1] > 6:
                vs.append(x1)
        t["vs"] = vs
        tables.append(t)
    return tables, boxes


def _tess_words(img, psm, scale, ox, oy, border=20):
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".png")
    _os.close(fd)
    img.save(p)
    try:
        tsv = subprocess.run(["tesseract", p, "stdout", "-l", "eng", "--psm", str(psm), "tsv"],
                             capture_output=True, text=True, errors="replace",
                             env=dict(_os.environ, OMP_THREAD_LIMIT="1")).stdout
    finally:
        try:
            _os.remove(p)
        except Exception:
            pass
    words = []
    for ln in tsv.split("\n")[1:]:
        c = ln.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        try:
            x, y, w, h = [int(c[i]) for i in (6, 7, 8, 9)]
            cf = float(c[10])
        except ValueError:
            continue
        if cf < 15:
            continue
        words.append(((x - border) / scale + ox, (y - border) / scale + oy, (x - border + w) / scale + ox, (y - border + h) / scale + oy, c[11]))
    return words


def _prep_crop(L, box, scale):
    from PIL import ImageOps, ImageFilter
    cl = L.crop(box)
    cl = cl.resize((max(1, cl.width * scale), max(1, cl.height * scale)), 1 if scale >= 5 else 1)
    return ImageOps.expand(cl, border=20, fill=255)


_KEEP_TOK = ("-", "\u2013", "\u2014", "/", "<", ">", "+", "%")


def _cell_text(ws):
    ws = [w for w in ws if re.search(r"[A-Za-z0-9]", w[4]) or w[4] in _KEEP_TOK]
    ws.sort(key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    lines = []
    for w in ws:
        yc = (w[1] + w[3]) / 2
        if lines and abs(yc - lines[-1][0]) < (w[3] - w[1]) * 0.6:
            lines[-1][1].append(w)
        else:
            lines.append([yc, [w]])
    txt = " ".join(" ".join(x[4] for x in sorted(l[1], key=lambda w: w[0])) for l in lines)
    txt = re.sub(r"\s+", " ", txt).strip()
    txt = re.sub(r"^(?:[Oo@\u00a9\u00ae]\s+)(?=[A-Z])", "", txt)      # header ka eye-icon ("Findings O")
    txt = re.sub(r"(?<=[A-Za-z])\s+[Oo@\u00a9]$", "", txt)
    txt = {"Nat": "Na+", "Na*": "Na\u207a", "Kt": "K+", "K*": "K+", "Cat": "Ca2+", "Na\u207a": "Na+"}.get(txt, txt)
    txt = txt.replace("}", ")").replace("{", "(")
    _m = re.match(r"^\(\s*(\d[\d.,]*)\s+(\d[\d.,]*)\)\s*-$", txt)
    if _m:
        txt = "(%s - %s)" % (_m.group(1), _m.group(2))
    _m = re.match(r"^(.*?:)\s*(\d[\d.,]*)\s*[-\u2013]\s*(\d[\d.,]*)\)$", txt)
    if _m:
        txt = "%s (%s-%s)" % (_m.group(1), _m.group(2), _m.group(3))
    if txt.startswith("(") and re.search(r"\d\s*[1Il|!:;\]]$", txt) and ")" not in txt:
        txt = re.sub(r"\s*[1Il|!:;\]]$", ")", txt)
    elif txt.startswith("(") and ")" not in txt and re.search(r"\d$", txt) and re.search(r"[-\u2013]\s*\d", txt):
        txt += ")"
    return txt


def _ocr_grid(im, tab, scale=4):
    """Table: grid-lines mita ke tesseract; khali cells ka alag-alag retry. Return rows ya None (agar mostly khali)."""
    from PIL import ImageDraw
    x0, y0, x1, y1 = tab["vs"][0], tab["hs"][0], tab["vs"][-1], tab["hs"][-1]
    Lraw = im.convert("L").crop((x0, y0, x1 + 1, y1 + 1))
    L = Lraw.point(lambda v: 255 if v >= 150 else v)
    d = ImageDraw.Draw(L)
    for h in tab["hs"]:
        d.rectangle((0, h - y0 - 2, L.width, h - y0 + 2), fill=255)
    for v in tab["vs"]:
        d.rectangle((v - x0 - 2, 0, v - x0 + 2, L.height), fill=255)
    L = L.point(lambda v: 255 if v > 190 else v)
    words = _tess_words(_prep_crop(L, (0, 0, L.width, L.height), scale), 6, scale, x0, y0)
    hs, vs = tab["hs"], list(tab["vs"])
    if len(vs) < 3:
        # vertical lines nahi mili -- word x-gaps se columns nikalo
        if not words:
            return None
        hh = sorted(w[3] - w[1] for w in words)[len(words) // 2]
        iv = sorted((w[0], w[2]) for w in words)
        seps, cur = [], iv[0][1]
        for a, b in iv[1:]:
            if a - cur > 1.8 * hh:
                seps.append((cur + a) / 2)
            cur = max(cur, b)
        if not seps:
            return None
        vs = [vs[0]] + seps + [vs[-1]]
        tab = dict(tab, vs=vs)
    nr, nc = len(hs) - 1, len(vs) - 1
    cells = [[[] for _ in range(nc)] for _ in range(nr)]
    for w in words:
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        ri = next((i for i in range(nr) if hs[i] <= cy < hs[i + 1]), None)
        ci = next((i for i in range(nc) if vs[i] <= cx < vs[i + 1]), None)
        if ri is not None and ci is not None:
            cells[ri][ci].append(w)
    rows = [[_cell_text(ws) for ws in r] for r in cells]
    # har cell ka alag (single-cell) OCR -- chhote cells mein grid-OCR se behtar; grid result fallback
    for ri in range(nr):
        for ci in range(nc):
            bx = (max(0, vs[ci] - x0 + 3), max(0, hs[ri] - y0 + 3), min(L.width, vs[ci + 1] - x0 - 2), min(L.height, hs[ri + 1] - y0 - 2))
            if bx[2] - bx[0] < 6 or bx[3] - bx[1] < 6:
                continue
            prof = Lraw.crop(bx).point(lambda v: 255 if v < 170 else 0).resize((1, bx[3] - bx[1]), 2).tobytes()
            bands, inb = 0, False
            for v in prof:
                if v > 20 and not inb:
                    bands += 1
                inb = v > 20
            multi = bands >= 2
            ws = _tess_words(_prep_crop(Lraw, bx, 5), 6 if multi else 7, 5, x0 + bx[0], y0 + bx[1])
            t = _cell_text(ws)
            if t.count(")") > t.count("("):
                for _psm in (6, 13):
                    t2 = _cell_text(_tess_words(_prep_crop(Lraw, bx, 5), _psm, 5, x0 + bx[0], y0 + bx[1]))
                    if t2.count(")") == t2.count("(") and t2:
                        t = t2
                        break
            if re.match(r"^\([\d.,\s-]+\)$", t):
                t = re.sub(r"(\d),(\d)", r"\1.\2", t)
            if t and (not rows[ri][ci] or len(re.sub(r"\W", "", t)) >= len(re.sub(r"\W", "", rows[ri][ci])) - 1):
                rows[ri][ci] = t
    tot = nr * nc
    filled = sum(1 for r in rows for c in r if c)
    if (tot and filled < 0.5 * tot) or nc < 2:
        return None
    _ne = [c for r in rows for c in r if c]
    if _ne and sum(1 for c in _ne if re.search(r"[A-Za-z]{4,}", c)) < 0.4 * len(_ne):
        return None
    lastc = [r[-1] for r in rows if r]
    if sum(1 for c in lastc if re.match(r"^\d{1,3}\s*%$", c or "")) >= 2:
        return None                        # options box ka % column -- table nahi
    return rows


def _style_words(im, words, scale):
    """Har word ko teal-fraction (idx7) aur line-start bullet-blob flag (idx8) do."""
    from PIL import ImageChops
    rgb = im.convert("RGB")
    L = rgb.convert("L")
    r, g, b = rgb.split()
    tealm = ImageChops.multiply(ImageChops.subtract(b, r).point(lambda v: 255 if v > 26 else 0),
                                L.point(lambda v: 255 if v < 200 else 0))
    darkm = L.point(lambda v: 255 if v < 170 else 0)
    inv = 1.0 / scale
    W, H = im.size
    for w in words:
        while len(w) < 9:
            w.append(0)
        box = (max(0, int(w[0] * inv)), max(0, int(w[1] * inv)), min(W, int(w[2] * inv) + 1), min(H, int(w[3] * inv) + 1))
        if box[2] <= box[0] or box[3] <= box[1]:
            w[7] = 0
            continue
        dc = darkm.crop(box).histogram()[255]
        tc = tealm.crop(box).histogram()[255] if dc >= 6 else 0
        w[7] = round(tc / dc, 2) if dc >= 6 else 0
    groups = {}
    for w in words:
        if w[5] < 99000:
            groups.setdefault(w[5], []).append(w)
    for ws in groups.values():
        ws.sort(key=lambda w: w[0])
        w0 = ws[0]
        lh = max(1.0, max(x[3] for x in ws) - min(x[1] for x in ws)) * inv
        lh = min(lh, 40.0)
        bx0 = int(w0[0] * inv - 3.2 * lh)
        bx1 = int(w0[0] * inv - 0.4 * lh)
        by0 = int(min(x[1] for x in ws) * inv)
        by1 = int(max(x[3] for x in ws) * inv)
        if bx0 < 0 or bx1 - bx0 < 3 or by1 <= by0:
            continue
        win = darkm.crop((bx0, by0, bx1, by1))
        bb = win.getbbox()
        if bb and 0.15 * lh <= bb[2] - bb[0] <= 0.8 * lh and 0.15 * lh <= bb[3] - bb[1] <= 0.8 * lh \
                and win.histogram()[255] >= 4:
            w0[8] = 1


def _ocr_styles(im, words, scale, W, H):
    """Tables / boxes nikalo; table ke words hata do; styles lagao. Return (words, tables, boxes)."""
    tables, boxes = [], []
    try:
        data0 = {"w": W * scale, "h": H * scale, "words": words}
        cl = _pct_block(data0)
        excl = None
        if cl:
            cs = [c for c, _ in cl]
            pitch = (cs[-1] - cs[0]) / max(1, len(cs) - 1) if len(cs) > 1 else 30.0
            excl = [(int((cs[0] - 0.7 * pitch) / scale), int((cs[-1] + 0.62 * pitch) / scale))]
        grids, bxs = _find_grids(im, excl)
        for g in grids:
            rows = _ocr_grid(im, g)
            if not rows:
                continue
            flat = " ".join(" ".join(r) for r in rows)
            if len(rows) < 2 or re.search(r"Textbook|Media|Report broken|Discuss|Improve|Next question|Links", flat):
                continue
            if not any(c for r in rows for c in r):
                continue
            bb = [g["vs"][0] * scale, g["hs"][0] * scale, g["vs"][-1] * scale, g["hs"][-1] * scale]
            if (bb[3] - bb[1]) > 0.8 * H * scale or len(g["vs"]) > 9:
                continue                     # galat grid (text ko table samajh liya) -- ignore
            tables.append({"bbox": bb, "rows": rows})
        for b in bxs:
            boxes.append([b[0] * scale, b[1] * scale, b[2] * scale, b[3] * scale])
    except Exception:
        tables, boxes = [], []
    if tables:
        def _in(w):
            cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            return any(t["bbox"][0] - 3 <= cx <= t["bbox"][2] + 3 and t["bbox"][1] - 3 <= cy <= t["bbox"][3] + 3 for t in tables)
        words = [w for w in words if not _in(w)]
    try:
        _style_words(im, words, scale)
    except Exception:
        pass
    return words, tables, boxes


def _ocr_one_page(pdf, n, d):
    """n = 1-based. Cache JSON: {w,h,words,bars}"""
    jp = _os.path.join(d, "p%04d.json" % n)
    if _os.path.exists(jp):
        try:
            return _json.load(_bopen(jp))
        except Exception:
            pass
    from PIL import Image
    prefix = _os.path.join(d, "tmp%04d" % n)
    subprocess.run(["pdftoppm", "-r", str(OCR_DPI), "-f", str(n), "-l", str(n), "-png", pdf, prefix],
                   capture_output=True)
    pngs = [f for f in _os.listdir(d) if f.startswith("tmp%04d" % n) and f.endswith(".png")]
    if not pngs:
        return {"w": 595, "h": 842, "words": [], "bars": [], "tables": [], "boxes": []}
    png = _os.path.join(d, pngs[0])
    scale = 72.0 / OCR_DPI
    env = dict(_os.environ, OMP_THREAD_LIMIT="1")
    tsv = subprocess.run(["tesseract", png, "stdout", "-l", "eng", "--psm", "4", "tsv"],
                         capture_output=True, text=True, errors="replace", env=env).stdout
    im = Image.open(png)
    W, H = im.size
    bars = _green_bars(im, scale)
    words = _parse_tsv(tsv, scale)
    # option ke right edge pe akeli "14" jaisi digits (% OCR se gir gaya) => "14%"
    _byl = {}
    for w in words:
        _byl.setdefault(w[5], []).append(w)
    for _ws in _byl.values():
        _ws.sort(key=lambda w: w[0])
        for _i, w in enumerate(_ws):
            if _i == len(_ws) - 1 and _i > 0 and re.match(r"^\d{1,3}$", w[4]) and int(w[4]) <= 100 \
                    and w[0] > W * scale * 0.72 and w[0] - _ws[_i - 1][2] > 30 * (OCR_DPI / 130.0):
                w[4] += "%"
    # chhoti/halki grey percentages ("1%", "5%") main OCR mein miss ho jati hain -- right strip alag se padho
    try:
        words += _pct_strip_words(im, png, scale, words, env)
    except Exception:
        pass
    try:
        words += _recover_rows(im, png, scale, words, env)
    except Exception:
        pass
    try:
        _refine_pcts(im, words, scale, env)
    except Exception:
        pass
    words, tables, boxes = _ocr_styles(im, words, scale, W, H)
    try:
        im.close(); _os.remove(png)
    except Exception:
        pass
    data = {"w": W * scale, "h": H * scale, "words": words, "bars": bars, "tables": tables, "boxes": boxes}
    with _bopen(jp + ".tmp", "w") as _f:
        _json.dump(data, _f)
    _os.replace(jp + ".tmp", jp)
    return data


def ocr_all_pages(pdf, progress=True, workers=None):
    n = pdf_num_pages(pdf)
    d = _ocr_dir(pdf)
    todo = [i for i in range(1, n + 1) if not _os.path.exists(_os.path.join(d, "p%04d.json" % i))]
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        workers = workers or max(1, min(4, (_os.cpu_count() or 2)))
        done = 0
        with ThreadPoolExecutor(workers) as ex:
            for _ in ex.map(lambda i: _ocr_one_page(pdf, i, d), todo):
                done += 1
                if progress and (done % 10 == 0 or done == len(todo)):
                    print("      OCR: %d/%d pages (baaki cache mein)" % (done, len(todo)), flush=True)
    return n, d



_NOISE_RES = [re.compile(p, re.I) for p in (
    r"pass\s*m[a-z]{0,3}d?i?cine", r"^\W*\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2}", r"passmedicine\.com|questions\.php|^https?\W",
    r"^\W*\d\s*/\s*\d\W*$")]


def _is_page_noise(t):
    t = t.strip()
    if not t:
        return True
    toks = t.split()
    if len(toks) >= 6 and sum(len(x) for x in toks) / len(toks) <= 2.6 and len(re.findall(r"[|\\/@)(]", t)) >= 3:
        return True      # editor toolbar ("B I A T! ...") ka OCR kachra
    if len(toks) >= 6:
        sh = [x for x in toks if len(x) <= 3 and not re.match(r"^(a|an|the|of|in|on|to|is|are|or|and|for|by|at|as|it|be|no|not|but|may|can|has|if|do|so|we|he|she|you|his|her|its|age|dm|bp|hr|rr|ct|mri|ecg|eeg|gp|ace|ssri|mmse)\W*$", x, re.I)]
        if len(sh) / len(toks) >= 0.7 and len(re.findall(r"[^A-Za-z0-9\s]", t)) >= 3:
            return True
    return any(r.search(t) for r in _NOISE_RES)


def _page_lines(data):
    """OCR words -> lines: [{x0,y0,x1,y1,text,size,bullet,words}] (top-to-bottom)."""
    groups = {}
    for w in data["words"]:
        groups.setdefault(w[5], []).append(w)
    lines = []
    # OCR ne kai option rows ek line mein jod di ho ("A 3% I B 86% C 6% ...") -- har % pe todo
    _pe = set()
    _g2 = {}
    for k, ws in groups.items():
        ws.sort(key=lambda w: w[0])
        pidx = [i for i, w in enumerate(ws) if re.match(r"^\(?\d{1,3}\s*%\)?$", w[4]) and i < len(ws) - 1
                and re.match(r"^[A-Za-z|(\[]", ws[i + 1][4])]
        allp = [i for i, w in enumerate(ws) if re.match(r"^\d{1,3}%$", w[4])]
        if len(allp) >= 2 and pidx:
            cuts = set(i + 1 for i in pidx)
            for i, w in enumerate(ws[:allp[0]]):
                if w[4].endswith("?") and i + 1 < len(ws):
                    cuts.add(i + 1)
                    break
            seg, n = [], 0
            for i, w in enumerate(ws):
                if i in cuts and seg:
                    _g2[(k, n)] = seg; n += 1; seg = []
                seg.append(w)
            _g2[(k, n)] = seg
            for kk, sg in list(_g2.items()):
                if kk[0] == k:
                    if kk[1] > 0 and sg and sg[0][4] in ("I", "l", "|", "1", "!") and len(sg) > 1:
                        sg.pop(0)
                    if re.match(r"^\d{1,3}%$", sg[-1][4]):
                        _pe.add(kk)
        else:
            _g2[(k, 0)] = ws
    groups = _g2
    _x0s = sorted(min(w[0] for w in ws) for ws in groups.values() if ws)
    _Mx = _x0s[len(_x0s) // 8] if _x0s else 0
    for k, ws in groups.items():
        ws.sort(key=lambda w: w[0])
        bullet = False
        # leading green/red bar ya bullet glyph hatao
        while ws and ((ws[0][4].strip("|[]!_") == "" and len(ws[0][4]) <= 2) or ws[0][4] in ("|", "||")):
            ws = ws[1:]
        if len(ws) >= 2:
            w0 = ws[0]
            hh = max(1.0, w0[3] - w0[1])
            if (w0[4] in _BULLET_CHARS or (len(w0[4]) == 1 and not w0[4].isalnum())
                    or (len(w0[4]) <= 2 and not re.match(r"^(I|A|a|\d\.?|a\)|I\))$", w0[4]) and (ws[1][0] - w0[2]) > 0.8 * hh
                        and not re.search(r"[a-z]{2}", w0[4]))) \
                    and (w0[2] - w0[0]) < 1.3 * hh and (ws[1][0] - w0[2]) > 0.25 * hh:
                bullet = True
                bw = w0
                ws = ws[1:]
        if not bullet and ws and len(ws[0]) > 8 and ws[0][8] == 1 and ws[0][7] < 0.5 and ws[0][0] - _Mx < 0.1 * data.get("w", 595):
            bullet = True
            bw = (ws[0][0] - 6, ws[0][1], ws[0][0] - 1, ws[0][3])
        if not ws:
            continue
        if len(ws) > 1 and ws[-1][4] == "%" and not re.search(r"\d$", ws[-2][4]):
            ws = ws[:-1]                         # heading ke aakhir ka star icon "%" padha gaya
        _tf = [(w[7] if len(w) > 7 else 0) for w in ws]
        teal = bool(ws) and sum(1 for t in _tf if t >= 0.5) >= max(1, 0.6 * len(ws))
        if teal:
            while len(ws) > 1 and (ws[-1][7] if len(ws[-1]) > 7 else 0) < 0.3:
                ws = ws[:-1]                     # heading ke baad ka star / icon (teal nahi)
        ws = _fix_qheader(ws)
        txt = " ".join(w[4] for w in ws)
        qm = re.search(r"Question\s+\d+\s+of\s+\d+", txt)
        if qm:                                   # nav-icons ka kachra hatao ("Vv Pp (>)")
            keep, acc = [], ""
            for w in ws:
                keep.append(w)
                acc = " ".join(x[4] for x in keep)
                if re.search(r"Question\s+\d+\s+of\s+\d+$", acc):
                    break
            ws = [w for w in keep if True]
            while ws and not re.match(r"^(Question|\d+|of)$", ws[0][4]) and len(ws) > 3:
                ws = ws[1:]
            txt = " ".join(w[4] for w in ws)
        elif (_improve_idx(txt.split()) is not None or (re.search(r"Discuss", txt) and re.search(r"Improve", txt, re.I))) and len(txt.split()) <= 10:
            pass                                  # Discuss | Improve button-row -- boundary marker, noise nahi
        elif _is_page_noise(txt):
            continue
        elif re.match(r"^\d{1,3}$", txt.strip()) and min(w[0] for w in ws) > data.get("w", 595) * 0.7:
            continue                              # akeli number right edge pe (page/strip kachra)
        elif re.match(r"^(\d{1,2}\s+)?(Vv|X|V|v|T|TV|H|uil|emon|emion|enon|emin|oo|om)(\s+(Vv|X|V|v|uil))?$", txt.strip()):
            continue                              # media rating column / icon kachra
        elif len(txt.split()) >= 2 and all(len(t) <= 3 for t in txt.split()) and re.search(r"[~+@=|\\]", txt) \
                and not re.search(r"[a-z]{3}", txt):
            continue                              # icon-row kachra ("B+ Qo", "v Tiv H~ M e")
        x0 = min(w[0] for w in ws); y0 = min(w[1] for w in ws)
        x1 = max(w[2] for w in ws); y1 = max(w[3] for w in ws)
        rawh = y1 - y0
        if rawh >= 14.5:                         # bade heading ke aakhir ke icon (star etc) hatao
            while len(ws) > 1 and not re.search(r"[A-Za-z0-9]", ws[-1][4]):
                ws = ws[:-1]
            txt = " ".join(w[4] for w in ws)
            x1 = max(w[2] for w in ws)
        while len(ws) > 1 and len(ws[-1][4]) <= 2 and not re.search(r"[A-Za-z0-9)\]%.,]", ws[-1][4]):
            ws = ws[:-1]                         # trailing icon (star etc) ka kachra
        # trailing toolbar kachra ("... insomnia @-%-Quz'i B I & A~ E =y Tiv H- @ o")
        _k = len(ws)
        while _k > 1 and len(ws[_k-1][4]) <= 4 and not re.match(r"^[a-z]{2,4}[.,]?$|^\d{1,3}%?$|^[A-Z][a-z]{1,3}$", ws[_k-1][4]):
            _k -= 1
        if len(ws) - _k >= 4 and _k >= 2 and sum(1 for w in ws[_k:] if re.search(r"[^A-Za-z0-9]", w[4])) >= 2:
            ws = ws[:_k]
        txt = " ".join(w[4] for w in ws)
        x1 = max(w[2] for w in ws)
        pad = 0.12 * rawh                        # fitz/poppler jaisa line-box (leading ke saath) -- wrap detection ke liye
        ln = {"x0": x0, "y0": y0 - pad, "x1": x1, "y1": y1 + pad, "text": txt,
              "size": rawh, "bullet": bullet, "words": ws, "pctend": k in _pe, "teal": teal}
        if bullet:
            ln["bx"] = (bw[0], bw[1], bw[2], bw[3])
        lines.append(ln)
    lines.sort(key=lambda l: (round(l["y0"] / 3), l["x0"]))
    # ek hi visual line jo OCR ne do tukdon mein baanti ho ("... is" / "around 90% with ...") -- jod do
    mg = []
    for l in lines:
        if mg:
            p = mg[-1]
            hmin = max(1.0, min(p["y1"] - p["y0"], l["y1"] - l["y0"]))
            if (not p.get("pctend") and not l.get("pctend") and not l["bullet"] and p["words"] and l["words"]
                    and abs((p["y0"] + p["y1"]) / 2 - (l["y0"] + l["y1"]) / 2) < 0.3 * hmin
                    and 0 <= l["x0"] - p["x1"] < 0.1 * data.get("w", 595) and p["x0"] < l["x0"]
                    and not p.get("teal") and not l.get("teal")):
                p["words"] = p["words"] + l["words"]
                p["text"] = p["text"] + " " + l["text"]
                p["x1"] = max(p["x1"], l["x1"])
                p["y0"] = min(p["y0"], l["y0"]); p["y1"] = max(p["y1"], l["y1"])
                p["size"] = max(p["size"], l["size"])
                continue
        mg.append(l)
    lines = mg
    # content column ke bahar (right) wale chhote icon-garbage tokens ("emon", "enon") hatao
    _long = sorted(l["x1"] for l in lines if len(l["text"]) >= 50 and not l.get("pctend"))
    if len(_long) >= 4:
        Rm = _long[int(len(_long) * 0.85)]
        lim = Rm + 0.012 * data.get("w", 595)
        for l in lines:
            ws2 = l["words"]
            if l.get("pctend") or len(ws2) < 3:
                continue
            n0 = len(ws2)
            while len(ws2) > 2 and ws2[-1][0] > lim and len(ws2[-1][4]) <= 6 and not re.match(r"^[\d%.,)]+$", ws2[-1][4]):
                ws2 = ws2[:-1]
            while len(ws2) > 2 and re.match(r"^(emon|enon|emin|emion|emen|eman|oo|om)$", ws2[-1][4]) and ws2[-1][0] > data.get("w", 595) * 0.6:
                ws2 = ws2[:-1]
            if len(ws2) != n0:
                l["words"] = ws2
                l["text"] = " ".join(w[4] for w in ws2)
                l["x1"] = max(w[2] for w in ws2)
    return lines


def _fix_qheader(ws):
    """OCR ne 'Question 108 of 258' bigaad diya ho ('Quest1on 1O8 of 258') to words ko theek karo (in-place copy)."""
    import difflib
    tk = [w[4] for w in ws]
    # "Question 1080f258" / "Question 1120258/" -- number, 'of', total ek token mein chipke
    for i in range(len(tk) - 1):
        if re.match(r"^[A-Za-z0-9]{6,9}$", tk[i]) and difflib.SequenceMatcher(None, tk[i].lower(), "question").ratio() >= 0.72:
            m = None
            for k in (1, 2, 3):
                mm = re.match(r"^(?:o\s*f|0f|of|0|o)(\d{2,4})\W{0,2}$", tk[i + 1][k:], re.I)
                if tk[i + 1][:k].isdigit() and mm and not mm.group(1).startswith("0"):
                    m = (tk[i + 1][:k], mm.group(1))
                    break
            if m and 1 <= int(m[0]) <= int(m[1]) <= 9999:
                out = list(ws)
                w = list(out[i]); w[4] = "Question"; out[i] = tuple(w) if isinstance(out[i], tuple) else w
                w = list(out[i + 1]); w[4] = "%s of %s" % (m[0], m[1]); out[i + 1] = tuple(w) if isinstance(out[i + 1], tuple) else w
                return out
    dm = {"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "!": "1", "S": "5", "B": "8"}
    num = lambda t: re.match(r"^[0-9OolI|!SB]{1,4}$", t) and re.search(r"\d|[OolI|!]", t)
    for i in range(len(tk) - 3):
        if not re.match(r"^[A-Za-z0-9]{6,9}$", tk[i]) or difflib.SequenceMatcher(None, tk[i].lower(), "question").ratio() < 0.72:
            continue
        if not num(tk[i + 1]) or not re.match(r"^[o0O][fF]$", tk[i + 2]) or not num(tk[i + 3]):
            continue
        a = "".join(dm.get(c, c) for c in tk[i + 1])
        b = "".join(dm.get(c, c) for c in tk[i + 3])
        if not (a.isdigit() and b.isdigit()) or not (1 <= int(a) <= int(b) <= 9999):
            continue
        out = list(ws)
        for j, t in ((i, "Question"), (i + 1, a), (i + 2, "of"), (i + 3, b)):
            w = list(out[j]); w[4] = t; out[j] = tuple(w) if isinstance(out[j], tuple) else w
        return out
    return ws


def _pct_block(data):
    """Right-column percentages ka vertical cluster (= option rows). Geometry se options pakdo, text-heuristics se nahi."""
    W = data["w"]
    sc = W / 595.0
    P = [w for w in data["words"] if re.match(r"^\d{1,3}%$", w[4]) and w[0] > W * 0.62 and int(w[4][:-1]) <= 100]
    P.sort(key=lambda w: ((w[1] + w[3]) / 2, w[5] >= 99000))
    clusters, cur = [], []
    for w in P:
        c = (w[1] + w[3]) / 2
        if cur and abs(c - cur[-1][0]) < 4 * sc:
            continue                                   # same row ka duplicate (main + strip)
        if cur and c - cur[-1][0] > 85 * sc:
            clusters.append(cur); cur = []
        cur.append((c, w))
    if cur:
        clusters.append(cur)
    clusters = [c for c in clusters if len(c) >= 3]
    if not clusters:
        return None
    cl = max(clusters, key=len)
    val = lambda e: int(e[1][4][:-1])
    if len(cl) >= 4:
        x1s = sorted(e[1][2] for e in cl)
        med = x1s[len(x1s) // 2]
        keep = [e for e in cl if abs(e[1][2] - med) <= 12 * sc]
        if len(keep) >= 3:
            cl = keep
    while len(cl) > 3 and sum(val(e) for e in cl) >= 102:
        s = sum(val(e) for e in cl)
        opts = [(abs(s - val(cl[0]) - 100), 0), (abs(s - val(cl[-1]) - 100), 1)]
        opts.sort()
        ok = False
        for _, which in opts:
            ns = s - val(cl[0] if which == 0 else cl[-1])
            if 97 <= ns <= 101:
                cl = cl[1:] if which == 0 else cl[:-1]
                ok = True
                break
        if not ok:
            break
    while len(cl) > 3 and sum(val(e) for e in cl) > 104:
        s = sum(val(e) for e in cl)
        a, b = s - val(cl[0]), s - val(cl[-1])
        cl = cl[1:] if abs(a - 100) <= abs(b - 100) else cl[:-1]
    return cl


_QH = {}


def ocr_page_qheader(pdf, page):
    """Page par 'Question N of M' ho to N, warna None (layout text cache hota hai)."""
    if pdf not in _QH:
        _QH[pdf] = ocr_layout_text(pdf, False).split("\f")
    pages = _QH[pdf]
    if page - 1 >= len(pages):
        return None
    m = re.search(r"Question\s+(\d+)\s+of\s+\d+", pages[page - 1][:600])
    return int(m.group(1)) if m else None


def ocr_find_figures(pdf, page, dpi_out=200):
    """Image-only PDF ke page mein diagrams / photos / ECG (text ke bahar ke bade graphics). Return [(x0,y0,x1,y1, ypos_frac)] 130dpi pixels."""
    from PIL import Image, ImageDraw, ImageFilter
    import tempfile
    n, d = ocr_all_pages(pdf, False)
    data = _ocr_one_page(pdf, page, d)
    scale = data.get("scale", None)
    tmp = tempfile.mkdtemp()
    base = _os.path.join(tmp, "pg")
    subprocess.run(["pdftoppm", "-r", str(OCR_DPI), "-f", str(page), "-l", str(page), "-png", pdf, base],
                   capture_output=True)
    pngs = [f for f in _os.listdir(tmp) if f.endswith(".png")]
    if not pngs:
        return []
    im = Image.open(_os.path.join(tmp, pngs[0])).convert("RGB")
    W, H = im.size
    k = W / float(data["w"]) if data.get("w") else 1.0          # OCR coord -> pixel
    L = im.convert("L")
    m = L.point(lambda v: 255 if v < 225 else 0)
    dr = ImageDraw.Draw(m)
    for w in data["words"]:
        if w[5] >= 99000:
            continue
        dr.rectangle((w[0] * k - 4, w[1] * k - 3, w[2] * k + 4, w[3] * k + 3), fill=0)
    for t in data.get("tables") or []:
        b = t["bbox"]
        dr.rectangle((b[0] * k - 6, b[1] * k - 6, b[2] * k + 6, b[3] * k + 6), fill=0)
    for b in data.get("boxes") or []:
        dr.rectangle((b[0] * k - 6, b[1] * k - 6, b[2] * k + 6, b[3] * k + 6), fill=0)
    cl = _pct_block(data)
    if cl:
        cs = [c * k for c, _ in cl]
        pitch = (cs[-1] - cs[0]) / max(1, len(cs) - 1) if len(cs) > 1 else 40.0
        dr.rectangle((0, cs[0] - 0.8 * pitch, W, cs[-1] + 1.8 * pitch), fill=0)
    dr.rectangle((0, 0, W, H * 0.05), fill=0)
    dr.rectangle((0, H * 0.955, W, H), fill=0)
    dr.rectangle((0, 0, W * 0.08, H), fill=0)
    small = m.resize((max(1, W // 4), max(1, H // 4)), 2).point(lambda v: 255 if v > 25 else 0)
    small = small.filter(ImageFilter.MaxFilter(7))
    sw, sh = small.size
    px = bytearray(small.tobytes())
    seen = bytearray(sw * sh)
    comps = []
    for i0 in range(sw * sh):
        if px[i0] and not seen[i0]:
            st = [i0]
            seen[i0] = 1
            x0 = y0 = 10 ** 9
            x1 = y1 = -1
            while st:
                i = st.pop()
                y, x = divmod(i, sw)
                if x < x0: x0 = x
                if x > x1: x1 = x
                if y < y0: y0 = y
                if y > y1: y1 = y
                for j in (i - 1, i + 1, i - sw, i + sw):
                    if 0 <= j < sw * sh and px[j] and not seen[j] and not (abs((j % sw) - x) > 1):
                        seen[j] = 1
                        st.append(j)
            comps.append((x0 * 4, y0 * 4, (x1 + 1) * 4, (y1 + 1) * 4))
    out = []
    for (a, b, c, e) in comps:
        w_, h_ = c - a, e - b
        if w_ < 0.18 * W or h_ < 0.08 * H:
            continue
        ink = m.crop((a, b, c, e)).histogram()[255] / float(max(1, w_ * h_))
        if ink < 0.07:
            continue
        out.append({"box": (max(0, a - 6), max(0, b - 6), min(W, c + 6), min(H, e + 6)), "cl_top": (cs[0] - 0.8 * pitch) if cl else None})
    try:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass
    return out


def ocr_crop_region(pdf, page, box, out_path, dpi_out=200):
    """box = 130dpi pixel bbox; page ko dpi_out par render karke crop save karo."""
    from PIL import Image
    import tempfile
    tmp = tempfile.mkdtemp()
    base = _os.path.join(tmp, "pg")
    subprocess.run(["pdftoppm", "-r", str(dpi_out), "-f", str(page), "-l", str(page), "-png", pdf, base], capture_output=True)
    pngs = [f for f in _os.listdir(tmp) if f.endswith(".png")]
    if not pngs:
        return False
    im = Image.open(_os.path.join(tmp, pngs[0])).convert("RGB")
    f = dpi_out / float(OCR_DPI)
    im.crop(tuple(int(v * f) for v in box[:4])).save(out_path)
    import shutil as _sh
    _sh.rmtree(tmp, ignore_errors=True)
    return True


def ocr_expected_options(pdf, p0, p1):
    """Geometry se: options-box ke percentage rows. Return (count, [pct..]) ya None."""
    try:
        n, d = ocr_all_pages(pdf, False)
        for p in range(max(1, p0), min(p1, n) + 1):
            cl = _pct_block(_ocr_one_page(pdf, p, d))
            if cl:
                return len(cl), [int(e[1][4].rstrip("%")) for e in cl]
    except Exception:
        pass
    return None


def _clean_data(data):
    """(data, cluster): cluster ke bahar ke strip-%-words (junk) hata do."""
    cl = _pct_block(data)
    if not cl:
        return dict(data, words=[w for w in data["words"] if w[5] < 99000]), None
    keep = set(id(e[1]) for e in cl)
    words = [w for w in data["words"] if w[5] < 99000 or id(w) in keep]
    return dict(data, words=words), cl


def _opt_rows(data, cl):
    """lines with option rows: har option ek line 'text  NN%', uske aage-peeche blank (optrow flag)."""
    W = data["w"]
    cs = [c for c, _ in cl]
    drop = set(id(e[1]) for e in cl)
    d2 = dict(data, words=[w for w in data["words"] if id(w) not in drop])
    lines = _page_lines(d2)
    d1 = (cs[1] - cs[0]) if len(cs) > 1 else 30.0
    d2_ = (cs[-1] - cs[-2]) if len(cs) > 1 else 30.0
    top, bot = cs[0] - d1 * 0.5, cs[-1] + d2_ * 0.5
    mids = [(cs[i] + cs[i + 1]) / 2 for i in range(len(cs) - 1)]
    block, rest = [], []
    _bx = data.get("boxes") or []
    for l in lines:
        cy = (l["y0"] + l["y1"]) / 2
        _inb = any(b[1] - 2 <= cy <= b[3] + 2 and l["x0"] >= b[0] - 6 for b in _bx)
        (block if (top <= cy <= bot and not _inb) else rest).append(l)
    parts = [[] for _ in cs]
    for l in block:
        cy = (l["y0"] + l["y1"]) / 2
        i = sum(1 for m in mids if cy > m)
        parts[i].append(l)
    rows = []
    for i, ls in enumerate(parts):
        if not ls:
            continue
        ls.sort(key=lambda l: l["y0"])
        subs, cur = [], [ls[0]]
        for l in ls[1:]:
            pv = cur[-1]
            if l["y0"] - pv["y1"] > 0.8 * max(1.0, pv["size"]):
                subs.append(cur); cur = []
            cur.append(l)
        subs.append(cur)
        cen = [sum((l["y0"] + l["y1"]) / 2 for l in s) / len(s) for s in subs]
        best = min(range(len(subs)), key=lambda k: abs(cen[k] - cs[i]))
        for k, s in enumerate(subs):
            pct = cl[i][1][4] if k == best else "0%"
            txt = " ".join(l["text"] for l in s)
            txt = re.sub(r"^[Il|!1]\s+(?=[A-Z(])", "", txt).strip() + "  " + pct
            rows.append({"x0": min(l["x0"] for l in s), "y0": min(l["y0"] for l in s),
                         "x1": W * 0.9, "y1": max(l["y1"] for l in s), "text": txt,
                         "size": s[0]["size"], "bullet": False, "words": s[0]["words"],
                         "pctend": True, "optrow": True})
    out = rest + rows
    out.sort(key=lambda l: (round(l["y0"] / 3), l["x0"]))
    return out



def _ocr_tables(data):
    """OCR word-geometry se tables: gap-based cells + column alignment. Returns list of rows (list of list of str)."""
    cl = _pct_block(data)
    ex = set(id(e[1]) for e in cl) if cl else set()
    words = [w for w in data["words"] if id(w) not in ex and w[4].strip()]
    if len(words) < 8:
        return []
    hs = sorted(w[3] - w[1] for w in words)
    H = max(5.0, hs[len(hs) // 2])
    words.sort(key=lambda w: (w[1] + w[3]) / 2)
    vr = []
    for w in words:
        yc = (w[1] + w[3]) / 2
        if vr and abs(yc - vr[-1]["yc"]) < 0.5 * H:
            vr[-1]["w"].append(w)
            vr[-1]["yc"] = sum((x[1] + x[3]) / 2 for x in vr[-1]["w"]) / len(vr[-1]["w"])
        else:
            vr.append({"yc": yc, "w": [w]})
    gapT = max(10.0, 1.8 * H)
    for r in vr:
        ws = sorted(r["w"], key=lambda w: w[0])
        while ws and len(ws[0][4]) <= 2 and not re.search(r"[A-Za-z0-9]", ws[0][4]):
            ws = ws[1:]
        cells, cur = [], []
        for w in ws:
            if cur and w[0] - cur[-1][2] > gapT:
                cells.append(cur); cur = []
            cur.append(w)
        if cur:
            cells.append(cur)
        r["cells"] = [(c[0][0], c[-1][2], " ".join(x[4] for x in c)) for c in cells]
    out = []
    i = 0
    while i < len(vr):
        if len(vr[i]["cells"]) < 2:
            i += 1
            continue
        cols = [c[0] for c in vr[i]["cells"]]
        run = [i]
        k = i + 1
        while k < len(vr):
            if vr[k]["yc"] - vr[k - 1]["yc"] > 5.0 * H or not vr[k]["cells"]:
                break
            ok = True
            for c in vr[k]["cells"]:
                if not any(abs(c[0] - cx) <= 9 for cx in cols):
                    ok = False
                    break
            if not ok:
                break
            run.append(k)
            for c in vr[k]["cells"]:
                if not any(abs(c[0] - cx) <= 9 for cx in cols):
                    cols.append(c[0])
            k += 1
        multi = sum(1 for j in run if len(vr[j]["cells"]) >= 2)
        if multi >= 3 or (multi >= 2 and len(run) >= 3):
            cols = sorted(cols)
            merged = []
            for ci, j in enumerate(run):
                row = [""] * len(cols)
                for c in vr[j]["cells"]:
                    idx = min(range(len(cols)), key=lambda q: abs(cols[q] - c[0]))
                    row[idx] = (row[idx] + " " + c[2]).strip()
                if merged and vr[j]["yc"] - vr[run[ci - 1]]["yc"] <= 1.75 * H:
                    for q, t in enumerate(row):
                        if t:
                            merged[-1][q] = (merged[-1][q] + " " + t).strip()
                else:
                    merged.append(row)
            if len(merged) >= 2:
                out.append(merged)
        i = k if k > i + 1 else i + 1
    return out



def _improve_idx(tk):
    """'Discuss (n) | Improve' button-row ka OCR (garble ke saath) -- 'Improve' token ka index ya None."""
    ii = None
    tk = [re.sub(r"[^\w\[(@|]+$", "", t) or t for t in tk]
    for ti, t in enumerate(tk[:8]):
        if re.match(r"^[\[(](re|ro)[a-z]{0,5}en$", t, re.I) or t == "emin":
            ii = ti
            break
        if t.lower() == "improve" and (len(tk) <= 4 or re.search(r"Discuss|\(e|^[|@\[(]", " ".join(tk[:ti])) or (ti == 0 and len(tk) <= 6)):
            ii = ti
            break
    if ii is None and len(tk) >= 2 and re.match(r"^[\[(][a-z@]{0,2}$", tk[0]) and re.match(r"^[a-z]{3,6}$", tk[1]) \
            and tk[1] not in ("then", "with", "that", "this", "from"):
        ii = 1
    if ii is not None and ii <= 7 and (tk[ii].lower() == "improve" or ii == 0 or re.search(r"Discuss|\(e|^[|@\[(]", " ".join(tk[:ii]))):
        return ii
    return None


OCR_TABLES = {}          # marker -> rows (ya None = pichli table mein merge ho gayi)
_MERGED = {}


def _merged_tables(pdf):
    """{(page,k): rows|None}: page-break pe tooti table ko jod do (repeat header hata ke)."""
    if pdf in _MERGED:
        return _MERGED[pdf]
    import difflib
    n, d = ocr_all_pages(pdf, False)
    res = {}
    last = None       # (key, bbox_y1, H, ncols, page)
    for p in range(1, n + 1):
        data = _ocr_one_page(pdf, p, d)
        tl = data.get("tables") or []
        H = data.get("h", 842)
        for k, t in enumerate(tl):
            res[(p, k)] = [list(r) for r in t["rows"]]
        if tl and last is not None and last[4] == p - 1:
            t0 = tl[0]
            nc0 = max(len(r) for r in t0["rows"])
            if t0["bbox"][1] <= 0.22 * H and last[1] >= 0.78 * last[2] and nc0 == last[3]:
                tgt = res[last[0]]
                rows0 = res[(p, 0)]
                norm = lambda r: re.sub(r"[^a-z0-9]", "", " ".join(r).lower())
                if tgt and rows0 and difflib.SequenceMatcher(None, norm(tgt[0]), norm(rows0[0])).ratio() >= 0.85:
                    rows0 = rows0[1:]
                tgt.extend(rows0)
                res[(p, 0)] = None
                if len(tl) == 1:
                    last = (last[0], t0["bbox"][3], H, last[3], p)
                    continue
        if tl:
            kk = len(tl) - 1
            if res.get((p, kk)) is not None:
                last = ((p, kk), tl[kk]["bbox"][3], H, max(len(r) for r in tl[kk]["rows"]), p)
    _MERGED[pdf] = res
    return res


def table_rows_md(rows):
    if not rows:
        return ""
    nc = max(len(r) for r in rows)
    rows = [list(r) + [""] * (nc - len(r)) for r in rows]
    esc = lambda c: (c or "").replace("|", "\\|")
    md = ["| " + " | ".join(esc(c) for c in rows[0]) + " |", "| " + " | ".join("---" for _ in rows[0]) + " |"]
    for r in rows[1:]:
        md.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(md)


def _synth_headers(pages, optflags, nextflags):
    """Kuch questions ke pehle page par 'Question N of M' header OCR/print mein hota hi nahi. Pattern: pichle question ka
    'Next question' guzar chuka, aur ab options-cluster wala naya page bina header ke aaya => naya question. Number sirf tab
    lagate hain jab do asli headers ke beech ke missing numbers aur aise starts ki ginti barabar ho."""
    hrx = re.compile(r"Question\s+(\d+)\s+of\s+(\d+)")
    hdr, tots = [], {}
    for t in pages:
        m = hrx.search(t[:700])
        hdr.append(int(m.group(1)) if m else None)
        if m:
            tots[m.group(2)] = tots.get(m.group(2), 0) + 1
    if not tots:
        return pages
    M = int(max(tots, key=tots.get))
    first = []
    for t in pages:
        fl = next((l.strip() for l in t.split("\n") if l.strip()), "")
        first.append(re.sub(r"\W+", "", fl.lower())[:25])
    seen = False
    seq = []
    for i, h in enumerate(hdr):
        if h is not None:
            seen = nextflags[i]
            seq.append(("H", h, i))
            continue
        if optflags[i] and seen:
            st = i
            if i > 0 and hdr[i - 1] is None and first[i - 1] and first[i - 1] == first[i] and not optflags[i - 1] \
                    and not (seq and seq[-1][0] == "C" and seq[-1][2] == i - 1):
                st = i - 1
            seq.append(("C", None, st))
            seen = False
        if nextflags[i]:
            seen = True
    last, pend, assign = None, [], []
    for k, n, i in seq:
        if k == "H":
            if n != last:
                if pend and last is not None and n - last - 1 == len(pend):
                    assign += [(last + 1 + j, pp) for j, pp in enumerate(pend)]
                pend = []
                last = n
        else:
            pend.append(i)
    out = list(pages)
    for num, i in assign:
        out[i] = " " * 34 + "Question %d of %d\n\n" % (num, M) + out[i]
    return out


def ocr_layout_text(pdf, progress=True):
    """pdftotext -layout jaisa text (page = \\f se alag), indent = x position."""
    n, d = ocr_all_pages(pdf, progress)
    pages = []
    _optf, _nxtf = [], []
    for i in range(1, n + 1):
        data = _ocr_one_page(pdf, i, d)
        data, _cl = _clean_data(data)
        lines = _opt_rows(data, _cl) if _cl else _page_lines(data)
        _optf.append(bool(_cl) or sum(1 for l in lines if re.search(r"\d{1,3}\s*%$", l["text"])) >= 3)
        _nxtf.append(any(_is_next_q(l["text"].strip()) or _is_improve_row(l["text"]) for l in lines))
        try:
            _mt = _merged_tables(pdf)
            for _k, _t in enumerate(data.get("tables") or []):
                _mk = "@@OTBL_%d_%d@@" % (i, _k)
                OCR_TABLES[_mk] = _mt.get((i, _k))
                lines.append({"x0": _t["bbox"][0], "y0": _t["bbox"][1], "x1": _t["bbox"][2], "y1": _t["bbox"][3],
                              "text": _mk, "size": 10.0, "bullet": False, "words": [], "pctend": False})
            lines.sort(key=lambda l: (round(l["y0"] / 3), l["x0"]))
        except Exception:
            pass
        out = []
        prev = None
        pw = data.get("w", 595)
        hs = sorted(l["y1"] - l["y0"] for l in lines) or [10]
        med = hs[len(hs) // 2]
        for l in lines:
            if prev is not None:
                gap = l["y0"] - prev["y1"]
                if gap > 0.9 * med:
                    out.append("")
                if gap > 2.2 * med:
                    out.append("")
                if out and out[-1] != "" and (l.get("optrow") or prev.get("optrow")):
                    out.append("")
                elif gap <= 0.9 * med and out and out[-1] != "" and prev.get("pctend"):
                    out.append("")
                elif gap <= 0.9 * med and out and out[-1] != "":
                    # option-row: pichli line % pe khatam => nayi option; stem ('?', ':') ke baad % wali line => options shuru
                    if re.search(r"\d{1,3}\s*%$", prev["text"]) and prev["x1"] > pw * 0.7 \
                            and not re.match(r"^\s*[a-z]", l["text"]):
                        out.append("")
                    elif re.search(r"[?:.]$", prev["text"]) and re.search(r"\s\d{1,3}\s*%$", l["text"]) \
                            and l["x1"] > pw * 0.7:
                        out.append("")
            ind = int(l["x0"] / 5.0)
            _tk = l["text"].split()
            _ii = _improve_idx(_tk)
            if _ii is not None and not l.get("optrow"):
                if out and out[-1] != "":
                    out.append("")
                out.append(" " * 70 + "Next question")
                out.append("")
                _rest = " ".join(_tk[_ii + 1:])
                if _rest:
                    out.append(" " * ind + _rest)
                prev = l
                continue
            out.append(" " * ind + l["text"])
            # "Next question" button halka-blue hota hai, OCR miss kar deta hai -- widgets se reconstruct karo
            if re.search(r"Discuss\s*\(?\d+\)?", l["text"]) and re.search(r"Improve", l["text"], re.I):
                out.append("")
                out.append(" " * 70 + "Next question")
            elif re.search(r"(^|\s)\+\s+Q\b", l["text"]) and len(l["text"].split()) <= 6:
                out.append(" " * 20 + "Q")
                out.append("")
                out.append(" " * 70 + "Next question")
            prev = l
        pages.append("\n".join(out))
    try:
        pages = _synth_headers(pages, _optf, _nxtf)
    except Exception:
        pass
    return "\f".join(pages) + "\f"




# ---------------------------------------------------------------- structured blocks (explanation / notes) from page geometry
_TAIL_RE = re.compile(r"^(MRCP\s*\d?\s*Content\s*Map|Textbooks?\b|Links\b|Report broken media|High-yield textbook|Extended textbook|Media\b)", re.I)


def _is_improve_row(t):
    tk = t.split()
    return _improve_idx(tk) is not None or (bool(re.search(r"Discuss\s*\(?\d*\)?", t)) and len(tk) <= 10)


def _is_next_q(t):
    """"Next question" button -- OCR ne chahe 'ext question' / 'Nex questlon' kar diya ho."""
    import difflib
    a = re.sub(r"[^a-z]", "", t.lower())
    if not a or len(t) > 22 or len(t.split()) > 3:
        return False
    return a == "nextquestion" or len(a) >= 8 and difflib.SequenceMatcher(None, a, "nextquestion").ratio() >= 0.8


def _is_icon_row(t):
    if re.match(r"^\W*\w{1,3}\s+Q\s+\d{1,3}\W{0,3}$", t.strip()):
        return True
    return bool(re.search(r"(^|\s)\+\s+Q\b", t) and len(t.split()) <= 7) or bool(re.search(r"\b123\b", t) and len(t.split()) <= 7 and re.search(r"[+Q]", t))


def _garbled_improve(t):
    import difflib
    a = re.sub(r"[^A-Za-z]", "", t).lower()
    if not a or len(t.split()) > 3 or len(t) > 24:
        return False
    if re.match(r"^\s*[\[(@|{]\s*[A-Za-z]{2,9}\W*$", t) and a not in ("see", "cvs"):
        return True
    if a != "improve" and 4 <= len(a) <= 10 and difflib.SequenceMatcher(None, a, "improve").ratio() >= 0.55 and a[-1] in "en":
        return True
    if re.match(r"^\s*[\[(@|{]\s*\w{1,2}\s+[A-Za-z]{3,6}\W*$", t):
        return True
    if any(x.lower().strip("[](){}|@.,") in ("enon", "emon", "emin", "emion", "emen", "eman") for x in t.split()) and len(t) <= 14:
        return True
    return bool(re.match(r"^(re|ro)[a-z]{0,4}(en|ien|sen)$", a))


def _junk_line(t):
    """Icon / toolbar / rating-column OCR kachra."""
    tk = t.split()
    if not tk:
        return True
    if _garbled_improve(t):
        return True
    if re.match(r"^\W*\[[A-Za-z]{4,14}[\]_]*\W*$", t):
        return True
    alnum = sum(ch.isalnum() for ch in t)
    if len(t) <= 40 and not re.search(r"[A-Za-z]{4,}", t):
        return True
    if len(tk) >= 3 and sum(1 for x in tk if len(x) <= 3) >= 0.7 * len(tk) and len(t) < 45 and not re.search(r"[a-z]{4,}", t):
        return True
    if alnum < 0.55 * len(t) and len(t) < 30:
        return True
    if re.match(r"^\W*(emon|enon|emin|emion)\W*$", t) or re.search(r"\b(Quz|Tiv|ressen|reswien)\b", t) and len(tk) <= 6:
        return True
    return False


def _blocks_from_items(items, W):
    """items: [{'k':'line','l':..., 'p':page} | {'k':'table','rows':..,'p':page}] -> blocks[{'t','text','lvl','rows'}]"""
    lns = [it["l"] for it in items if it["k"] == "line"]
    if not lns:
        return [({"t": "table", "rows": it["rows"]} if it["k"] == "table" else {"t": "img", "file": it["file"], "caption": it.get("caption", "")})
                for it in items if (it["k"] == "table" and it.get("rows")) or it["k"] == "fig"]
    xs = sorted(l["x0"] for l in lns if not l["bullet"] and not l.get("teal") and len(l["text"]) >= 20)
    M = xs[len(xs) // 8] if len(xs) >= 3 else W * 0.178
    sz = sorted(l["size"] for l in lns if not l.get("teal"))
    body = sz[len(sz) // 2] if sz else 10.0
    blocks = []
    cur = None
    prev = None
    prev_p = None

    def flush():
        nonlocal cur
        if cur is not None:
            blocks.append(cur)
        cur = None

    for it in items:
        if it["k"] == "fig":
            flush()
            blocks.append({"t": "img", "file": it["file"], "caption": it.get("caption", "")})
            prev = None
            continue
        if it["k"] == "table":
            flush()
            if it.get("rows"):
                blocks.append({"t": "table", "rows": it["rows"]})
            prev = None
            continue
        l = it["l"]
        txt = l["text"].strip()
        if not txt or _junk_line(txt):
            continue
        same_page = prev is not None and prev_p == it["p"]
        gap = (l["y0"] - prev["y1"]) if same_page else None
        heading = bool(l.get("teal")) and not l["bullet"] and len(txt.split()) <= 14 \
            and (l["size"] >= 1.0 * body or len(txt.split()) <= 8)
        if heading:
            if cur is not None and cur["t"] == "h" and same_page and gap is not None and gap < 0.6 * l["size"]:
                cur["text"] += " " + txt
            else:
                flush()
                cur = {"t": "h", "text": txt}
        elif l["bullet"]:
            flush()
            off = l["x0"] - M
            cur = {"t": "b", "lvl": 2 if off > 0.061 * W else 1, "text": txt, "x0": l["x0"]}
        else:
            cont = False
            if cur is not None and cur["t"] in ("p", "b"):
                if same_page:
                    if cur["t"] == "b":
                        cont = gap is not None and gap < 0.55 * l["size"] and l["x0"] >= cur["x0"] - 0.02 * W
                    else:
                        cont = gap is not None and gap < 0.55 * l["size"] and abs(l["x0"] - cur["x0"]) < 0.02 * W
                else:       # page break
                    ended = bool(re.search(r"[.?!:;]\s*$", cur["text"]))
                    if cur["t"] == "b" and len(cur["text"].split()) <= 2:
                        ended = True
                    near = (abs(l["x0"] - cur["x0"]) < 0.02 * W) if cur["t"] == "p" else (l["x0"] >= cur["x0"] - 0.02 * W)
                    cont = (not ended) and near
            if cont:
                cur["text"] += " " + txt
            else:
                flush()
                if l["x0"] - M > 0.035 * W and prev is not None and blocks and blocks[-1]["t"] == "b" and same_page and gap is not None and gap < 0.55 * l["size"]:
                    # bullet glyph miss hua -- upar wale bullet ki continuation / sibling
                    b0 = blocks[-1]
                    b0["text"] += " " + txt
                    cur = None
                else:
                    cur = {"t": "p", "text": txt, "x0": l["x0"]}
        prev, prev_p = l, it["p"]
    flush()
    # chhoti label paragraph + uske baad bullets => bold label
    for i, b in enumerate(blocks):
        if b["t"] == "p" and len(b["text"].split()) <= 9 and not re.search(r"[.?!]$", b["text"]) \
                and i + 1 < len(blocks) and blocks[i + 1]["t"] == "b":
            b["t"] = "label"
    return blocks


def _blocks_to_md(blocks, first_is_title=False):
    """Return (title, markdown)."""
    title = None
    out = []
    prevt = None
    for i, b in enumerate(blocks):
        t = b["t"]
        if t == "h":
            if first_is_title and title is None and not out:
                title = b["text"]
                continue
            out.append(("", "## " + b["text"]))
        elif t == "label":
            out.append(("", "**" + b["text"].rstrip() + "**"))
        elif t == "p":
            out.append(("", b["text"]))
        elif t == "b":
            out.append(("b", ("  - " if b["lvl"] == 2 else "- ") + b["text"]))
        elif t == "table":
            out.append(("", table_rows_md(b["rows"])))
        elif t == "img":
            out.append(("", "![%s](%s)" % (b.get("caption", ""), b["file"])))
    if first_is_title and title is None and blocks and blocks[0]["t"] in ("p", "label") \
            and len(blocks[0]["text"].split()) <= 8 and not re.search(r"[.?!:]$", blocks[0]["text"]):
        title = blocks[0]["text"]
        out = out[1:]
    if title:
        out = [(k, ln) for (k, ln) in out if ln.strip().lower() != title.strip().lower()]
    md = []
    pk = None
    for k, line in out:
        if md and not (k == "b" and pk == "b"):
            md.append("")
        md.append(line)
        pk = k
    return title, "\n".join(md).strip()


OCR_BLOCKS_FAIL = [""]


def ocr_blocks(pdf, p0, p1, cut_hints=None, figs=None):
    """Question ke pages [p0..p1] (1-based) se: keyfact box text, explanation markdown, notes heading + markdown.
    Return dict ya None (jab structure pakda na jaye)."""
    n, d = ocr_all_pages(pdf, False)
    res = _merged_tables(pdf)
    items = []
    first = True
    cl_seen = False
    qno = None
    for p in range(max(1, p0), min(p1, n) + 1):
        data = _ocr_one_page(pdf, p, d)
        data, cl = _clean_data(data)
        _hq = None
        for _l in _page_lines(data)[:6]:
            _m = re.search(r"Question\s+(\d+)\s+of\s+\d+", _l["text"])
            if _m:
                _hq = int(_m.group(1))
                break
        if _hq is not None:
            if qno is None:
                qno = _hq
            elif _hq != qno:
                break                        # agle question ka page -- range overshoot
        if cl:
            drop = set(id(e[1]) for e in cl)
            d2 = dict(data, words=[w for w in data["words"] if id(w) not in drop])
            cs = [c for c, _ in cl]
            pitch = (cs[-1] - cs[0]) / max(1, len(cs) - 1) if len(cs) > 1 else 30.0
            cut = cs[-1] + pitch * 0.62
            cl_seen = True
        else:
            d2 = data
            cut = None
        lines = _page_lines(d2)
        boxes = data.get("boxes") or []
        pg = []
        for l in lines:
            cy = (l["y0"] + l["y1"]) / 2
            if cut is not None and cy < cut:
                continue
            _tx = l["text"]
            if re.search(r"passmedicine\.com|^https?:|^\d{1,2}/\d{1,2}/\d{2,4}|^PassMedicine$|^\d+\s*/\s*\d+$", _tx) \
                    and (l["y0"] > data["h"] * 0.93 or l["y1"] < data["h"] * 0.07):
                continue                         # browser print footer / header
            if l["y0"] > data["h"] * 0.978 or l["y1"] < data["h"] * 0.034:
                continue
            if l["x0"] > data["w"] * 0.86 and len(l["text"]) <= 4:
                continue
            inbox = any(b[1] - 2 <= cy <= b[3] + 2 and l["x0"] >= b[0] - 6 for b in boxes)
            pg.append({"k": "line", "p": p, "l": l, "y0": l["y0"], "inbox": inbox})
        for k, t in enumerate(data.get("tables") or []):
            if cut is not None and t["bbox"][1] < cut:
                continue
            pg.append({"k": "table", "p": p, "y0": t["bbox"][1], "rows": res.get((p, k))})
        for _f in (figs or []):
            if _f.get("page") == p and _f.get("box") and (cut is None or _f["box"][1] >= cut):
                pg.append({"k": "fig", "p": p, "y0": _f["box"][1], "file": _f["file"], "caption": _f.get("caption") or ""})
        pg.sort(key=lambda it: (round(it["y0"] / 3), it["l"]["x0"] if it["k"] == "line" else 0))
        if cl:
            items = []                       # sirf options-wale page se shuru
        items.extend(pg)
        first = False
    OCR_BLOCKS_FAIL[0] = ""
    if not items:
        OCR_BLOCKS_FAIL[0] = "koi item nahi (pages khali)"
        return None
    impr0 = None
    for i, it in enumerate(items):
        if it["k"] == "line" and _is_improve_row(it["l"]["text"]):
            impr0 = i
            break
    if impr0 is None:                        # Improve row OCR mein bigad gayi -- "Next question" / icon-row se kaam chalao
        for i, it in enumerate(items):
            if it["k"] == "line" and (_is_next_q(it["l"]["text"].strip()) or _is_icon_row(it["l"]["text"].strip())):
                impr0 = i
                break
    if impr0 is None:
        OCR_BLOCKS_FAIL[0] = "Improve/Discuss/Next question row nahi mili"
        return None
    # key-fact box = pehli inbox lines (Improve-row se pehle)
    inbox_idx = [i for i, it in enumerate(items[:impr0]) if it["k"] == "line" and it["inbox"]
                 and not re.search(r"Discuss|Improve", it["l"]["text"])]
    kf_end = -1
    kf_lines = []
    if inbox_idx:
        # sirf pehla contiguous run
        run = [inbox_idx[0]]
        for i in inbox_idx[1:]:
            if i == run[-1] + 1:
                run.append(i)
            else:
                break
        kf_end = run[-1]
        kf_lines = [items[i]["l"]["text"] for i in run]
    else:
        for i, it in enumerate(items):
            if it["k"] == "line" and re.search(r"important\s+for\s+m|less\s+i?m?r?n?portant", it["l"]["text"], re.I):
                kf_end = i
                kf_lines = []
                j = i - 1
                prev_y = it["l"]["y0"]
                while j >= 0 and len(kf_lines) < 3 and items[j]["k"] == "line":
                    lj = items[j]["l"]
                    if lj.get("optrow") or lj.get("pctend") or re.search(r"\d\s*%$", lj["text"]) or prev_y - lj["y1"] > 1.7 * lj["size"]:
                        break
                    kf_lines.insert(0, lj["text"])
                    prev_y = lj["y0"]
                    j -= 1
                break
    if not cl_seen and kf_end < 0:
        OCR_BLOCKS_FAIL[0] = "na options-cluster na key-fact box"
        return None                          # na options-cluster, na key-fact box -- explanation ki shuruat nahi pata
    kf_lines = [t for t in kf_lines if not re.search(r"important\s+for\s+m|less\s+i?m?r?n?portant", t, re.I)]
    impr = None
    for i in range(kf_end + 1, len(items)):
        it = items[i]
        if it["k"] == "line" and _is_improve_row(it["l"]["text"]):
            impr = i
            break
    if impr is None:
        for i in range(kf_end + 1, len(items)):
            it = items[i]
            if it["k"] == "line" and _is_next_q(it["l"]["text"].strip()):
                impr = i - 1 if (i - 1 > kf_end and items[i - 1]["k"] == "line" and len(items[i - 1]["l"]["text"]) < 30) else i
                break
    if impr is None:
        OCR_BLOCKS_FAIL[0] = "key-fact ke baad Improve/Next question row nahi mili"
        return None
    W = d and 595.0
    try:
        W = _ocr_one_page(pdf, max(1, p0), d)["w"]
    except Exception:
        pass
    ex_items = [it for it in items[kf_end + 1:impr]
                if not (it["k"] == "line" and re.search(r"important\s+for\s+me|less\s+i?m?r?n?portant", it["l"]["text"], re.I))]
    notes_items = []
    seen_content = False
    _rest = items[impr + 1:]
    for ri, it in enumerate(_rest):
        if it["k"] == "line":
            t = it["l"]["text"].strip()
            hit = False
            for (src, ttl) in (cut_hints or []):
                nxt = _rest[ri + 1]["l"]["text"].strip().lower() if ri + 1 < len(_rest) and _rest[ri + 1]["k"] == "line" else ""
                if src and len(src) >= 3 and t.startswith(src) and len(t) <= len(src) + 8 and ttl and nxt.startswith(ttl[:12].lower()):
                    hit = True
                if ttl and len(ttl) >= 12 and seen_content and t.lower().startswith(ttl[:18].lower()) and not t.startswith(("-", "*")):
                    hit = True
            if hit:
                break
            if _is_next_q(t):
                if seen_content:
                    break
                continue
            if _TAIL_RE.match(t) or _is_icon_row(t):
                break
            if it["inbox"] and not seen_content:
                pass
        notes_items.append(it)
        seen_content = True
    ex_blocks = _blocks_from_items(ex_items, W)
    nt_blocks = _blocks_from_items(notes_items, W)
    while len(nt_blocks) > 2 and nt_blocks[-1]["t"] == "p" and len(nt_blocks[-1]["text"].split()) <= 3 \
            and len(nt_blocks[-1]["text"]) <= 14 and not re.search(r"[.?!:;)]$", nt_blocks[-1]["text"]):
        nt_blocks.pop()                      # AI/+/Q icon-row ka OCR kachra ("Laxce it", "Saae")
    _t, ex_md = _blocks_to_md(ex_blocks)
    title, nt_md = _blocks_to_md(nt_blocks, first_is_title=True)
    return {"keyfact": " ".join(kf_lines).strip() or None, "explanation_md": ex_md,
            "notes_heading": title, "notes_md": nt_md}


def ocr_debug_dump(pdf, out_dir):
    """ocr_debug/: raw OCR layout text (page markers ke saath), page images (JPEG), question->pages map."""
    import json as _j
    n, d = ocr_all_pages(pdf, False)
    dd = _os.path.join(out_dir, "ocr_debug")
    pg = _os.path.join(dd, "pages")
    _os.makedirs(pg, exist_ok=True)
    txt = ocr_layout_text(pdf, False).split("\f")
    qmap, cur_q, total = {}, None, None
    parts = []
    for i in range(n):
        t = txt[i] if i < len(txt) else ""
        m = re.search(r"Question\s+(\d+)\s+of\s+(\d+)", t)
        if m:
            cur_q = int(m.group(1))
        if cur_q is not None:
            qmap.setdefault(str(cur_q), []).append(i + 1)
        parts.append("\n===== PAGE %d (Q%s) =====\n%s" % (i + 1, cur_q, t))
    with _bopen(_os.path.join(dd, "ocr_raw_layout.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    with _bopen(_os.path.join(dd, "question_pages.json"), "w") as f:
        _j.dump(qmap, f, indent=1)
    try:
        _tb = {}
        _mt = _merged_tables(pdf)
        for (pp, kk), rr in sorted(_mt.items()):
            _tb["p%d_t%d" % (pp, kk)] = rr
        with _bopen(_os.path.join(dd, "tables.json"), "w") as f:
            _j.dump(_tb, f, indent=1, ensure_ascii=False)
    except Exception:
        pass
    # page images (chhoti JPEG) -- jo pehle se na hon
    todo = [i for i in range(1, n + 1) if not _os.path.exists(_os.path.join(pg, "p%04d.jpg" % i))]
    if todo and ocr_available():
        for i in todo:
            subprocess.run(["pdftoppm", "-r", "70", "-f", str(i), "-l", str(i), "-jpeg", "-jpegopt", "quality=60",
                            pdf, _os.path.join(pg, "tmp")], capture_output=True)
            for fn in _os.listdir(pg):
                if fn.startswith("tmp") and fn.endswith(".jpg"):
                    _os.replace(_os.path.join(pg, fn), _os.path.join(pg, "p%04d.jpg" % i))
    return dd


class OcrPage:
    def __init__(self, doc, idx):
        self.doc, self.idx = doc, idx
        self._data = None
        self._lines = None

    @property
    def data(self):
        if self._data is None:
            self._data = _clean_data(_ocr_one_page(self.doc.path, self.idx + 1, self.doc.dir))[0]
        return self._data

    @property
    def rect(self):
        return Rect(0, 0, self.data["w"], self.data["h"])

    def _L(self):
        if self._lines is None:
            self._lines = _page_lines(self.data)
        return self._lines

    def get_drawings(self):
        out = []
        for (y0, y1) in self.data.get("bars", []):
            out.append({"rect": Rect(self.data["w"] * 0.15, y0, self.data["w"] * 0.17, y1),
                        "fill": (0.0, 0.5, 0.0), "color": None, "type": "f", "segs": [], "lw": 0, "isrect": True})
        for l in self._L():
            if l["bullet"]:
                bx0, by0, bx1, by1 = l["bx"]
                cy = (by0 + by1) / 2
                out.append({"rect": Rect(bx0 + 1, cy - 2.5, bx0 + 6, cy + 2.5), "fill": (0, 0, 0), "color": None,
                            "type": "f", "segs": [], "lw": 0, "isrect": False})
        return out

    def get_text(self, kind="text"):
        L = self._L()
        if kind == "words":
            res = []
            for bi, l in enumerate(L):
                for wi, w in enumerate(l["words"]):
                    res.append((w[0], w[1], w[2], w[3], w[4], bi, 0, wi))
            return res
        if kind == "dict":
            return {"blocks": [{"lines": [{"bbox": (l["x0"], l["y0"], l["x1"], l["y1"]),
                                           "spans": [{"text": l["text"], "size": l["size"]}]}]} for l in L]}
        return "\n".join(l["text"] for l in L)

    def get_textbox(self, rect):
        r = Rect(rect)
        got = [w for w in self.get_text("words") if r.x0 - 1 <= (w[0] + w[2]) / 2 <= r.x1 + 1
               and r.y0 - 1 <= (w[1] + w[3]) / 2 <= r.y1 + 1]
        return " ".join(w[4] for w in got)

    def get_links(self):
        out = []
        try:
            from pypdf import PdfReader
            if getattr(self.doc, "_rd", None) is None:
                self.doc._rd = PdfReader(self.doc.path)
            pp = self.doc._rd.pages[self.idx]
            mb = pp.mediabox
            pw, ph = float(mb.width), float(mb.height)
            sc = self.data["w"] / pw if pw else 1.0
            for a in pp.get("/Annots") or []:
                a = a.get_object()
                if a.get("/Subtype") != "/Link":
                    continue
                act = a.get("/A")
                uri = act.get_object().get("/URI") if act is not None else None
                if not uri:
                    continue
                r = [float(v) for v in a["/Rect"]]
                x0, x1 = sorted([r[0], r[2]])
                y0, y1 = sorted([r[1], r[3]])
                out.append({"uri": str(uri), "from": Rect(x0 * sc, (ph - y1) * sc, x1 * sc, (ph - y0) * sc), "kind": 2})
        except Exception:
            pass
        return out

    def find_tables(self, strategy="lines"):
        class _N:
            tables = []
        return _N()


class OcrDoc:
    def __init__(self, path):
        self.path = path
        self._n = pdf_num_pages(path)
        self.dir = _ocr_dir(path)
        self._cache = {}

    def __len__(self):
        return self._n

    def __getitem__(self, i):
        if i < 0:
            i += self._n
        if i not in self._cache:
            self._cache[i] = OcrPage(self, i)
        return self._cache[i]

    def close(self):
        pass
'''


_PDFGEO_MOD = None


def _pdfgeo_module():
    """EK hi module instance (OCR_TABLES / caches share hon) -- pehle har call par naya banta tha."""
    global _PDFGEO_MOD
    if _PDFGEO_MOD is None:
        import types
        mod = types.ModuleType("pdfgeo")
        exec(compile(_PDFGEO_SRC, "pdfgeo", "exec"), mod.__dict__)
        _PDFGEO_MOD = mod
    return _PDFGEO_MOD


def _make_geo():
    if HAVE_FITZ:
        return fitz, "pymupdf"
    try:
        import pypdf  # noqa: F401
        import shutil as _sh
        if not _sh.which("pdftotext"):
            return None, "pdftotext missing (pkg install poppler)"
        return _pdfgeo_module(), "pypdf+poppler"
    except ImportError:
        return None, "pypdf missing (pip install pypdf)"


PARSER_VERSION = "r26"
FRESH = False
_OCR_PDFS = set()      # image-only PDFs jinka text OCR se banaya gaya


def _ocr_mod():
    try:
        mod = _pdfgeo_module()
        return mod if mod.ocr_available() else None
    except Exception:
        return None


def _open_geo_doc(pdf_path):
    """text-PDF -> geo (PyMuPDF/pypdf); image-only PDF -> OCR-backed doc."""
    if pdf_path in _OCR_PDFS:
        mod = _ocr_mod()
        return mod.OcrDoc(pdf_path) if mod else None
    return geo.open(pdf_path) if geo is not None else None


geo, GEO_BACKEND = _make_geo()
HAVE_GEO = geo is not None

try:
    from PIL import Image, ImageOps, ImageEnhance
except ImportError:
    print("Pillow chahiye: pip install pillow --break-system-packages")
    sys.exit(1)

try:
    import numpy as np
    from scipy import ndimage
    HAVE_AUTOCROP = True
except ImportError:
    HAVE_AUTOCROP = False

try:
    from pypdf import PdfReader
    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False

try:
    import urllib.request
    import urllib.error
    HAVE_URLLIB = True
except ImportError:
    HAVE_URLLIB = False


# ---------------------------------------------------------------------------
# CONFIG: noise patterns yahan edit karo agar naya PDF format thoda alag ho
# ---------------------------------------------------------------------------

NOISE_LINE_PATTERNS = [
    r'^\d{1,2}/\d{1,2}/\d{4},?\s*\d{1,2}:\d{2}',      # timestamp: 1/1/2025, 20:29
    r'^\d{1,2}/\d{1,2}/\d{4}\.?\s*\d{1,2}:\d{2}',      # OCR variant "12/11/2025. 20:29"
    r'^PassMedicine\s*$',
    r'^\s*Question\s+\d+\s+of\s+\d+',
    r'^\s*[<\[]\s*Question',
    r'Discuss\s*\(\d+\)',
    r'^\s*Improve\s*$',
    r'Important for me',
    r'Less important',
    r'passmedicine\.com',                              # any mangled URL variant
    r'^\s*\d+\s*/\s*\d+\s*$',                          # page footer "1/6" or "4 / 2622"
    r'^\s*Textbooks\s*$',
    r'^\s*Links\s*$',
    r'^\s*High-yield textbook\s*$',
    r'^\s*Extended textbook\s*$',
    r'^\s*Next question\s*$',
    r'Image sourced from',
    r'^\s*[BIAU]\s*[▼•]?\s*$',                         # stray toolbar letters
    r'^\s*[=☰▾]+\s*$',
    r'^Dr\.\s?[A-Za-z]+\s+[A-Za-z]+\s*$',              # doctor-name branding line
    r'^\s*Cardiologist\s*$',
    r'^\s*MRCPUK-MSc\s*$',
    r'^https?://t\.me/\S+',                            # telegram branding link
    r'Suggest\s+(an\s+)?edit',
    r'Report\s+broken\s+link',
    r'^\s*Score:\s*[\d.]+%?\s*$',                       # page-rating widget "Score: 57.1%"
    r'^\s*(\d\s+){1,4}\d\s*$',                          # stray page/rating numbers like "1 2 3"
    r'^\s*Q\s*$',                                       # lone "Q" icon-text leak
    r'^[\u0000-\u001F\u2400-\u2BFF\uE000-\uF8FF\s]+$',  # box-drawing / private-use glyph junk
    r'^\s*\d{1,4}\s*$',                                 # bare standalone number (media/ref index leak)
    r'Report\s+broken\s+media',
    r'©\s*Image\s+used\s+on\s+license',
    r'Life in the Fast Lane',
    r'-\s*YouTube\b',                                   # widget video-row (not always at line-end -- counts follow)
    r'-\s*causes,\s*symptoms,\s*diagnosis,\s*treatment,\s*pathology\s*$',  # related-topic tag-list widget line
    r'^\s*Discuss\s*$',                                 # bare "Discuss" widget-table header (no count in parens)
    r'^\s*Media\s*$',                                   # standalone "Media" mini-heading with no real content
    r'All contents of this site are',                   # site-wide footer/copyright widget
    r'^\s*Back to top\s*$',
    r'^[A-Za-z][A-Za-z .()\'/-]{1,60}\s{2,}\d{1,3}\s{2,}\d{1,3}\s*$',  # reference-guideline widget row (e.g. "NICE   66   42", "Resuscitation Council   43   23")
]

# Known option-text markers that appear bold in the explanation (we don't
# have real bold info from OCR, so we bold-tag any sentence that STARTS
# with one of the option phrases, matched from the options list at runtime).

HEADING_KEYWORDS = [
    "Murmurs", "Ejection systolic", "Holosystolic", "Pansystolic",
    "Late systolic", "Early diastolic", "Mid-late diastolic",
    "Continuous machine-like murmur",
]

# Regex fallback for subheadings not in the fixed list above (so the parser
# still works on a different specialty's notes page, not just "Murmurs").
SUBHEADING_PATTERN = re.compile(
    r'^[A-Z][a-zA-Z\- ]{2,40}(systolic|diastolic|murmur|regurgitation|stenosis)$',
    re.IGNORECASE
)

MNEMONIC_TITLE_PATTERN = r'^[A-Z]{3,6}$'   # e.g. "RILE"

# Common medical/investigation abbreviations (MRI, CSF, ECG etc.) match the
# same ALL-CAPS 3-6 letter shape as genuine mnemonics (like "RILE") but are
# plain topic headings, not memory aids -- isliye inhe explicitly exclude
# karte hain taake wo galti se mnemonic-box na ban jayein.
MNEMONIC_TITLE_EXCLUDE = {
    "MRI", "CT", "CSF", "ECG", "EKG", "EEG", "EMG", "FBC", "LFT", "LFTS",
    "TFT", "TFTS", "TSH", "ABG", "VBG", "CXR", "MSU", "PCR", "HIV", "DNA",
    "RNA", "ANA", "ANCA", "RF", "CRP", "ESR", "INR", "APTT", "BMI", "BP",
    "HR", "RR", "GCS", "ICU", "HDU", "IV", "IM", "PO", "PR", "PEG", "NG",
    "ARDS", "DVT", "PE", "MI", "TIA", "CVA", "SAH", "ICH", "UTI", "URTI",
    "LRTI", "COPD", "GORD", "IBD", "IBS", "PUD", "GI", "GU", "CNS", "PNS",
    "ANS", "CVS", "RS", "USS", "OGD", "ERCP", "MRCP", "MCV", "MCH", "MCHC",
    "WCC", "PLT", "HB", "U", "LDH", "ALP", "ALT", "AST", "GGT", "CK",
    "BNP", "NT", "PSA", "HBA", "HBA1C", "OCP", "COCP", "HRT", "PPI",
    "NSAID", "NSAIDS", "COX", "ACE", "ARB", "CCB", "SSRI", "SNRI", "TCA",
    "MAOI", "DMARD", "STI", "STD", "PID", "IUD", "IUS", "AF", "SVT", "VT",
    "VF", "HF", "CHF", "IHD", "CAD", "CABG", "PCI", "COPD", "ARF", "CKD",
    "AKI", "ESRF", "RRT", "PD", "HD", "DKA", "HHS", "T1DM", "T2DM",
}

# OCR renders the two bullet levels PassMedicine uses ("•" top-level,
# "○" nested) inconsistently as plain-text glyphs. These are the
# character sets we've observed for each level.
BULLET_L1_CHARS = "•*e¢●"
BULLET_L2_CHARS = "○©°o■▪▫‣·"


# ---------------------------------------------------------------------------
# STEP 1: extract embedded images from PDF
# ---------------------------------------------------------------------------

def extract_images(pdf_path, work_dir):
    """
    PDF ke andar embedded images nikaalta hai. Agar PyMuPDF (fitz) available
    hai to wahi use hota hai (fast, no external process). Nahi to Termux-
    friendly fallback: `mutool extract` command-line tool (pkg install
    mupdf-tools se milta hai) -- isse pip pe heavy PyMuPDF build karne ki
    zaroorat nahi padti.
    """
    if HAVE_FITZ:
        return _extract_images_fitz(pdf_path, work_dir)
    return _extract_images_mutool(pdf_path, work_dir)


def _extract_images_fitz(pdf_path, work_dir):
    doc = fitz.open(pdf_path)
    image_paths = []
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            base = doc.extract_image(xref)
            ext = base["ext"]
            out_path = os.path.join(work_dir, f"raw_{len(image_paths):02d}.{ext}")
            with open(out_path, "wb") as f:
                f.write(base["image"])
            image_paths.append(out_path)
    doc.close()
    return image_paths


def _extract_images_mutool(pdf_path, work_dir):
    import shutil as _sh
    if not _sh.which("mutool"):
        print(f"{C.YELLOW}      (mutool nahi mila -- embedded-image step skip; PassMedicine ke images CloudFront links se aate hain, "
              f"isse farq nahi padta){C.RESET}")
        return []
    result = subprocess.run(
        ["mutool", "extract", os.path.abspath(pdf_path)],
        cwd=work_dir, capture_output=True, text=True
    )
    if result.returncode != 0:
        print("mutool extract fail hua:", result.stderr)
        print("Termux pe chalane ke liye: pkg install mupdf-tools")
        sys.exit(1)
    # mutool extract embedded images ko work_dir mein
    # "image-XXXXX.jpg"/"image-XXXXX.png" (aur fonts ko "font-XXXXX.ttf")
    # jaisi files ke roop mein deta hai -- sirf image-* files chahiye
    found = sorted(Path(work_dir).glob("image-*.*"))
    # rename to our raw_NN.ext convention for consistency downstream
    image_paths = []
    for idx, f in enumerate(found):
        new_path = os.path.join(work_dir, f"raw_{idx:02d}{f.suffix}")
        os.replace(f, new_path)
        image_paths.append(new_path)
    return image_paths


# ---------------------------------------------------------------------------
# STEP 2: OCR each image
# ---------------------------------------------------------------------------

IMAGE_LINK_EXT_RE = re.compile(r'\.(png|jpe?g|webp)(\?|$)', re.IGNORECASE)


def build_page_link_image_map(pdf_path):
    """
    PassMedicine PDFs mein diagram screenshot ke peeche ek clickable
    hyperlink (URI annotation) hoti hai jo CloudFront pe hosted **asal
    sharp image** ki taraf point karti hai -- jabke page pe dikhne wala
    screenshot khud chhota/compressed hota hai. Isliye pixel-crop karne ki
    bajaye hum seedha ye links nikaal kar unse original-resolution image
    download karte hain -- koi blur/DPI/crop dependency nahi rehti.

    Poore PDF ko ek hi baar scan kar ke {page_num: [uri, uri, ...]}
    (deduped, per-page order-preserved) return karta hai -- taake har
    question ke liye dobara PDF na parse karna pade.
    """
    page_map = {}
    if not HAVE_PYPDF:
        return page_map
    try:
        reader = PdfReader(pdf_path)
    except Exception:
        return page_map

    for pnum, page in enumerate(reader.pages, start=1):
        annots = page.get("/Annots")
        if not annots:
            continue
        seen = set()
        uris = []
        for a in annots:
            try:
                obj = a.get_object()
            except Exception:
                continue
            if obj.get("/Subtype") != "/Link":
                continue
            action = obj.get("/A")
            if not action or "/URI" not in action:
                continue
            uri = str(action["/URI"])
            # sirf image-file links chahiye -- menu.php/textbook.php jaisi
            # navigation links (jo har page pe hoti hain) exclude karte hain
            if not IMAGE_LINK_EXT_RE.search(uri):
                continue
            if uri not in seen:
                seen.add(uri)
                uris.append(uri)
        if uris:
            page_map[pnum] = uris
    return page_map


def _curl_available():
    return shutil.which("curl") is not None


def download_image(uri, dest_path, timeout=15):
    """
    Diagram ki asal-resolution image ko uske CloudFront/source URL se
    seedha download karta hai.

    IMPORTANT: Python ka apna urllib/ssl stack ek alag TLS "fingerprint"
    (JA3) bhejta hai jo real browsers se different hota hai -- AWS WAF /
    CloudFront ke bot-detection rules isay pehchan kar block kar dete
    hain, chahe headers (User-Agent, Referer) bilkul sahi kyun na hon.
    Isi wajah se sirf phone/Termux se 403 aata tha, jabke wahi URL kahin
    aur se (jaise browser ya doosre tool se) fetch karne par bilkul
    theek chalta tha. Fix: `curl` binary use karte hain jiska TLS
    fingerprint normal browser jaisa hi hota hai, isliye WAF usay block
    nahi karta. Agar curl kisi wajah se available na ho, tab hi purana
    urllib fallback chalega.

    Network blips (timeout, connection reset) par yahi RUK kar dobara
    try karta rehta hai. Lekin agar server khud hi permanently mana kar
    de (403 Forbidden / 404 Not Found -- matlab signed URL expire ho
    chuki hai ya image wahan hai hi nahi), to infinite retry karna
    faida mand nahi -- chand attempts ke baad us ek image ko skip kar
    dete hain, taake baaki images/questions aur poora JSON generate
    hone se na ruke. Wait ke dauran user Ctrl+C se bhi safely rok
    sakta hai.
    """
    attempt = 0
    max_permanent_attempts = 3  # 403/404 jaisi permanent error par sirf itni baar try
    use_curl = _curl_available()

    while True:
        attempt += 1
        try:
            if use_curl:
                tmp_out = dest_path + ".part"
                result = subprocess.run(
                    [
                        "curl", "-sS", "-L", "--fail",
                        "--max-time", str(timeout),
                        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        "-e", "https://www.passmedicine.com/",
                        "-H", "Accept: image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                        "-o", tmp_out,
                        uri,
                    ],
                    capture_output=True, text=True, timeout=timeout + 10,
                )
                if result.returncode != 0:
                    # curl --fail se HTTP error par exit code 22 milta hai,
                    # baaki codes network/timeout/DNS jaise transient errors
                    if os.path.exists(tmp_out):
                        try:
                            os.remove(tmp_out)
                        except OSError:
                            pass
                    if result.returncode == 22:
                        raise urllib.error.HTTPError(uri, 403, "curl --fail: HTTP error", None, None)
                    raise RuntimeError(f"curl exit code {result.returncode}: {result.stderr.strip()[:200]}")
                if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) < 200:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
                    raise ValueError("response too small -- likely error page")
                os.replace(tmp_out, dest_path)
            else:
                if not HAVE_URLLIB:
                    return False
                req = urllib.request.Request(
                    uri,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        "Referer": "https://www.passmedicine.com/",
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    }
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read()
                if len(data) < 200:  # itna chhota response matlab error page hai
                    raise ValueError("response too small -- likely error page")
                with open(dest_path, "wb") as f:
                    f.write(data)
            if attempt > 1:
                print(f"\r\x1b[2K{C.GREEN}Harry Bht Dair Lgadi 😎{C.RESET}")
            return True
        except urllib.error.HTTPError as e:
            if e.code in (403, 404) and attempt >= max_permanent_attempts:
                RED = "\033[1;31m"
                print(f"\r\x1b[2K{RED}Harry ye URL hi mar chuka hai (HTTP {e.code}) -- "
                      f"{max_permanent_attempts} tries ke baad skip kar raha hoon, "
                      f"baaki images/questions pe aage badh raha hoon.{C.RESET}")
                return False
            wait_s = min(30, 5 * attempt)
            RED = "\033[1;31m"
            print(f"\r\x1b[2K{RED}Harry Yaar Net Tou Acha Lgwa 😤  (HTTP {e.code}, attempt {attempt}, {wait_s}s mein dobara, Ctrl+C rokne ke liye){C.RESET}", end="", flush=True)
            try:
                time.sleep(wait_s)
            except KeyboardInterrupt:
                print()
                raise
        except Exception as e:
            max_generic_attempts = 6  # network blip jaisi generic error par bhi hamesha ke liye retry nahi -- itni baar ke baad skip
            if attempt >= max_generic_attempts:
                RED = "\033[1;31m"
                print(f"\r\x1b[2K{RED}Harry ye image download hi nahi ho rahi ({type(e).__name__}) -- "
                      f"{max_generic_attempts} tries ke baad skip kar raha hoon, "
                      f"baaki images/questions pe aage badh raha hoon.{C.RESET}")
                return False
            wait_s = min(30, 5 * attempt)  # 5,10,15... max 30s tak badhta hai
            RED = "\033[1;31m"
            print(f"\r\x1b[2K{RED}Harry Yaar Net Tou Acha Lgwa 😤  (attempt {attempt}, {wait_s}s mein dobara, Ctrl+C rokne ke liye){C.RESET}", end="", flush=True)
            try:
                time.sleep(wait_s)
            except KeyboardInterrupt:
                print()
                raise


def autocrop_diagram(image_path):
    """
    Jab diagram image ek pure screenshot ho (URL bar, toolbar, textbook/link
    boxes ke saath), to us diagram ke real chart area ko dhoondh kar sirf
    utna hi crop karta hai. PassMedicine diagrams aksar ek halke cream/beige
    background box (~RGB 255,246,213) mein hote hain -- hum us rang ka
    sabse bada connected region dhoondh kar uske bounding box tak crop
    karte hain. Agar aisa koi region na mile (plain photo/diagram hai,
    poore-screenshot wala nahi), to original image waisi ki waisi rehti hai.
    """
    if not HAVE_AUTOCROP:
        return image_path

    img = Image.open(image_path).convert("RGB")
    arr = np.array(img)
    # cream/beige background PassMedicine diagrams mein common hai; agar
    # aapke source ka background color alag ho to yahan target adjust karo
    target = np.array([255, 246, 213])
    dist = np.abs(arr.astype(int) - target).sum(axis=2)
    mask = dist < 30

    if mask.sum() < 5000:  # itna chhota match matlab beige box hai hi nahi
        return image_path

    labeled, n = ndimage.label(mask)
    if n == 0:
        return image_path
    sizes = ndimage.sum(mask, labeled, range(1, n + 1))
    biggest = sizes.argmax() + 1
    ys, xs = np.where(labeled == biggest)

    # bahut chhota component (noise pixel) ho to skip
    if (xs.max() - xs.min()) < 100 or (ys.max() - ys.min()) < 100:
        return image_path

    pad = 15
    x0, x1 = max(0, xs.min() - pad), min(img.width, xs.max() + pad)
    y0, y1 = max(0, ys.min() - pad), min(img.height, ys.max() + pad)
    cropped = img.crop((x0, y0, x1, y1))

    out_path = str(Path(image_path).with_suffix("")) + "_cropped.png"
    cropped.save(out_path)
    return out_path


def get_pdf_text_if_real(pdf_path, min_words=30):
    """
    Kuch PDFs (jaise print/export se banayi gayi) mein real selectable text
    hota hai -- OCR ki zaroorat nahi, seedha `pdftotext -layout` se text
    milta hai jo OCR se kaafi zyada accurate hota hai (properly wraps,
    percentages bhi bina kisi issue ke aate hain). Agar itna text na mile,
    None return karta hai aur caller OCR-mode pe fallback kar sakta hai.
    """
    result = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    text = result.stdout
    if len(re.findall(r'[A-Za-z]{3,}', text)) < min_words:
        # image-only PDF (screenshots) -- agar tesseract hai aur PassMedicine-2 format lagta hai to OCR se
        # pdftotext-jaisa layout text banao (cache hota hai, dobara chalane pe fast).
        mod = _ocr_mod()
        if mod is None:
            return None
        print(f"{C.YELLOW}      Text layer nahi hai (image-only PDF) -- OCR se padh raha hoon (pehli baar slow, phir cache){C.RESET}")
        otext = mod.ocr_layout_text(pdf_path)
        if len(re.findall(r"Question\s+\d+\s+of\s+\d+", otext)) < 3:
            return None        # PassMedicine-2 jaisa format nahi -- purane OCR path pe wapas
        _OCR_PDFS.add(pdf_path)
        return otext
    return text


def filter_large_images(image_paths, min_dim=250):
    """
    Text-PDF mode mein embedded images ke bahut saare icons/logos/decoration
    ho sakte hain jo diagram nahi hain. Chhote images (icons, bullet dots,
    watermarks) ko hata kar sirf badi images (jo asal diagram ho sakti hain)
    rakhta hai.
    """
    kept = []
    for p in image_paths:
        try:
            with Image.open(p) as img:
                if img.width >= min_dim and img.height >= min_dim:
                    kept.append(p)
        except Exception:
            continue
    return kept


def _dedup_diagrams_by_content(diagram_list, out_dir):
    """
    Diagram-entries ki list mein se, agar do (ya zyada) entries ka ASAL
    file-content byte-for-byte SAME hai (chahe filename/idx alag ho --
    jaise ek hi diagram stem aur notes dono jagah alag CloudFront links
    se aaya ho, ya koi purana duplicate save ho gaya ho), to sirf pehli
    entry rakhte hain, baaki discard karte hain. Ye per-idx dedup se
    alag hai -- wo sirf SAME idx wale duplicates pakadta hai, ye kisi
    bhi do entries ke beech content-level duplication pakadta hai.
    """
    seen_hashes = set()
    result = []
    for d in diagram_list:
        fpath = os.path.join(out_dir, d.get("file", ""))
        try:
            with open(fpath, "rb") as f:
                h = hashlib.md5(f.read()).hexdigest()
        except Exception:
            result.append(d)  # file read na ho paye to filter mat karo
            continue
        if h in seen_hashes:
            continue  # yehi content is question mein pehle bhi aa chuka -- skip
        seen_hashes.add(h)
        result.append(d)
    return result


def render_page_range(pdf_path, start_page, end_page, work_dir, tag, dpi=400):
    """
    pdfimages sirf PDF ke andar ki embedded raw image nikalta hai, jo kabhi
    kabhi low-resolution hoti hai (blurry). Iski jagah poore page ko high-DPI
    pe render karte hain (vector lines/text sahit, jaisa PDF visually dikhta
    hai) -- isse diagram crop karne ke baad bhi sharp rehta hai.
    """
    prefix = os.path.join(work_dir, f"page{tag}")
    subprocess.run(
        ["pdftoppm", "-png", "-r", str(dpi), "-f", str(start_page), "-l", str(end_page), pdf_path, prefix],
        capture_output=True, text=True
    )
    return sorted(Path(work_dir).glob(f"page{tag}-*.png"))


def extract_page_range_images(pdf_path, start_page, end_page, work_dir, tag):
    """
    pdfimages (poppler-utils) se sirf diye gaye page-range ke embedded
    images nikalta hai -- isse har question ko uske sahi page(s) ke
    diagrams se link kiya ja sakta hai, na ke poori PDF ke flat pool se.
    """
    prefix = os.path.join(work_dir, f"p{tag}")
    subprocess.run(
        ["pdfimages", "-f", str(start_page), "-l", str(end_page), "-png", pdf_path, prefix],
        capture_output=True, text=True
    )
    return sorted(Path(work_dir).glob(f"p{tag}-*.*"))


def compute_page_bounds(raw_text_with_pages):
    """
    pdftotext ka output form-feed (\\f) characters se page-separated hota
    hai. Har page ka (start_offset, end_offset) raw_text ke andar nikalta
    hai, taake baad mein kisi bhi text-position (jaise ek question marker)
    ka asal PDF page number pata chal sake.
    """
    bounds = []
    offset = 0
    for page_text in raw_text_with_pages.split("\f"):
        start = offset
        end = offset + len(page_text)
        bounds.append((start, end))
        offset = end + 1  # +1 for the consumed \f character
    return bounds


def offset_to_page(bounds, pos):
    for idx, (start, end) in enumerate(bounds):
        if start <= pos <= end:
            return idx + 1  # 1-indexed page number
    return len(bounds)  # fallback: last page


def preprocess_for_ocr(image_path):
    """
    PassMedicine ke option-percentage numbers halke gray rang mein hote hain
    aur plain OCR unhe miss kar deta hai. Contrast boost karke unhe readable
    banate hain — isse text ka baaki hissa bhi cleaner OCR hota hai.
    """
    img = Image.open(image_path).convert("L")
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Contrast(img).enhance(2.5)
    enhanced_path = str(Path(image_path).with_suffix("")) + "_enh.png"
    img.save(enhanced_path)
    return enhanced_path


def ocr_image(image_path, psm="3", enhance=True):
    src = preprocess_for_ocr(image_path) if enhance else image_path
    result = subprocess.run(
        ["tesseract", src, "-", "--psm", psm],
        capture_output=True, text=True
    )
    return result.stdout


def classify_and_ocr(image_paths, psm="3", enhance=True, diagram_mode="sparse"):
    """
    Returns:
        full_text: concatenated OCR text from all "content" images (in order)
        diagram_images: list of image paths judged to be non-text diagrams

    diagram_mode:
        "sparse"  - flag images with very few real OCR words (good for
                    question-PDF screenshots where the diagram is visually
                    dense but has little text)
        "markers" - flag images containing specific axis-label words
                    together (good for notes screenshots, where a short
                    bullet list can have a similarly low word count to an
                    actual diagram, so word-sparsity alone is unreliable)
    """
    texts = []
    diagram_images = []
    for path in image_paths:
        text = ocr_image(path, psm=psm, enhance=enhance)
        alpha_words = re.findall(r'[A-Za-z]{3,}', text)
        low = text.lower()
        if diagram_mode == "markers":
            is_diagram = "diastole" in low and "systole" in low
        else:
            is_diagram = len(alpha_words) < 60
        if is_diagram:
            diagram_images.append(path)
        else:
            texts.append(text)
    return "\n".join(texts), diagram_images


# ---------------------------------------------------------------------------
# STEP 3: clean noise
# ---------------------------------------------------------------------------

# Fragments that show up STUCK inside an otherwise-real content line (OCR
# sometimes merges the header/nav bar onto the same line as the question
# stem). These are stripped out of the line rather than dropping the whole
# line.
SUBSTRING_NOISE_PATTERNS = [
    r'^\d{1,4}/?\d{0,4}/?\d{0,4},?\s*\d{1,2}:\d{2}\s*',   # leading timestamp junk
    r'PassMedicine',
    r'\(?[<\[]\}?\s*Question\s+\d+\s+of\s+\d+[^A-Za-z]*',
    r'[xX]\s*[Pp]\s*[>\]\)]+',                             # stray nav icon glyphs
]


def clean_lines(raw_text):
    lines = raw_text.split("\n")
    clean = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if any(re.search(pat, stripped, re.IGNORECASE) for pat in NOISE_LINE_PATTERNS):
            continue
        for pat in SUBSTRING_NOISE_PATTERNS:
            stripped = re.sub(pat, '', stripped, flags=re.IGNORECASE).strip()
        if not stripped:
            continue
        clean.append(stripped)
    return clean


# ---------------------------------------------------------------------------
# STEP 4: structure the content
# ---------------------------------------------------------------------------

OPTION_PCT_RE = re.compile(r'^(.*?)\s+(\d{1,3})\s*%?$')


def extract_line_segments(indent, text):
    """
    Ek line ko uske "column" segments mein todta hai (3+ spaces ke gap pe
    split), absolute page-position ke saath -- table-detection ke liye
    zaroori hai kyunki wahi columns ka reliable signal hai.
    """
    return [(indent + m.start(), m.group()) for m in re.finditer(r'\S.*?(?=\s{3,}|$)', text)]


def paragraph_is_table(para):
    """
    Ek paragraph table ki row/header lagta hai agar usmein kam se kam ek
    line mein 2+ column-segments (bade space-gap se separated) hon --
    prose sentences mein 3+ consecutive spaces practically kabhi nahi
    aate, isliye ye signal reliable hai.
    """
    return any(len(extract_line_segments(ind, t)) >= 2 for ind, t in para)


def cluster_positions(offsets, tol=8):
    """Close-together x-positions ko ek column mein group karta hai."""
    uniq = sorted(set(offsets))
    clusters = []
    for x in uniq:
        if clusters and x - clusters[-1][0] <= tol:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    return [c[0] for c in clusters]  # representative (smallest) offset per cluster


def paragraph_to_table_row(para, columns, tol=10):
    """Paragraph ki saari lines ko diye gaye columns mein map karke, har
    column ki text ko (wrap/continuation) jodta hai."""
    cells = ["" for _ in columns]
    for indent, text in para:
        for seg_x, seg_text in extract_line_segments(indent, text):
            best_idx = min(range(len(columns)), key=lambda i: abs(columns[i] - seg_x))
            if abs(columns[best_idx] - seg_x) <= tol:
                cells[best_idx] = (cells[best_idx] + " " + seg_text).strip()
    return cells


def table_to_markdown(header, rows):
    """GFM-style markdown table string banata hai."""
    ncols = max(len(header), max((len(r) for r in rows), default=0))
    header = header + [""] * (ncols - len(header))
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("|" + "|".join([" --- "] * ncols) + "|")
    for r in rows:
        r = r + [""] * (ncols - len(r))
        lines.append("| " + " | ".join(c.replace("|", "/") for c in r) + " |")
    return "\n".join(lines)


def build_table_md_from_run(run):
    """
    Ek run (consecutive tabular paragraphs ki list) se markdown table
    banata hai, ya None agar genuine table nahi lagta (sirf 1 row, no data).
    """
    if not run:
        return None
    # Special case: ek hi paragraph hai jismein HAR line khud apni
    # poori row hai (jaise lab-values table: "Na+   141 mmol/L   (135-145)")
    if len(run) == 1 and len(run[0]) >= 1 and all(
        len(extract_line_segments(ind, t)) >= 2 for ind, t in run[0]
    ):
        all_offsets = [x for ind, t in run[0] for x, _ in extract_line_segments(ind, t)]
        cols = cluster_positions(all_offsets)
        rows = [paragraph_to_table_row([line], cols) for line in run[0]]
        return table_to_markdown(["" for _ in cols], rows)

    all_offsets = [x for para in run for ind, t in para for x, _ in extract_line_segments(ind, t)]
    cols = cluster_positions(all_offsets)
    rows = [paragraph_to_table_row(para, cols) for para in run]
    header, data_rows = (rows[0], rows[1:]) if rows else ([], [])
    if data_rows:  # kam se kam ek header + ek data row ho tabhi asal table hai
        return table_to_markdown(header, data_rows)
    return None


CALLOUT_MARKER = "\x00CALLOUT\x00"
CALLOUT_SEP = "\x01"
IMAGE_MARKER = "\x00IMAGE\x00"
NOTES_START_TOKEN = "@@NOTES_START@@"
ASSUME_IMAGES = False   # --assume-images: download nahi, bas filenames JSON mein daalo
IMAGES_ONLY = False     # --images-only: sirf images download karo, JSON mat banao


def is_callout_paragraph(para):
    """
    Ek "summary callout" box (jaise 'Water deprivation test: primary
    polydipsia' + niche indented points) -- ye kahin bhi (explanation ya
    notes ke andar) ho sakta hai, is function se identify hota hai.
    Genuine subheadings (jaise "Major points include:") bhi colon se
    khatam hoti hain lekin colon ke BAAD koi text nahi hoti -- isliye
    "colon ke baad real text ho" ye zaroori condition hai taake normal
    subheading + uski bullets galti se poori callout-box na ban jaye.
    """
    if len(para) < 2:
        return False
    first_indent, first_text = para[0]
    rest_lines = para[1:]
    if ':' not in first_text:
        return False
    after_colon = first_text.split(':', 1)[1].strip()
    if not after_colon:
        return False
    return all(ind > first_indent for ind, _ in rest_lines)


def callout_to_blockquote_md(para):
    first_indent, first_text = para[0]
    # Callout box ka title kabhi OCR se ek stray "NN%" pakad leta hai
    # (pass options ke percentage badges se bilkul paas hone ki wajah se) --
    # genuine callout titles kabhi percentage pe khatam nahi hote.
    first_text = re.sub(r'\s+\d{1,3}%$', '', first_text)
    # Har line is callout ke andar apna naya bullet nahi hota -- kai baar
    # sirf pichle bullet ka word-wrap continuation hota hai (bullet marker
    # OCR mein gayab ho jata hai, sirf indentation bachti hai jo yahan sab
    # lines ke liye SAME hoti hai). Naya bullet sirf tab maante hain jab
    # line khud "label: description" pattern se shuru ho (jaisa pehle
    # bullet ka style hai) -- warna pichle bullet mein jod dete hain.
    points = []
    for _, t in para[1:]:
        if points and not re.search(r'[.?!:]$', points[-1].rstrip()) and not re.match(r'^\s*\S+:\s', t):
            points[-1] = points[-1] + " " + t
        else:
            points.append(t)
    md_points = "\n".join(f"> - {p}" for p in points)
    return f"> **{first_text}**\n{md_points}"


KEYFACT_MARKER = "\x03KEYFACT\x03"


def explanation_paragraphs_to_markdown(paragraphs):
    """
    Explanation ke paragraphs ko unki asli order mein rakhte hue markdown
    banata hai -- agar beech mein kahin summary-callout ho to usko blockquote
    box ki tarah wahi rakhta hai, baaki normal flowing paragraphs.
    """
    parts = []
    for para in paragraphs:
        joined = " ".join(t for _, t in para)
        if joined.startswith(KEYFACT_MARKER):
            parts.append(f"> {joined[len(KEYFACT_MARKER):]}")
        elif is_callout_paragraph(para):
            parts.append(callout_to_blockquote_md(para))
        else:
            parts.append(joined)
    return "\n\n".join(parts).strip()


PBREAK = "\x01"


def notes_paragraphs_to_lines(paragraphs):
    """
    Notes paragraphs ko flat (indent,text) lines mein todta hai (jaisa
    parse_notes_by_indent expect karta hai), lekin agar koi paragraph
    summary-callout ho to use ek special sentinel line mein encode kar
    deta hai taake wo apni sahi position pe hi (jahan mila) blockquote
    box ki tarah render ho, na ke bullets mein mix ho jaye.
    """
    lines = []
    for para in paragraphs:
        if is_callout_paragraph(para):
            first_indent, first_text = para[0]
            payload = CALLOUT_SEP.join([first_text] + [t for _, t in para[1:]])
            lines.append((first_indent, CALLOUT_MARKER + payload))
        else:
            # blank-line se alag hui lambi prose paragraph ko pichle bullet mein mat chipkao:
            # PBREAK marker lagao, parse_notes_by_indent isse alag bullet banata hai.
            if (lines and para and not lines[-1][1].startswith((CALLOUT_MARKER, IMAGE_MARKER))):
                ft = para[0][1]
                prev_ind, prev_txt = lines[-1]
                if (not os.environ.get("M2_NOPB") and not re.match(r"^(-\s|#)", ft) and len(ft.split()) > 8 and ft[:1].isupper()
                        and (re.search(r"[.?!:)\"%]$", prev_txt.rstrip()) or prev_ind != para[0][0])):
                    para = [(para[0][0], PBREAK + ft)] + list(para[1:])
            lines.extend(para)
    return lines


def stem_paragraphs_to_markdown(paragraphs):
    """
    Stem ke paragraphs ko UNKI ASLI ORDER mein prose aur tables interleave
    karte hue ek markdown string banata hai -- taake table apni sahi jagah
    (reference jaisi) pe aaye, sabse end mein dhakel na jaye.
    """
    parts = []
    prose_buf = []
    run = []

    def flush_prose():
        if prose_buf:
            parts.append(" ".join(prose_buf))
            prose_buf.clear()

    def flush_run():
        if not run:
            return
        if is_noise_widget_table(run):
            # Discuss/Improve ya YouTube-suggestion widget hai -- poora
            # discard karo, prose mein bhi wapas mat daalo.
            run.clear()
            return
        table_md = build_table_md_from_run(run)
        if table_md:
            parts.append(table_md)
        else:
            for para in run:
                prose_buf.append(" ".join(t for _, t in para))
        run.clear()

    for para in paragraphs:
        if is_callout_paragraph(para):
            flush_run()
            flush_prose()
            parts.append(callout_to_blockquote_md(para))
        elif paragraph_is_table(para):
            flush_prose()
            run.append(para)
        else:
            flush_run()
            prose_buf.append(" ".join(t for _, t in para))
    flush_run()
    flush_prose()
    return "\n\n".join(parts).strip()


def is_noise_widget_table(run):
    """
    Kuch PassMedicine widgets (jaise "Discuss / Improve" video-suggestion
    box: channel-name + view-count + like-count columns) tabular text ki
    tarah dikhte hain isliye genuine table samajh liye jaate the. In cells
    ke end mein numbers (view/like count) hone ki wajah se line-level
    "-\\s*YouTube$" noise-pattern match nahi karta (line YouTube pe khatam
    nahi hoti, uske baad numbers hote hain). Isliye poore run ka combined
    text check karke aise widget-tables ko discard karte hain.
    """
    combined = " ".join(t for para in run for _, t in para).lower()
    has_discuss_improve = "discuss" in combined and "improve" in combined
    has_youtube_row = "youtube" in combined
    return has_discuss_improve or has_youtube_row


def extract_tables(paragraphs):
    """
    Notes ke paragraphs mein se table-jaisi consecutive paragraph-runs
    dhoondh kar unhe markdown tables mein convert karta hai. Returns
    (table_markdown_blocks, remaining_paragraphs) -- table wale
    paragraphs remaining se hata diye jaate hain taake baaki heading/
    bullet parsing pe asar na pade.
    """
    tables_md = []
    remaining = []
    run = []

    def flush():
        if not run:
            return
        if is_noise_widget_table(run):
            # Discuss/Improve ya YouTube-suggestion widget hai -- na table
            # ki tarah rakho, na hi remaining text mein wapas daalo (poora
            # discard karo, ye kabhi genuine content nahi hota).
            run.clear()
            return
        table_md = build_table_md_from_run(run)
        if table_md:
            tables_md.append(table_md)
        else:
            # sirf ek row thi -- table nahi tha, wapas normal paragraphs mein daal do
            remaining.extend(run)
        run.clear()

    for para in paragraphs:
        if paragraph_is_table(para):
            run.append(para)
        else:
            flush()
            remaining.append(para)
    flush()
    return tables_md, remaining


_ICON_PREFIX_RE = re.compile(r'^\[[A-Za-z]{3,14}[\]_]*\s+(?=[A-Z0-9])')


def clean_paragraph_lines(para_text):
    """Ek paragraph ki raw text ko cleaned (indent,text) lines mein todta hai."""
    cleaned = []
    seen_short_lines = {}
    # Page-join "\f" pichle page ki aakhri line ke AAKHIR mein, bina kisi
    # "\n" ke, agle page ki pehli line se seedha chaspa reh jaata hai
    # (ocr_layout_text pages ko "\f".join se jodta hai, beech mein newline
    # nahi hota) -- iski wajah se do alag (bullet) lines ek hi lambi line
    # ban jaati thi (bade space-gap ke sath), jo kabhi to agle bullet ko
    # pichle mein continuation bana deta tha, kabhi galti se "table"
    # detect ho jaata tha (paragraph_is_table bade gaps ko column samajhta
    # hai). \f ko newline se replace karke dono hisson ko wapas do alag
    # lines bana dete hain taake har ek ki apni sahi indent/text mile.
    para_text = para_text.replace("\f", "\n")
    for line in para_text.split("\n"):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(' '))
        stripped = line.strip()
        if any(re.search(pat, stripped, re.IGNORECASE) for pat in NOISE_LINE_PATTERNS):
            continue
        _pg = _pdfgeo_module()
        # MCQ option-lines ("Ibuprofen  19%") kabhi _garbled_improve ke
        # fuzzy-match mein galti se phans jaati hain (jaise "ibuprofen"
        # vs "improve" ~62% similar) -- lekin koi bhi icon/button garbage
        # line percentage ke sath khatam nahi hoti, is liye aisi lines ko
        # junk-check se bilkul exempt rakhte hain.
        _is_option_line = bool(re.search(r'\d{1,3}\s*%\s*$', stripped))
        if _pg is not None and not _is_option_line and (_pg._junk_line(stripped) or _pg._is_next_q(stripped)):
            continue
        # Icon-row OCR garbage kabhi agli heading/line ke bilkul SAME row
        # pe parh li jaati hai (jaise '[newsuson Lichen planus') -- poori
        # line junk nahi lagti (_junk_line) kyunki asal heading text bhi
        # saath mein hai, isliye sirf garbage bracket-prefix hatate hain.
        stripped = _ICON_PREFIX_RE.sub('', stripped)
        for pat in SUBSTRING_NOISE_PATTERNS:
            stripped = re.sub(pat, '', stripped, flags=re.IGNORECASE).strip()
        if not stripped:
            continue
        # related-topic tag-list widgets (jaise "Multiple sclerosis" 2-3 baar
        # repeat hona) short standalone lines ki tarah dikhte hain -- pehli
        # baar rakhte hain, dobara wahi exact line aaye to widget-noise
        # samajh kar hata dete hain (genuine prose itni exact repetition
        # nahi karta).
        key = stripped.lower()
        if len(stripped.split()) <= 6:
            seen_short_lines[key] = seen_short_lines.get(key, 0) + 1
            if seen_short_lines[key] > 1:
                continue
        cleaned.append((indent, stripped))
    return cleaned


def _looks_like_broken_sentence(prev_para, next_para):
    """
    True agar prev_para ka aakhri line aur next_para ka pehla line asal
    mein EK hi sentence ke beech mein galti se toot gaye hain (PDF text-
    layer mein stray blank line -- jaise page/column boundary ki wajah
    se). Signal: prev sentence-ending punctuation pe khatam nahi hota,
    AND next lowercase letter se shuru hota hai (heading/bullet/naya
    sentence hota to capital letter ya number se shuru hota).
    """
    if not prev_para or not next_para:
        return False
    prev_text = prev_para[-1][1].strip()
    next_text = next_para[0][1].strip()
    if not prev_text or not next_text:
        return False
    if prev_text[-1] in '.?!:"\')]':
        return False
    if not next_text[0].islower():
        return False
    return True


def merge_broken_paragraphs(paragraphs_with_offsets):
    """
    Consecutive paragraphs ko wapas jod deta hai jab wo ek stray mid-
    sentence blank-line ki wajah se galti se do paragraphs ban gaye hon
    (see _looks_like_broken_sentence). Offset pehle wale paragraph ka hi
    rakha jaata hai.
    """
    merged = []
    for cleaned, offset in paragraphs_with_offsets:
        if merged and _looks_like_broken_sentence(merged[-1][0], cleaned):
            prev_cleaned, prev_offset = merged[-1]
            merged[-1] = (prev_cleaned + cleaned, prev_offset)
        else:
            merged.append((cleaned, offset))
    return merged


def paragraphs_from_text_with_offsets(raw_text):
    """
    paragraphs_from_text() jaisa hi, bas har paragraph ke saath uski
    raw_text ke andar shuru hone ki char-position bhi deta hai -- taake
    baad mein us paragraph ka asal PDF page number pata chal sake (diagram
    ko uske sahi notes-paragraph ke paas rakhne ke liye).
    Returns: list of (paragraph_lines, start_offset).
    """
    result = []
    pos = 0
    for m in re.finditer(r'\n\s*\n', raw_text):
        para_text = raw_text[pos:m.start()]
        cleaned = clean_paragraph_lines(para_text)
        if cleaned:
            result.append((cleaned, pos))
        pos = m.end()
    para_text = raw_text[pos:]
    cleaned = clean_paragraph_lines(para_text)
    if cleaned:
        result.append((cleaned, pos))

    # Related-topic tag-list widgets (jaise "Multiple sclerosis" baar baar,
    # har baar apne alag single-line paragraph mein) -- clean_paragraph_lines
    # ka dedup sirf EK paragraph ke andar kaam karta hai, isliye cross-
    # paragraph repeats yahan hataate hain: agar koi paragraph sirf EK
    # chhoti (<=6 words) line par mushtamil ho aur wahi exact text pehle
    # bhi kisi standalone single-line paragraph mein aa chuki ho, to usay
    # duplicate widget-noise samajh kar hata dete hain.
    result = merge_broken_paragraphs(result)

    seen_standalone = set()
    deduped = []
    for cleaned, offset in result:
        if len(cleaned) == 1 and len(cleaned[0][1].split()) <= 6:
            key = cleaned[0][1].strip().lower()
            if key in seen_standalone:
                continue
            seen_standalone.add(key)
        deduped.append((cleaned, offset))
    return deduped


def paragraphs_from_text(raw_text):
    """
    Raw text ko blank-line paragraph-boundaries ke hisaab se todta hai (noise
    lines hata kar), taake explanation ke paragraphs aur unke beech ka gap
    save rahe -- isse explanation ek hi giant block banne se bachta hai.
    Har line (indent, text) tuple hai -- indent = leading space count, jo
    baad mein notes-section ki heading/bullet/sub-bullet hierarchy nikalne
    ke liye use hota hai (bullet characters jaise •/○ is PDF export mein
    pdftotext se gayab ho jate hain, sirf indentation hi reliable signal
    bachta hai).
    Returns: list of paragraphs, each paragraph ek list of (indent, text).
    """
    return [p for p, _ in paragraphs_from_text_with_offsets(raw_text)]


# Capital "I" OCR mein kabhi "l"/"ll" ban jaata hai (font-rendering
# ambiguity) -- ye chand bohot common exam/drug terms hain jo isi wajah se
# baar baar galat parse hote hain. Sirf exact (case-insensitive) match pe
# fix karte hain taake genuinely-lowercase medical terms (jaise "c-ANCA")
# na chhide.
_OPTION_OCR_FIXES = {
    "infliximab": "Infliximab",
    "lliopsoas": "Iliopsoas",
    "lliopsoas abscess": "Iliopsoas abscess",
    "ct": "CT",
}


def _fix_option_ocr_case(text):
    return _OPTION_OCR_FIXES.get(text.strip().lower(), text)


def parse_question_paragraphs(paragraphs, paragraph_offsets=None):
    """
    Text-mode (real PDF text) ke liye paragraph-aware parser -- OCR-mode
    wale parse_question_block() se alag isliye hai kyunki yahan blank-line
    paragraph boundaries reliably milti hain (real text-layer PDF se), jo
    explanation ko multi-paragraph rakhne aur reference-notes ko generically
    alag karne ke liye use hoti hain.
    """
    option_start = None
    options = []
    i = 0
    ISOLATED_PCT_RE = re.compile(r'^(\d{1,3})\s*%$')

    # PassMedicine-2 FIX #0: page-break ka footer (URL, timestamp, "1/7",
    # "PassMedicine", akela "PM"/"AM") options ke BEECH mein aa jata hai aur
    # option-run ko adhoora tod deta tha (jaise 5 mein se sirf 3 options).
    # Ye lines paragraphs se hata dete hain (offsets align rakhte hue).
    _PAGE_NOISE_RES = [
        re.compile(r'passmedicine\.com', re.I),
        re.compile(r'^\s*https?:/', re.I),
        re.compile(r'^\s*\d{1,2}/\d{1,2}/\d{2,4},?\s*\d{1,2}:\d{2}'),
        re.compile(r'^\s*PassMedicine\s*$'),
        re.compile(r'^\s*\d{1,2}\s*/\s*\d{1,2}\s*$'),
        re.compile(r'^\s*(AM|PM)\s*$'),
    ]
    _clean_paras, _clean_offs = [], []
    for _pi, _para in enumerate(paragraphs):
        _kept = [(ind, t) for ind, t in _para if not any(rx.search(t) for rx in _PAGE_NOISE_RES)]
        if _kept:
            _clean_paras.append(_kept)
            _clean_offs.append(paragraph_offsets[_pi] if paragraph_offsets is not None else None)
    paragraphs = _clean_paras
    if paragraph_offsets is not None:
        paragraph_offsets = _clean_offs

    # PassMedicine-2 FIX #1: kabhi kabhi saare options ek hi paragraph mein
    # aa jaate hain (har line = ek option, "a wave   9%"). Aise paragraph ko
    # per-line split karte hain, warna poora block ek option ban jata hai.
    # paragraph_offsets ko bhi sath sath align rakhte hain.
    _LINE_OPT_RE = re.compile(r'^\s*\S.*?\s+\d{1,3}\s*%\s*$')
    _new_paras, _new_offs = [], []
    for _pi, _para in enumerate(paragraphs):
        _off = paragraph_offsets[_pi] if paragraph_offsets is not None else None
        if len(_para) >= 2 and all(_LINE_OPT_RE.match(t) for _, t in _para):
            for _line in _para:
                _new_paras.append([_line])
                _new_offs.append(_off)
        else:
            _new_paras.append(_para)
            _new_offs.append(_off)
    paragraphs = _new_paras
    if paragraph_offsets is not None:
        paragraph_offsets = _new_offs

    _trim_info = {"start": None}

    def _trim_option_run(opts):
        # PassMedicine-2 FIX #2: options ke turant baad ka "key fact" box
        # agar "...< 40%" par khatam ho to wo ek jhoota 6th option ban jata
        # tha, sum >120 hota tha aur POORA block reject ho jata tha.
        # Ab trailing items tab tak hataate hain jab tak sum ~100 na ho.
        # Q318 FIX: stem ki aakhri line "...giving 100%" ban gayi pehla fake option (100%) --
        # pehle leading fake item hatao agar uske baad sum ~100 ban jaye.
        _trim_info["start"] = None
        while (len(opts) > 3 and sum(o["percentage"] for o in opts) > 104
               and (opts[0]["percentage"] >= 90 or len(opts[0]["text"]) > 140)
               and 96 <= sum(o["percentage"] for o in opts[1:]) <= 104):
            _trim_info["start"] = opts[0]["_end"]
            opts = opts[1:]
        while len(opts) > 2 and sum(o["percentage"] for o in opts) > 104:
            opts = opts[:-1]
        # trailing "key fact" box jo ":"/">="/"<" rakhta ho (jaise "Anticoagulation
        # should be considered for the following: Men: CHA2DS2-VASC >= 1 ... 2")
        while (len(opts) > 2 and sum(o["percentage"] for o in opts) >= 101
               and re.search(r':|>=|<=', opts[-1]["text"]) and len(opts[-1]["text"].split()) >= 6):
            opts = opts[:-1]
        # Q20 FIX: aakhri "option" asal mein explanation ka sentence ho (pct nahi, '.' pe khatam)
        while (len(opts) > 3 and opts[-1]["text"].rstrip().endswith('.') and len(opts[-1]["text"].split()) >= 8
               and 90 <= sum(o["percentage"] for o in opts[:-1]) <= 104):
            opts = opts[:-1]
        return opts

    while i < len(paragraphs):
        para_lines = paragraphs[i]
        matched = False
        if len(para_lines) <= 5:
            texts = [t for _, t in para_lines]
            # Case 1: percentage sits on its OWN line within the paragraph
            # (option text wraps across 2-3 lines, % ke apni line pe, kabhi
            # kabhi beech mein hi -- jaise "text\n11%\nmore text")
            pct_idx = next(
                (idx for idx, t in enumerate(texts) if ISOLATED_PCT_RE.match(t.strip())),
                None
            )
            if pct_idx is not None:
                pct = int(ISOLATED_PCT_RE.match(texts[pct_idx].strip()).group(1))
                option_text = " ".join(t for idx, t in enumerate(texts) if idx != pct_idx).strip()
                if option_text and len(option_text) < 300:
                    option_start = option_start if option_start is not None else i
                    options.append({"text": option_text, "percentage": pct, "_end": i + 1})
                    matched = True
            else:
                # Case 2: option text + "%" together at the very end of the
                # joined paragraph (single-line options, jaise Q1 format)
                joined = " ".join(texts)
                m = OPTION_PCT_RE.match(joined)
                # Mnemonic/callout box (jaise "Golimumab is a TNF inhibitor"
                # ya "Denosumab - RANKL inhibitor") options ke turant baad
                # aati hai aur kabhi stray percentage uske saath chaspa ho
                # jaata hai (nearby star/bookmark icon se). Signal: ye poora
                # sentence (is/are/was/were) ya "label - description"/
                # "label: description" format mein hoti hai, AUR pehle se
                # collect hui options ka sum pehle hi ~100% (complete set)
                # ban chuka hota hai -- dono mil kar pakka signal hain ke ye
                # naya option nahi, callout hai.
                _looks_like_callout = m and (
                    re.search(r'\b(is|are|was|were)\b', m.group(1))
                    or re.search(r'\s[-:]\s', m.group(1))
                )
                _is_extra_callout = (
                    _looks_like_callout and options
                    and 90 <= sum(o["percentage"] for o in options) <= 110
                )
                if m and len(joined) < 300 and not _is_extra_callout:
                    option_start = option_start if option_start is not None else i
                    options.append({"text": m.group(1).strip(), "percentage": int(m.group(2)), "_end": i + 1})
                    matched = True
                else:
                    # Case 3 (PassMedicine-2): wrapped option jis ka "NN%"
                    # kisi line ke AKHIR mein hai (beech wali line), baqi
                    # lines sirf text ka wrap hain.
                    for _li, _t in enumerate(texts):
                        _m3 = re.search(r'\s(\d{1,3})\s*%\s*$', _t)
                        if _m3 and len(joined) < 300 and len(texts) >= 2:
                            _rest = texts[:_li] + [_t[:_m3.start()]] + texts[_li + 1:]
                            _txt = " ".join(x.strip() for x in _rest if x.strip())
                            # Asal option kabhi multi-sentence prose nahi
                            # hoti -- agar beech mein ek pura sentence-
                            # boundary (". Capital") mil jaye to ye
                            # explanation ka paragraph hai jisme kahin
                            # beech mein ek percentage line ke aakhir mein
                            # aa gaya (jaise "...sclerosis. Around 2% of
                            # women and 6%"), genuine option nahi.
                            if _txt and re.search(r'\.\s+[A-Z]', _txt):
                                break
                            if _txt:
                                option_start = option_start if option_start is not None else i
                                options.append({"text": _txt, "percentage": int(_m3.group(1)), "_end": i + 1})
                                matched = True
                            break
        if matched:
            i += 1
            continue
        if option_start is not None and options and i == options[-1].get("_end"):
            # Continuation: option ka wrapped aakhri lafz/phrase alag paragraph
            # mein gir gaya ("...is to be 71%" / blank / "performed")
            _ct = " ".join(t.strip() for _, t in paragraphs[i])
            if (_ct and len(_ct.split()) <= 8 and _ct[0].islower()
                    and not _ct.rstrip().endswith('.') and '%' not in _ct):
                options[-1]["text"] = (options[-1]["text"] + " " + _ct).strip()
                paragraphs[i] = []          # consumed
                options[-1]["_end"] = i + 1
                i += 1
                continue
        if option_start is not None:
            # Candidate option-block ek non-matching paragraph pe khatam
            # hui -- lekin accept karne se pehle validate karte hain ke ye
            # GENUINE MCQ options hain, na ke stem ke andar koi data-table
            # (jaise cardiac catheterisation oxygen-saturation results)
            # jo sirf "text + NN%" ki shape match kar gaya ho. Genuine
            # option percentages hamesha ~100% ke close sum hote hain
            # (test-takers ka response-distribution hota hai); ek stem
            # data-table ka sum kuch bhi ho sakta hai (jaise 71+82+81=234,
            # jo clearly options nahi thay balke investigation results
            # thay). Agar sum 100 ke aas-paas nahi hai, to ye ek false
            # positive tha -- discard karke aage scan jaari rakhte hain,
            # taake asal options block (jo aage kahin ho sakta hai) miss
            # na ho.
            options = _trim_option_run(options)
            if _trim_info["start"] is not None:
                option_start = _trim_info["start"]
            pct_sum = sum(o["percentage"] for o in options)
            if len(options) >= 2 and 65 <= pct_sum <= 135:
                # i ko trimmed run ke turant baad point karo (jo paragraphs
                # trim hue wo explanation/key-fact side mein wapas chale jayen)
                i = options[-1]["_end"]
                break
            option_start = None
            options = []
        i += 1

    # Paragraphs poori list khatam hone tak bhi pohonch sakta hai (agar
    # "options" PDF ke bilkul end tak chalti hain) -- is case mein bhi
    # wahi ~100% validation lagani zaroori hai, warna ek galat-matched
    # data-table poora "stem" nigal jayega bina kabhi reject huye.
    if option_start is not None:
        options = _trim_option_run(options)
        if _trim_info["start"] is not None:
            option_start = _trim_info["start"]
        pct_sum = sum(o["percentage"] for o in options)
        if not (len(options) >= 2 and 65 <= pct_sum <= 135):
            option_start = None
            options = []
        else:
            i = options[-1]["_end"]

    for _o in options:
        _o.pop("_end", None)
        _o["text"] = _fix_option_ocr_case(_o["text"])
    stem_paragraphs = paragraphs[:option_start] if option_start is not None else paragraphs
    # kabhi kabhi stem ke andar hi ek data-table hoti hai (jaise lab
    # values: Na+, K+, Urea, Creatinine) -- usko generic table-parser se
    # extract karke stem ke end mein ek markdown table ki tarah jodte hain
    stem = stem_paragraphs_to_markdown(stem_paragraphs)

    remainder_paragraphs = paragraphs[i:]
    remainder_offsets = paragraph_offsets[i:] if paragraph_offsets is not None else [None] * len(remainder_paragraphs)

    # Options ke turant baad kabhi kabhi ek chhota "key fact" box hota hai
    # (jaise "Newly diagnosed patient with hypertension (> 55 years) - add
    # a calcium channel blocker") -- ye Water-deprivation-test wale box se
    # alag hai (koi colon+indented-points structure nahi, sirf ek chhota
    # flowing sentence) isliye alag se detect karte hain, sirf pehli
    # paragraph pe (taake normal explanation paragraphs is se na tootein).
    if remainder_paragraphs:
        first_para = remainder_paragraphs[0]
        first_joined = " ".join(t for _, t in first_para)
        first_words = first_joined.split()
        if (
            1 <= len(first_words) <= 30
            and not first_joined.lower().startswith('the correct answer')
            and not re.search(r'\bis\s+(in)?correct\b', first_joined, re.IGNORECASE)
            and not is_callout_paragraph(first_para)
        ):
            # single-line callout (bullet points ke bina) ki tarah mark
            # karte hain -- explanation-builder ise blockquote ki tarah render karega
            remainder_paragraphs = (
                [[(first_para[0][0], "\x03KEYFACT\x03" + first_joined)]] + remainder_paragraphs[1:]
            )

    # Generic explanation-vs-notes split: after the first explanation
    # paragraph, a short heading-like paragraph (few words, no ending
    # period, not talking about "correct"/"incorrect") signals the start
    # of a reference-notes block (e.g. "Murmurs", "ECG changes", etc.)
    notes_start_idx = None
    # PassMedicine-2: authoritative notes-start sentinel (pehli "Next question")
    for idx, para_lines in enumerate(remainder_paragraphs):
        if " ".join(t.strip() for _, t in para_lines) == NOTES_START_TOKEN:
            remainder_paragraphs = remainder_paragraphs[:idx] + remainder_paragraphs[idx + 1:]
            remainder_offsets = remainder_offsets[:idx] + remainder_offsets[idx + 1:]
            notes_start_idx = idx if idx < len(remainder_paragraphs) else None
            break
    for idx, para_lines in enumerate(remainder_paragraphs):
        if notes_start_idx is not None:
            break
        if idx == 0:
            continue
        joined = " ".join(t for _, t in para_lines)
        words = joined.split()
        looks_like_heading = (
            len(words) <= 4
            and not joined.rstrip().endswith('.')
            and 'incorrect' not in joined.lower()
            and 'correct answer' not in joined.lower()
        )
        if looks_like_heading:
            notes_start_idx = idx
            break

    if notes_start_idx is not None:
        explanation_paragraphs = remainder_paragraphs[:notes_start_idx]
        notes_paragraphs = remainder_paragraphs[notes_start_idx:]
        notes_offsets = remainder_offsets[notes_start_idx:]
    else:
        explanation_paragraphs = remainder_paragraphs
        notes_paragraphs = []
        notes_offsets = []

    explanation = explanation_paragraphs_to_markdown(explanation_paragraphs)
    # notes_paragraphs (offsets ke saath) -- caller (jaise diagram-placement
    # logic) ko diye jaate hain BEFORE table-extraction, taake wo apni
    # marzi se in paragraphs ko augment kar sake (jaise image-markers
    # insert karna) aur phir khud extract_tables + notes_paragraphs_to_lines
    # dobara chala sake
    notes_paragraphs_with_offsets = list(zip(notes_paragraphs, notes_offsets))
    notes_tables_md, notes_paragraphs = extract_tables(notes_paragraphs)
    notes_lines = notes_paragraphs_to_lines(notes_paragraphs)

    correct_answer = None
    correct_source = None
    if options:
        # PassMedicine-2 FIX #3: "sab se zyada % wala = sahi jawab" galat
        # hota hai (kayi sawalon mein ghalat option zyada chuna jata hai).
        # Pehle explanation ke text se sahi jawab nikalo; ye na mile to
        # hi max-% fallback. (Green-bar override caller mein lagta hai.)
        idx = detect_correct_from_explanation(options, explanation)
        if idx is not None:
            correct_source = "explanation"
        else:
            idx = max(range(len(options)), key=lambda k: options[k]["percentage"])
            correct_source = "max_percentage"
        correct_answer = options[idx]["text"]
        for k, o in enumerate(options):
            o["correct"] = (k == idx)

    return {
        "correct_source": correct_source,
        "text": stem,
        "options": options,
        "correct_answer": correct_answer,
        "explanation_raw": explanation,
        "notes_lines": notes_lines,
        "notes_tables_md": notes_tables_md,
        "notes_paragraphs_with_offsets": notes_paragraphs_with_offsets,
    }


def _norm_opt(s):
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def detect_correct_from_explanation(options, explanation):
    """
    PassMedicine explanation hamesha sahi jawab batati hai:
      "The correct answer is X."  /  "X is correct."  /  "X is the correct answer."
    Option text se normalised match karke index return karta hai (ya None).
    """
    if not explanation:
        return None
    text = re.sub(r'^>.*$', '', explanation, flags=re.M)   # callout box hatao
    text = text.replace('**', '')
    cands = []
    for m in re.finditer(r'The\s+correct\s+answer\s+is[:\s]+(.+?)(?:\.\s|\.$|\n)', text, re.I):
        cands.append(m.group(1))
    for m in re.finditer(r'(?:^|\n|\.\s)([^\n.]{2,200}?)\s+is\s+(?:the\s+)?correct(?:\s+answer)?\b(?!\s*ly)', text, re.I):
        if not re.search(r'\bin$', m.group(1).strip().lower()):
            cands.append(m.group(1))
    norm_opts = [_norm_opt(o["text"]) for o in options]
    for cand in cands:
        nc = _norm_opt(cand)
        if not nc:
            continue
        hits = [k for k, no in enumerate(norm_opts) if no == nc]
        if not hits:
            hits = [k for k, no in enumerate(norm_opts) if no and (nc.startswith(no) or no.startswith(nc))]
        if len(hits) == 1:
            return hits[0]
    return None


def green_bar_correct_index(pdf_doc, start_page, end_page, options):
    """
    Option-row ke saath PDF mein ek green vector bar (fill ~ (0,0.5,0)) hota
    hai jo SAHI option ko mark karta hai -- ye sab se bharosemand signal hai.
    Return: option index ya None (bar na mile / ambiguous ho).
    """
    if pdf_doc is None or not options:
        return None
    pct_re = re.compile(r'^(\d{1,3})%$')
    for pn in range(max(0, start_page - 1), min(len(pdf_doc), end_page)):
        page = pdf_doc[pn]
        bars = []
        for dr in page.get_drawings():
            f = dr.get("fill")
            r = dr["rect"]
            if f and len(f) == 3 and 20 < r.height < 70 and 0.40 < f[1] < 0.60 and f[0] < 0.1 and f[2] < 0.1:
                bars.append(r)
        if not bars:
            continue
        words = [w for w in page.get_text("words") if pct_re.match(w[4])]
        for r in bars:
            inside = [w for w in words if r.y0 - 2 <= (w[1] + w[3]) / 2 <= r.y1 + 2]
            if len(inside) != 1:
                continue
            p = int(pct_re.match(inside[0][4]).group(1))
            same = [k for k, o in enumerate(options) if o["percentage"] == p]
            if len(same) == 1:
                return same[0]
            if same:
                same_words = sorted([w for w in words if w[4] == inside[0][4]], key=lambda w: w[1])
                rank = same_words.index(inside[0]) if inside[0] in same_words else 0
                if rank < len(same):
                    return same[rank]
    return None


def extract_options_geometry(pdf_doc, start_page, end_page):
    """
    PassMedicine-2 FIX #4 (fallback + cross-check): option rows ko PDF ke
    word-coordinates se nikalta hai (text-order par bharosa nahi) -- wrapped
    options ("...to be\n71%\nperformed") aur page-break ke beech phansi
    options ke liye. Har "NN%" word ek option hai; text = us row ke band
    ke andar ke saare words (% column ke baayen taraf).
    Return: [{"text","percentage"}] ya None.
    """
    if pdf_doc is None:
        return None
    pct_re = re.compile(r'^(\d{1,3})%$')
    pts = []   # (global_y, pct, page_no)
    words_all = []
    for pn in range(max(0, start_page - 1), min(len(pdf_doc), end_page)):
        page = pdf_doc[pn]
        W = page.get_text("words")
        base = pn * 10000.0
        for w in W:
            gy = base + (w[1] + w[3]) / 2
            words_all.append((gy, w[0], w[2], w[4]))
            if pct_re.match(w[4]) and w[0] > 400:
                pts.append((gy, int(pct_re.match(w[4]).group(1))))
    pts.sort()
    # pehli window jis ka sum ~100 ho (2..8 options, same-question rows)
    best = None
    for lo_ok, hi_ok in ((97, 103), (95, 105)):
        for a in range(len(pts)):
            tot = 0
            cand = None
            for b in range(a, min(len(pts), a + 8)):
                tot += pts[b][1]
                if tot > hi_ok:
                    break
                if tot >= lo_ok and b - a + 1 >= 2:
                    cand = pts[a:b + 1]      # pehli valid window (aage ke % explanation ke ho sakte hain)
                    break
            if cand:
                best = cand
                break
        if best:
            break
    if not best:
        return None
    ys = [y for y, _ in best]
    opts = []
    for k, (y, pct) in enumerate(best):
        lo = (ys[k - 1] + y) / 2 if k > 0 else y - 30
        hi = (y + ys[k + 1]) / 2 if k + 1 < len(ys) else y + 30
        row = [w for w in words_all if lo < w[0] <= hi and w[2] < 470 and not pct_re.match(w[3])]
        # same visual line ke words x se, lines y se sort
        row.sort(key=lambda w: (round(w[0] / 6), w[1]))
        text = " ".join(w[3] for w in row).strip()
        opts.append({"text": text, "percentage": pct})
    return opts


def compute_continuation_lines(pdf_doc, start_page, end_page):
    """
    PassMedicine-2 FIX #6: notes ke bullets PDF mein glyph nahi balki chhote
    VECTOR CIRCLES hain, isliye text-layer mein "naya bullet" aur "pichli
    line ka wrap" ek jaise dikhte hain. Yahan geometry se wo lines nikalte
    hain jinke baayen taraf bullet-circle NAHI hai aur jo seedha kisi
    bullet/continuation ke neeche usi text-indent par hain => wrap lines.
    Return: set of whitespace-normalised line texts.
    """
    out = set()
    if pdf_doc is None:
        return out
    norm = lambda t: re.sub(r"\s+", " ", t).strip()
    for pn in range(max(0, start_page - 1), min(len(pdf_doc), end_page)):
        page = pdf_doc[pn]
        bullets = []
        for dr in page.get_drawings():
            r = dr["rect"]
            if 2 <= r.width <= 7 and 2 <= r.height <= 7 and r.x0 < 260 and r.y0 > 40:
                bullets.append(r)
        lines = []
        for blk in page.get_text("dict")["blocks"]:
            for ln in blk.get("lines", []):
                txt = "".join(sp["text"] for sp in ln["spans"])
                if not txt.strip():
                    continue
                x0, y0, x1, y1 = ln["bbox"]
                if y0 < 40 or y1 > 805:
                    continue
                size = max(sp["size"] for sp in ln["spans"])
                lines.append((y0, y1, x0, txt, size))
        lines.sort()
        in_list, list_x = False, None
        prev = None   # (y1, x0, kind) kind: 'h' heading, 'b' bullet/list, 'p' prose
        for y0, y1, x0, txt, size in lines:
            has_b = any(b.x1 <= x0 + 1 and x0 - b.x1 < 22 and y0 - 2 <= (b.y0 + b.y1) / 2 <= y1 + 2 for b in bullets)
            gap = (y0 - prev[0]) if prev else 999
            kind = None
            if size >= 15:                       # notes ka bada heading (wrapped ho sakta hai)
                kind = "h"
                if prev and prev[2] == "h" and 0 <= gap < 9 and abs(x0 - prev[1]) < 4:
                    out.add(norm(txt))
                in_list = False
            elif has_b:
                kind = "b"
                in_list, list_x = True, x0
            elif in_list and abs(x0 - list_x) < 4 and 0 <= gap < 7:
                kind = "b"
                out.add(norm(txt))
            else:
                in_list = False
                kind = "p"
                # prose paragraph ki wrapped line: pichli line bhi prose, same indent, tight gap
                if prev and prev[2] == "p" and abs(x0 - prev[1]) < 4 and 0 <= gap < 7:
                    out.add(norm(txt))
            prev = (y1, x0, kind)
    return out


def merge_wrapped_bullet_lines(chunk, cont_set):
    """cont_set ki lines ko pichli (bina blank-line wali) line mein jod do.
    Length preserve karta hai (char offsets/page-mapping na bigdein)."""
    if not cont_set:
        return chunk
    lines = chunk.split("\n")
    out = []
    for ln in lines:
        key = re.sub(r"\s+", " ", ln).strip()
        if key and key in cont_set and out and out[-1].strip():
            indent = len(ln) - len(ln.lstrip())
            out[-1] = out[-1].rstrip() + " " + ln.strip() + " " * (indent + 1 - 1)
        else:
            out.append(ln)
    return "\n".join(out)


_SCORE_ROW_RE = re.compile(r'^\d{1,3}[.\s]*[&xX]{1,3}\s*$')
# Link ki trailing token agar digit ke sath koi ajeeb symbol (&,%,*) rakhti
# hai, ya letters+digits ka random mix hai (jaise 'o798', 'A2Q7', '4493'),
# to wo icon/vote-count OCR garbage hai -- genuine words/titles mein aisi
# combination practically kabhi nahi aati. Asal 4-digit saal (2017, 2018)
# ko bachaya gaya hai taake titles jaise "2018 guidelines" na toote.
_NOT_A_YEAR = r'(?!(?:19|20)\d{2}$)'
_LINK_JUNK_TOKEN_RE = (
    r'^([A-Z]{1,2}\d*|\d{1,3}|' + _NOT_A_YEAR + r'\d{4}|\S*[@~+()&%*]\S*'
    r'|' + _NOT_A_YEAR + r'(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{2,8})$'
)
_KNOWN_LINK_SOURCES = ["DermNet NZ", "Primary Care Dermatology Society",
                       "British Association of Dermatologists"]
_LINK_SOURCE_FIX_RES = [(re.compile(r'^(' + re.escape(name) + r')\b.*$', re.I), name)
                        for name in _KNOWN_LINK_SOURCES]


def _normalize_link_source(l):
    """Thumbs-up/down icon + vote-count (jaise little green/red icons 11/4)
    link-source line ke SAME row pe hote hain aur OCR unhe us line ke
    akhir mein chaspa kar deta hai -- har baar vote-count alag hone ki
    wajah se garbage bhi har baar alag hota hai ('DermNet NZ Au gs',
    'Primary Care Dermatology Society Ax. oes', ...). Known source names
    ko canonical form pe fix karte hain."""
    stripped = l.strip()
    for rx, name in _LINK_SOURCE_FIX_RES:
        if rx.match(stripped):
            return name
    return l


def extract_tail_metadata(chunk):
    """
    Question ke aakhir mein "Textbooks / MRCP 1 Content Map / Links" widgets
    hote hain -- ye notes nahi balki metadata hain. Inhe chunk se kaat kar
    structured dict mein return karta hai.
    Return: (chunk_without_tail, {"content_map":{...}, "links":[...]})
    """
    meta = {"content_map": {}, "links": []}
    m = re.search(r'^[ \t]*MRCP\s*\d*\s*Content\s*Map\s*$', chunk, re.M | re.I)
    if not m:
        # OCR PDFs: "MRCP 1 Content Map" heading icons ki wajah se garbled ("B+ Qo ...") -- Textbooks..Links se pehchano
        m2 = re.search(r'^[ \t]*Textbooks\s*$', chunk, re.M)
        if m2 and (re.search(r'^[ \t]*Links\s*$', chunk[m2.end():], re.M)
                   or re.search(r'High-yield textbook', chunk[m2.end():m2.end() + 200])):
            ls = chunk[:m2.start()].split("\n")
            _pg = _pdfgeo_module()
            while ls and (not ls[-1].strip() or (len(ls[-1].split()) <= 8 and all(len(t) <= 3 for t in ls[-1].split()))
                          or (_pg is not None and _pg._junk_line(ls[-1].strip()))):
                ls.pop()
            head = "\n".join(ls) + "\n"
            lines = [l.strip() for l in chunk[m2.start():].split("\n") if l.strip()]
            junk = re.compile(r'passmedicine\.com|^\d{1,2}/\d{1,2}/\d{2,4}|^PassMedicine$|^\d+\s*/\s*\d+$|^(AM|PM)$|^Suggest link|^Report broken link|^Media$', re.I)
            lk = [k for k, l in enumerate(lines) if re.match(r'^Links$', l, re.I)]
            buf = []
            for l in (lines[lk[0] + 1:] if lk else []):
                if re.match(r'^(Media|Report broken media)\b', l):
                    break
                if junk.search(l) or re.fullmatch(r'[\d\s]+', l) or re.match(r'^Score:', l) or _SCORE_ROW_RE.match(l):
                    continue
                toks = l.split()
                while len(toks) > 1 and re.match(_LINK_JUNK_TOKEN_RE, toks[-1]):
                    toks.pop()
                l = _normalize_link_source(" ".join(toks).strip())
                if l and re.search(r'[A-Za-z]{3,}', l):
                    buf.append(l)
            if len(buf) % 2 == 0:
                meta["links"] = [{"source": buf[k], "title": buf[k + 1]} for k in range(0, len(buf), 2)]
            else:
                meta["links"] = [{"source": None, "title": b} for b in buf]
            return head, meta
        return chunk, meta
    cut = m.start()
    tb = list(re.finditer(r'^[ \t]*Textbooks\s*$', chunk[:cut], re.M))
    if tb and cut - tb[-1].start() < 400:
        cut = tb[-1].start()
    tail = chunk[m.start():]
    head = chunk[:cut]
    lines = [l.strip() for l in tail.split("\n") if l.strip()]
    key = None
    i = 1
    while i < len(lines):
        l = lines[i]
        if re.match(r'^(Suggest better categorisation|Links)$', l, re.I):
            break
        if re.match(r'^[A-Z][A-Z /&-]{3,}$', l):          # PRESENTATION / CONDITION / ...
            key = l.lower()
            meta["content_map"].setdefault(key, [])
        elif key and not re.search(r'passmedicine\.com|^\d{1,2}/\d{1,2}/\d{2,4}|^PassMedicine$|^\d+\s*/\s*\d+$|^(AM|PM)$', l):
            meta["content_map"][key].append(l)
        i += 1
    lk = [k for k, l in enumerate(lines) if re.match(r'^Links$', l, re.I)]
    if lk:
        buf = []
        junk = re.compile(r'passmedicine\.com|^\d{1,2}/\d{1,2}/\d{2,4}|^PassMedicine$|^\d+\s*/\s*\d+$|^(AM|PM)$|^Suggest link|^Report broken link', re.I)
        for l in lines[lk[0] + 1:]:
            if re.match(r'^(Media|Report broken media)\b', l):
                break
            if junk.search(l) or re.fullmatch(r'[\d\s]+', l) or re.match(r'^Score:', l) or _SCORE_ROW_RE.match(l):
                continue
            l = re.sub(r'(\s{2,}\d+)+\s*$', '', l).strip()   # "NICE    4   5" -> "NICE"
            toks = l.split()
            while len(toks) > 1 and re.match(_LINK_JUNK_TOKEN_RE, toks[-1]):
                toks.pop()
            l = _normalize_link_source(" ".join(toks).strip())
            if l and re.search(r'[A-Za-z]{3,}', l):
                buf.append(l)
        if len(buf) % 2 == 0:
            meta["links"] = [{"source": buf[k], "title": buf[k + 1]} for k in range(0, len(buf), 2)]
        else:
            meta["links"] = [{"source": None, "title": b} for b in buf]
    return head, meta


def _cell_txt(c):
    return re.sub(r"\s+", " ", (c or "").replace("\n", " ")).strip()


def extract_page_tables(pdf_doc, start_page, end_page):
    """PDF ki asli tables (find_tables, 'lines' strategy). UI-widget/icon tables hata deta hai.
    Return: list of {"rows": [[cell..]], "page": n}"""
    out = []
    if pdf_doc is None:
        return out
    for pn in range(max(0, start_page - 1), min(len(pdf_doc), end_page)):
        try:
            tf = pdf_doc[pn].find_tables(strategy="lines")
        except Exception:
            continue
        for t in tf.tables:
            try:
                rows = t.extract()
            except Exception:
                continue
            rows = [[_cell_txt(c) for c in r] for r in rows]
            if len(rows) < 2 or len(rows[0]) < 2:
                continue
            flat = " ".join(" ".join(r) for r in rows)
            if re.search(r"[\ue000-\uf8ff]|Discuss|Improve|Next question|Textbook", flat):
                continue
            # bilkul khali columns hatao (jaise stray "rta" column)
            ncol = max(len(r) for r in rows)
            rows = [r + [""] * (ncol - len(r)) for r in rows]
            keep = [j for j in range(ncol) if any(rows[i][j] for i in range(1, len(rows))) or (rows[0][j] and len(rows[0][j]) > 4)]
            rows = [[r[j] for j in keep] for r in rows]
            if len(rows[0]) < 2:
                continue
            out.append({"rows": rows, "page": pn + 1})
    return out


def table_to_md(rows):
    esc = lambda c: (c or "").replace("|", "\\|")
    md = ["| " + " | ".join(esc(c) for c in rows[0]) + " |",
          "| " + " | ".join("---" for _ in rows[0]) + " |"]
    for r in rows[1:]:
        md.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(md)


def splice_tables(chunk, tables):
    """Chunk-text mein table ki lines ko ek marker line ("@@TBLn@@") se badal deta hai.
    Return: (chunk, {marker: markdown})"""
    from collections import Counter
    mds = {}
    if not tables:
        return chunk, mds
    lines = chunk.split("\n")
    cursor = 0
    for ti, t in enumerate(tables):
        need = Counter(w for r in t["rows"] for c in r for w in c.split())
        header = set(w for c in t["rows"][0] for w in c.split())
        if not need:
            continue
        total = sum(need.values())
        # start: pehli line jis ke saare tokens table ke hain aur kam az kam 1 header token ho
        start = None
        for k in range(cursor, len(lines)):
            toks = lines[k].split()
            if len(toks) >= 1 and all(need[w] > 0 for w in toks) and any(w in header for w in toks):
                start = k
                break
        if start is None:
            continue
        rem = Counter(need)
        used, k = [], start
        while k < len(lines) and sum(rem.values()) > 0:
            toks = lines[k].split()
            if not toks:
                used.append(k); k += 1; continue
            if all(rem[w] > 0 for w in toks):
                for w in toks:
                    rem[w] -= 1
                used.append(k); k += 1
            else:
                break
        left = sum(rem.values())
        if left > max(3, 0.15 * total):      # match poor raha -- original text chhod do
            continue
        marker = "@@TBL%d@@" % ti
        mds[marker] = table_to_md(t["rows"])
        end = used[-1]
        # trailing blank lines hata do, marker ko apni paragraph banao
        lines[start:end + 1] = ["", marker, ""]
        cursor = start + 3
    return "\n".join(lines), mds


def apply_table_markers(text, mds):
    if not text or not mds:
        return text
    for m, md in mds.items():
        text = re.sub(r"^[ \t>#\-\*]*" + re.escape(m) + r"[ \t]*$", lambda _m, _md=md: "\n\n" + _md + "\n\n", text, flags=re.M)
        text = text.replace(m, "\n\n" + md + "\n\n")
    return text



def _clean_link_source(src):
    """'Alzheimer's Society bH Qs' -> 'Alzheimer's Society' (icon kachra trailing tokens)."""
    if not src:
        return src
    keep = {"UK", "US", "NHS", "NICE", "BMJ", "WHO", "The", "Of", "And", "BNF", "CKS", "RCP", "GMC"}
    tk = src.split()
    while len(tk) > 1 and len(tk[-1]) <= 3 and tk[-1] not in keep and (
            re.match(r"^[a-z]?[A-Z][a-z]?$", tk[-1]) or re.search(r"[a-z][A-Z]|[^A-Za-z]", tk[-1])):
        tk.pop()
    return " ".join(tk)


def screen_options(options, stem, expected=None):
    """Missing / extra options ki screening -> warnings list."""
    w = []
    n = len(options)
    pcts = [o["percentage"] for o in options]
    if n == 0:
        return ["no_options_parsed (stem/options text-layer mein nahi -- OCR/manual check)"]
    if expected:
        en, epcts = expected
        if n < en:
            w.append(f"missing_option: {en} options PDF mein dikhe, sirf {n} parse hue")
        elif n > en:
            w.append(f"extra_option: PDF mein {en} options hain, {n} parse hue (stem/explanation ki line option ban gayi?)")
        if sorted(pcts) != sorted(epcts) and n == en:
            w.append(f"option_pct_mismatch: parsed={pcts} geometry={epcts}")
    elif n < 4:
        w.append(f"possible_missing_option: sirf {n} options parse hue")
    elif n > 6:
        w.append(f"possible_extra_option: {n} options parse hue")
    tot = sum(pcts)
    if not (96 <= tot <= 104):
        if tot < 96:
            w.append(f"option_pct_sum={tot} (kam -- koi option ya % miss hua ho sakta hai)")
        else:
            w.append(f"option_pct_sum={tot} (zyada -- extra/duplicate option ho sakta hai)")
    seen = {}
    for i, o in enumerate(options):
        t = re.sub(r"\W+", " ", o["text"].lower()).strip()
        if t in seen:
            w.append(f"duplicate_option: {chr(97 + seen[t])} aur {chr(97 + i)} same text")
        seen[t] = i
        if len(o["text"].split()) > 22 or o["text"].rstrip().endswith("?"):
            w.append(f"option_{chr(97 + i)}_looks_like_sentence (stem/explanation ka hissa?): {o['text'][:50]}")
        elif len(o["text"].strip()) < 2:
            w.append(f"option_{chr(97 + i)}_text_empty_or_too_short")
        elif o["text"][:1].islower() and not re.match(r"^(ph|pH|mc|mm|g/|e\.g|i\.e|[a-z]{1,3}\d)", o["text"]):
            w.append(f"option_{chr(97 + i)}_starts_lowercase (pichli option ki wrapped line toot gayi?): {o['text'][:40]}")
    return w


def extract_link_urls(pdf_doc, start_page, end_page):
    """Link-widget ke asli URLs (PDF link annotations) -- title text ke saath."""
    res, textbooks = [], []
    if pdf_doc is None:
        return res, textbooks
    seen = set()
    for pn in range(max(0, start_page - 1), min(len(pdf_doc), end_page)):
        pg = pdf_doc[pn]
        for l in pg.get_links():
            uri = l.get("uri") or ""
            if not uri or "cloudfront.net" in uri or uri.endswith("passmedicine.com/menu.php"):
                continue
            try:
                txt = _cell_txt(pg.get_textbox(geo.Rect(l["from"])))
            except Exception:
                txt = ""
            if not txt or (txt, uri) in seen:
                continue
            seen.add((txt, uri))
            (textbooks if "passmedicine.com/textbook" in uri else res).append({"title": txt, "url": uri})
    return res, textbooks


def parse_question_block(lines):
    """
    Very layout-driven parser tuned to PassMedicine's single-best-answer
    question format:
        <stem sentences...>
        <option 1> <pct>
        <option 2> <pct>
        ...
        <explanation paragraphs...>
    """
    # Find where options start: PassMedicine options are short lines
    # immediately followed by a number (their %). We detect a contiguous
    # run of "short line + trailing percent" patterns.
    option_start = None
    options = []
    i = 0
    while i < len(lines):
        m = OPTION_PCT_RE.match(lines[i])
        if m and len(lines[i]) < 90:
            option_start = option_start if option_start is not None else i
            options.append({"text": m.group(1).strip(), "percentage": int(m.group(2))})
            i += 1
            continue
        if option_start is not None:
            break
        i += 1

    stem_lines = lines[:option_start] if option_start else lines
    stem = " ".join(stem_lines).strip()

    remainder = lines[i:]
    explanation = " ".join(remainder).strip()
    # Drop a leading duplicate "The correct answer is X" restatement line if
    # it also appears standalone right before the paragraph (common in export)
    explanation = re.sub(r'\s{2,}', ' ', explanation)

    correct_answer = None
    if options:
        best = max(options, key=lambda o: o["percentage"])
        correct_answer = best["text"]
        for o in options:
            o["correct"] = (o["text"] == correct_answer)

    return {
        "text": stem,
        "options": options,
        "correct_answer": correct_answer,
        "explanation_raw": explanation,
    }


def bold_explanation(explanation_raw, option_texts):
    """
    Har paragraph ke shuru mein agar koi option-name aata hai to use bold
    markdown (**term**) mein wrap karta hai — OCR se real bold info nahi
    milti, isliye ye content-based heuristic use ki hai. Blockquote
    (callout box, "> ...") lines ko temporarily placeholder se protect
    karte hain taake unke andar double-bold na lage.
    """
    lines = explanation_raw.split("\n")
    placeholders = {}
    protected_lines = []
    for idx, line in enumerate(lines):
        if line.startswith(">"):
            key = f"\x02BQ{idx}\x02"
            placeholders[key] = line
            protected_lines.append(key)
        else:
            protected_lines.append(line)
    text = "\n".join(protected_lines)

    paras = text.split("\n")
    for term in sorted(option_texts, key=len, reverse=True):
        if not term.strip():
            continue
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        for pi, para in enumerate(paras):
            m = pattern.search(para)
            if not m or "**" in para[:m.start()][-2:]:
                continue
            if m.start() <= 60:
                paras[pi] = para[:m.start()] + "**" + m.group(0) + "**" + para[m.end():]
                break
    text = "\n".join(paras)

    for key, original in placeholders.items():
        text = text.replace(key, original)
    return text


def parse_notes_by_indent(indented_lines):
    """
    Text-mode notes parser -- bullet characters (•/○) is PDF export mein
    pdftotext se gayab ho jate hain, isliye unke bajaye leading-space
    indentation se heading/bullet/sub-bullet hierarchy nikalte hain.
    indented_lines: list of (indent, text), jiski pehli line heading hai.

    Real data se pata chala ke indent values GLOBALLY cluster nahi karte
    (jaise 20,24,25,27,28,30,32,33 -- ek continuous chain), isliye levels
    ko HAR subheading ke andar RELATIVE tareeke se nikalte hain: pehli
    non-subheading indent us scope ke liye "bullet baseline" ban jaati
    hai, phir usse thodi zyada indent wali cheez "sub-bullet baseline",
    aur usse bhi zyada gehri koi cheez sirf pichle item mein continuation
    ki tarah jud jaati hai (bahut gehra nesting yahan practically hamesha
    extra-detail text hota hai, alag item nahi).
    """
    if not indented_lines:
        return {"heading": None, "sections": [], "mnemonic_box": None}

    # agar pehli line hi koi special marker (callout/image) hai, to koi
    # asal heading nahi hai -- usko body ki tarah hi process karte hain
    if indented_lines[0][1].startswith(CALLOUT_MARKER) or indented_lines[0][1].startswith(IMAGE_MARKER):
        heading = None
        body = indented_lines
    else:
        heading = indented_lines[0][1]
        body = indented_lines[1:]

    if not body:
        return {"heading": heading, "sections": [], "mnemonic_box": None}

    # callout/image marker lines hamesha indent=0 pe insert hoti hain (jo
    # baseline calculation ko galat tarike se 0 tak khinch deti hain) --
    # unhe is calculation se exclude karte hain
    non_marker_indents = [
        ind for ind, text in body
        if not text.startswith(CALLOUT_MARKER) and not text.startswith(IMAGE_MARKER)
    ]
    subheading_indent = min(non_marker_indents) if non_marker_indents else 0
    TOL = 2

    def unbalanced(text):
        stripped = text.rstrip()
        return stripped.count('(') > stripped.count(')') or stripped.endswith(',')

    sections = []
    current_section = None
    current_bullet = None
    mnemonic = None
    bullet_baseline = None
    subbullet_baseline = None
    seen_explicit_marker = False

    def append_to_deepest(text):
        """Bahut gehra nesting -- jo bhi sabse recent item hai usी mein extra detail ki tarah jod do."""
        if current_bullet is not None and current_bullet["sub_bullets"]:
            current_bullet["sub_bullets"][-1] += " " + text
        elif current_bullet is not None:
            current_bullet["text"] += " " + text
        elif current_section is not None and current_section["bullets"]:
            current_section["bullets"][-1]["text"] += " " + text

    i = 0
    while i < len(body):
        ind, text = body[i]
        newpara = text.startswith(PBREAK)
        if newpara:
            text = text[len(PBREAK):]

        # summary-callout box (kahin bhi mil sakta hai notes mein) -- ise
        # apni sahi position pe ek alag "callout" section ki tarah rakhte
        # hain, current heading/bullet state ko chhede bina
        if text.startswith(CALLOUT_MARKER):
            parts = text[len(CALLOUT_MARKER):].split(CALLOUT_SEP)
            sections.append({"type": "callout", "title": parts[0], "points": parts[1:]})
            i += 1
            continue

        # diagram image ka marker -- uske sahi notes-paragraph ke paas
        # (jahan wo asal PDF page pe mila) inline rakhte hain
        if text.startswith(IMAGE_MARKER):
            parts = text[len(IMAGE_MARKER):].split(CALLOUT_SEP)
            sections.append({"type": "image", "file": parts[0], "caption": parts[1] if len(parts) > 1 else ""})
            i += 1
            continue

        # kuch source pages ke raw text mein literal "-" bullet marker
        # already maujood hai (indent shift ke bawajood, jaise "Blood
        # pressure measurement" wala page) -- ye ek explicit, reliable
        # signal hai jo indent-guessing se hamesha priority leta hai
        explicit_bullet = re.match(r'^-\s+(.*)', text)
        explicit_heading = re.match(r'^#{1,4}\s+(.*)', text)

        if explicit_heading:
            seen_explicit_marker = True
            current_section = {"subheading": explicit_heading.group(1), "bullets": []}
            sections.append(current_section)
            current_bullet = None
            bullet_baseline = None
            subbullet_baseline = None
            i += 1
            continue

        if explicit_bullet:
            seen_explicit_marker = True
            current_bullet = {"text": explicit_bullet.group(1), "sub_bullets": []}
            if current_section is None:
                current_section = {"subheading": None, "bullets": []}
                sections.append(current_section)
            current_section["bullets"].append(current_bullet)
            i += 1
            continue

        # explicit-marker documents (hyphen/hash already in source) don't
        # reliably distinguish headings from continuation via indent at
        # all -- once we've seen a marker in this block, any subsequent
        # unmarked line is just a wrapped continuation of whatever is open
        if seen_explicit_marker and current_bullet is not None and not newpara:
            current_bullet["text"] += " " + text
            i += 1
            continue

        # short ALL-CAPS line (jaise "RILE") = mnemonic box title -- ye
        # kabhi subheading-indent pe hoti hai, kabhi bullet-indent pe
        # (indent se independent, sirf pattern se hi reliably pehchani
        # jaati hai), everything after it, till end of notes block, is
        # its content (mnemonic boxes are always the last element).
        # Agar image/callout marker bhi baaki lines mein mil jaaye, use
        # mnemonic box ke andar raw text ki tarah nahi lete -- alag se
        # trailing section ki tarah rakhte hain (mnemonic box ke baad).
        if (
            len(text.split()) <= 8
            and re.match(MNEMONIC_TITLE_PATTERN, text)
            and text.upper() not in MNEMONIC_TITLE_EXCLUDE
        ):
            mnemonic_lines = []
            trailing_sections = []
            for ind2, t2 in body[i + 1:]:
                t2 = t2.replace(PBREAK, "")
                if t2.startswith(CALLOUT_MARKER):
                    parts = t2[len(CALLOUT_MARKER):].split(CALLOUT_SEP)
                    trailing_sections.append({"type": "callout", "title": parts[0], "points": parts[1:]})
                elif t2.startswith(IMAGE_MARKER):
                    parts = t2[len(IMAGE_MARKER):].split(CALLOUT_SEP)
                    trailing_sections.append({"type": "image", "file": parts[0], "caption": parts[1] if len(parts) > 1 else ""})
                else:
                    mnemonic_lines.append(t2)
            mnemonic = {"title": text, "lines": mnemonic_lines, "trailing_sections": trailing_sections}
            break

        is_heading_level = (
            ind <= subheading_indent + TOL
            and len(text.split()) <= 8
            and bool(re.match(r"""^[\'\"‘“*(\[]?[A-Z0-9]""", text))
        )

        if is_heading_level:
            current_section = {"subheading": text, "bullets": []}
            sections.append(current_section)
            current_bullet = None
            bullet_baseline = None
            subbullet_baseline = None
            i += 1
            continue

        # indent0 par lambi (>8 words) line -- ye heading nahi, flowing
        # prose hai (jaise ek intro paragraph); usko current bullet/section
        # mein continuation ki tarah jod dete hain, naya heading nahi
        # banate
        if ind <= subheading_indent + TOL:
            if current_bullet is not None and not newpara:
                current_bullet["text"] += " " + text
            elif current_section is not None:
                current_section["bullets"].append({"text": text, "sub_bullets": []})
                current_bullet = current_section["bullets"][-1]
                bullet_baseline = ind
            else:
                current_section = {"subheading": None, "bullets": [{"text": text, "sub_bullets": []}]}
                sections.append(current_section)
                current_bullet = current_section["bullets"][-1]
                bullet_baseline = ind
            i += 1
            continue

        if bullet_baseline is None:
            bullet_baseline = ind

        if abs(ind - bullet_baseline) <= TOL:
            # level 1 (bullet) -- but a wrapped continuation of the
            # previous bullet if it left something unclosed
            if current_bullet is not None and unbalanced(current_bullet["text"]):
                current_bullet["text"] += " " + text
            else:
                current_bullet = {"text": text, "sub_bullets": []}
                if current_section is None:
                    current_section = {"subheading": None, "bullets": []}
                    sections.append(current_section)
                current_section["bullets"].append(current_bullet)
        else:
            if subbullet_baseline is None:
                subbullet_baseline = ind
            if abs(ind - subbullet_baseline) <= TOL:
                # level 2 (sub-bullet)
                if (current_bullet is not None and current_bullet["sub_bullets"]
                        and unbalanced(current_bullet["sub_bullets"][-1])):
                    current_bullet["sub_bullets"][-1] += " " + text
                elif current_bullet is not None:
                    current_bullet["sub_bullets"].append(text)
                elif current_section is not None:
                    current_section["bullets"].append({"text": text, "sub_bullets": []})
            else:
                # level 3+ -- too deep for our 2-level model, treat as
                # extra detail merged into whatever's currently open
                append_to_deepest(text)
        i += 1

    return {"heading": heading, "sections": sections, "mnemonic_box": mnemonic}


def sections_to_markdown_v2(notes):
    """sections_to_markdown() jaisa hi, bas subheading None ho sakti hai (bullets-only section)."""
    md = []
    if notes.get("heading"):
        md.append(f"# {notes['heading']}")
        md.append("")
    for sec in notes["sections"]:
        if sec.get("type") == "callout":
            md.append(f"> **{sec['title']}**")
            for p in sec["points"]:
                md.append(f"> - {p}")
            md.append("")
            continue
        if sec.get("type") == "image":
            md.append(f"![{sec.get('caption', '')}]({sec['file']})")
            md.append("")
            continue
        if sec.get("subheading"):
            md.append(f"## {sec['subheading']}")
        for b in sec["bullets"]:
            md.append(f"- {b['text']}")
            for sb in b["sub_bullets"]:
                md.append(f"  - {sb}")
        md.append("")
    if notes.get("mnemonic_box"):
        mb = notes["mnemonic_box"]
        md.append(f"> **{mb['title']}**")
        for line in mb["lines"]:
            md.append(f"> {line}")
        md.append("")
        for sec in mb.get("trailing_sections", []):
            if sec.get("type") == "callout":
                md.append(f"> **{sec['title']}**")
                for p in sec["points"]:
                    md.append(f"> - {p}")
                md.append("")
            elif sec.get("type") == "image":
                md.append(f"![{sec.get('caption', '')}]({sec['file']})")
                md.append("")
    return "\n".join(md).strip()


def parse_notes_block(lines, first_line_is_heading=False):
    """
    Parses reference-notes lines (like the 'Murmurs' page) into:
      heading, sections[{subheading, bullets[{text, sub_bullets[]}]}], mnemonic_box

    first_line_is_heading: True jab caller ne already generically detect
    kar liya ho ke ye notes-block hai aur uski pehli line hi heading hai
    (jaisa multi-question qbank mode mein hota hai, jahan topic "Murmurs"
    ke ilawa kuch bhi ho sakta hai) -- is case mein HEADING_KEYWORDS list
    pe depend nahi karte.
    """
    # Dedupe adjacent duplicate lines that come from overlapping screenshot
    # crops (common when the person photographs a scrolling page in chunks).
    deduped = []
    for l in lines:
        if deduped and deduped[-1] == l:
            continue
        deduped.append(l)
    lines = deduped

    heading = None
    sections = []
    current_section = None
    current_bullet = None
    mnemonic = None

    i = 0
    if first_line_is_heading and lines:
        heading = lines[0].strip()
        i = 1

    while i < len(lines):
        line = lines[i]

        if heading is None and line.split()[0:1] and line.rstrip(" *®★") in HEADING_KEYWORDS[:1]:
            heading = line.rstrip(" *®★").strip()
            i += 1
            continue

        is_known_subheading = any(
            line.strip() == kw or line.strip().startswith(kw) for kw in HEADING_KEYWORDS[1:]
        )
        is_pattern_subheading = bool(SUBHEADING_PATTERN.match(line.strip()))
        l1 = re.match(rf'^\s*[{re.escape(BULLET_L1_CHARS)}]\s+(.*)', line)
        l2 = re.match(rf'^\s*[{re.escape(BULLET_L2_CHARS)}]\s+(.*)', line)
        # Generic fallback: any short, period-less standalone line (not a
        # bullet itself) is treated as a subheading -- keeps this usable
        # for topics other than murmurs without needing a keyword list.
        is_generic_subheading = (
            not l1 and not l2 and 1 <= len(line.split()) <= 6
            and not line.rstrip().endswith(('.', ':', ',', ')'))
            and line[:1].isupper()
        )

        if (is_known_subheading or is_pattern_subheading or is_generic_subheading) and not l1 and not l2:
            current_section = {"subheading": line.strip(), "bullets": []}
            sections.append(current_section)
            current_bullet = None
            i += 1
            continue

        if l1 and current_section is not None:
            current_bullet = {"text": l1.group(1).strip(), "sub_bullets": []}
            current_section["bullets"].append(current_bullet)
            i += 1
            continue

        if l2 and current_bullet is not None:
            current_bullet["sub_bullets"].append(l2.group(1).strip())
            i += 1
            continue

        # mnemonic box: short ALL-CAPS title (e.g. "RILE") followed by
        # explanatory lines, with no bullet markers
        if (
            re.match(MNEMONIC_TITLE_PATTERN, line.strip())
            and line.strip().upper() not in MNEMONIC_TITLE_EXCLUDE
            and mnemonic is None
        ):
            mnemonic = {"title": line.strip(), "lines": []}
            i += 1
            while i < len(lines) and not re.match(rf'^\s*[{re.escape(BULLET_L1_CHARS)}{re.escape(BULLET_L2_CHARS)}]', lines[i]):
                if lines[i].strip() in HEADING_KEYWORDS or SUBHEADING_PATTERN.match(lines[i].strip()):
                    break
                mnemonic["lines"].append(lines[i].strip())
                i += 1
            continue

        # a bullet continuation line that OCR wrapped without a marker
        # (e.g. "and 'blowing' in character)") - append to the last bullet/sub-bullet
        if current_bullet is not None and not line.strip().startswith(tuple(BULLET_L1_CHARS + BULLET_L2_CHARS)):
            if current_bullet["sub_bullets"]:
                current_bullet["sub_bullets"][-1] += " " + line.strip()
            else:
                current_bullet["text"] += " " + line.strip()
            i += 1
            continue

        i += 1

    # Dedupe adjacent duplicate lines/entries that come from overlapping
    # screenshot crops (common when photographing a scrolling page in chunks)
    for sec in sections:
        deduped_bullets = []
        for b in sec["bullets"]:
            if deduped_bullets and deduped_bullets[-1]["text"] == b["text"]:
                continue
            b["sub_bullets"] = [
                sb for i, sb in enumerate(b["sub_bullets"])
                if i == 0 or sb != b["sub_bullets"][i - 1]
            ]
            deduped_bullets.append(b)
        sec["bullets"] = deduped_bullets

    return {"heading": heading, "sections": sections, "mnemonic_box": mnemonic}


# ---------------------------------------------------------------------------
# STEP 5: build final JSON (answer key + student view)
# ---------------------------------------------------------------------------

def build_json(question, notes, diagram_files, source_name):
    option_texts = [o["text"] for o in question["options"]]
    explanation_md = bold_explanation(question["explanation_raw"], option_texts)
    notes_tables_md = question.get("notes_tables_md") or []
    notes_body_md = sections_to_markdown_v2(notes)
    if notes_tables_md:
        notes_body_md = (notes_body_md + "\n\n" + "\n\n".join(notes_tables_md)).strip()

    answer_key = {
        "source_pdf": source_name,
        "question": {
            "id": "q1",
            "text": question["text"],
            "options": [
                {"id": chr(97 + idx), "text": o["text"], "percentage": o["percentage"], "correct": o["correct"]}
                for idx, o in enumerate(question["options"])
            ],
            "attempted": True,
            "correct_answer_id": next(
                (chr(97 + idx) for idx, o in enumerate(question["options"]) if o["correct"]), None
            ),
            "explanation": explanation_md,
        },
        "notes": {
            "heading": notes["heading"],
            "body_markdown": notes_body_md,
            "images": diagram_files,
        },
    }

    student_view = {
        "source_pdf": source_name,
        "question": {
            "id": "q1",
            "text": question["text"],
            "options": [
                {"id": chr(97 + idx), "text": o["text"]}
                for idx, o in enumerate(question["options"])
            ],
            "attempted": False,
            "student_answer": None,
            "correct_answer_id": None,
            "explanation": None,
        },
        "notes": {
            "heading": notes["heading"],
            "body_markdown": notes_body_md,
            "images": diagram_files,
        },
    }

    return answer_key, student_view


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def run_notes_mode(image_paths, out_dir, source_name):
    img_dir = os.path.join(out_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    print(f"[1/3] OCR on {len(image_paths)} notes image(s) ...")
    full_text, diagram_images = classify_and_ocr(image_paths, psm="6", enhance=False, diagram_mode="markers")

    diagram_files = []
    for idx, path in enumerate(diagram_images):
        cropped_path = autocrop_diagram(path)
        ext = ".png" if cropped_path != path else Path(path).suffix
        final_name = f"{source_name}_diagram_{idx}{ext}"
        final_path = os.path.join(img_dir, final_name)
        import shutil
        shutil.copy(cropped_path, final_path)
        diagram_files.append({
            "file": os.path.relpath(final_path, out_dir),
            "caption": "Diagram extracted from notes images (verify/edit caption manually)."
        })
    print(f"      -> {len(diagram_files)} diagram image(s) saved to {img_dir}/")

    print("[2/3] Cleaning noise + structuring notes ...")
    lines = clean_lines(full_text)
    notes = parse_notes_block(lines)
    notes["images"] = diagram_files
    notes["body_markdown"] = sections_to_markdown_v2(notes)

    print("[3/3] Writing JSON output ...")
    out_path = os.path.join(out_dir, f"{source_name}_notes.json")
    _atomic_write_json_global(out_path, notes)

    print(f"\n{C.GREEN}{C.BOLD}Done.{C.RESET}\n{C.GREEN}  Notes JSON -> {out_path}\n  Images     -> {img_dir}/{C.RESET}")

    # If an answer_key / student_view JSON from the earlier PDF-parsing step
    # already exists for this source, merge the notes into both.
    for suffix in ("_answer_key.json", "_student_view.json"):
        merge_path = os.path.join(out_dir, f"{source_name}{suffix}")
        if os.path.exists(merge_path):
            with open(merge_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["notes"] = {
                "heading": notes["heading"],
                "body_markdown": notes["body_markdown"],
                "images": diagram_files,
            }
            _atomic_write_json_global(merge_path, data)
            print(f"  Merged notes into -> {merge_path}")


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}


# qnum(global) -> {set_total, set_number}; split_into_questions bharta hai
CHUNK_META = {}


def dedupe_chunks(question_chunks):
    """
    PassMedicine-2 FIX #5: PDF mein kabhi ek hi question do baar capture ho jata
    hai (jaise Q2 do baar, Q3 gayab). Same global qnum ke duplicates mein se
    sirf sab se mukammal (lamba) chunk rakhta hai; order barqarar.
    Return: (unique_chunks, duplicates_removed:list[int])
    """
    best = {}
    for c in question_chunks:
        q = c[0]
        if q not in best or len(c[1]) > len(best[q][1]):
            best[q] = c
    seen, out, removed = set(), [], []
    for c in question_chunks:
        q = c[0]
        if q in seen:
            removed.append(q)
            continue
        seen.add(q)
        out.append(best[q])
    return out, removed


def split_into_questions(raw_text):
    """
    Poori qbank PDF (jisme 674 questions ek hi file mein hain) ko "Question
    X of Y" markers ke around split karta hai. Returns list of
    (question_number, chunk_text, char_start, char_end) tuples in order --
    char offsets original raw_text ke against hain (page-mapping ke liye).

    IMPORTANT: kai PDFs ke end mein ek chhota alag mock-exam/mini-quiz
    section bhi hota hai jiski apni ALAG "Question N of M" numbering hoti
    hai (jaise "Question 1 of 3", "Question 1 of 5" -- poori 674-question
    qbank se bilkul unrelated). Agar in sab markers ko bhi asal question-
    boundary maan liya jaye, to unke qnum (jaise 1, 2, 3...) asal qbank ke
    Q1, Q2, Q3 se COLLIDE kar jaate hain -- aur baad mein jo bhi process
    hota hai wo pehle wale (asal) question ko silently OVERWRITE kar deta
    hai.

    Kuch PDFs mein ULTA case bhi hota hai: DO YA ZYADA asal, poore-size
    question-sets ek hi file mein back-to-back hote hain (jaise "Question
    1 of 155" ... phir turant baad "Question 1 of 206"), har set apni
    numbering 1 se dobara shuru karta hai. Agar hum sirf pehla "of N"
    canonical maan kar baaki sab ignore kar dein, to poora doosra set
    (206 questions) silently gum ho jaata hai.

    Isliye markers ko unke "of N" total ke hisaab se CONSECUTIVE RUNS mein
    group karte hain. Jo run kaafi lamba hai (>=5 markers) usay ek asal
    question-section maante hain; chhote runs (jaise "of 3", "of 5") ko
    mock-quiz maan kar boundary ke taur par ignore karte hain -- unka text
    jis bhi asal question ke andar aaya, wahi reh jaata hai, jaisa pehle
    hota tha. Har asal section ke andar original "Question N" number use
    hota hai, lekin sections ke beech numbering CONTINUE hoti hai (pehle
    section ka aakhri number + is section ka N) taake do alag sets ke
    same local numbers (dono mein "Question 1") aapas mein collide na
    karein.
    """
    marker_re = re.compile(r'Question\s+(\d+)\s+of\s+(\d+)', re.IGNORECASE)
    all_matches = list(marker_re.finditer(raw_text))
    if len(all_matches) < 2:
        return None  # single-question file, caller falls back to old mode

    # Consecutive matches ko unke "of N" total ke hisaab se runs mein group karo
    runs = []  # list of {"total": str, "matches": [...]}
    for m in all_matches:
        total = m.group(2)
        if runs and runs[-1]["total"] == total:
            runs[-1]["matches"].append(m)
        else:
            runs.append({"total": total, "matches": [m]})

    # Sirf woh runs rakho jo ek asal question-section jaise lambe hon --
    # chhote runs (mock-quiz blurbs) ko boundary ke taur par ignore karo.
    MIN_REAL_SECTION_LEN = 5
    real_runs = [r for r in runs if len(r["matches"]) >= MIN_REAL_SECTION_LEN]
    if not real_runs:
        return None

    # Sab real-section markers ko ek single ordered list mein flatten karo
    # (taake har ek ka "end" agla marker ho, chahe wo agle run mein ho)
    boundary_matches = []
    for r in real_runs:
        boundary_matches.extend(r["matches"])
    boundary_matches.sort(key=lambda m: m.start())

    chunks = []
    global_offset = 0  # pichle sections ke total questions (numbering continue karne ke liye)
    prev_total = None
    for idx, m in enumerate(boundary_matches):
        total = m.group(2)
        if prev_total is not None and total != prev_total:
            # Naya section shuru hua -- offset ko pichle section ke last
            # local number tak badha do
            global_offset = chunks[-1][0]
        start = m.end()
        end = boundary_matches[idx + 1].start() if idx + 1 < len(boundary_matches) else len(raw_text)
        local_qnum = int(m.group(1))
        qnum = local_qnum + global_offset
        chunks.append((qnum, raw_text[start:end], m.start(), end))
        # PassMedicine-2: kis bank ("of N") ka kaunsa local number hai -- yaad rakho
        CHUNK_META[qnum] = {"set_total": int(total), "set_number": local_qnum}
        prev_total = total
    return chunks





def run_multi_question_mode(pdf_path, real_text, out_dir, source_name, image_paths, limit=None, no_diagrams=False, fill_images=False, offline_images=False, prebuilt_diagram_map=None):
    pdf_total_match = re.search(r'Question\s+\d+\s+of\s+(\d+)', real_text, re.IGNORECASE)
    pdf_total = pdf_total_match.group(1) if pdf_total_match else "?"

    question_chunks = split_into_questions(real_text)
    question_chunks, _dups = dedupe_chunks(question_chunks)
    if _dups:
        print(f"{C.YELLOW}      -> {len(_dups)} duplicate question capture(s) hata diye: Q{_dups}{C.RESET}")
    # set-wise gaps (jo numbers PDF mein hain hi nahi)
    _by_set = {}
    for _q, *_ in question_chunks:
        _m = CHUNK_META.get(_q)
        if _m:
            _by_set.setdefault(_m["set_total"], set()).add(_m["set_number"])
    for _tot, _nums in sorted(_by_set.items()):
        _gaps = [n for n in range(1, max(_nums) + 1) if n not in _nums]
        print(f"{C.MAGENTA}      -> set 'of {_tot}': {len(_nums)} questions mile (Q1-Q{max(_nums)})"
              f"{'; GAPS: ' + str(_gaps) if _gaps else ''}{C.RESET}")
    try:
        if pdf_path in _OCR_PDFS and any(_gl for _gl in [_by_set]):
            _lines = ["Gap report: jo question numbers nahi mile, unke candidate pages (header OCR mein bigad gaya ho sakta hai)\n"]
            _pg = real_text.split("\f")
            for _tot, _nums in sorted(_by_set.items()):
                for _n in range(1, max(_nums) + 1):
                    if _n in _nums:
                        continue
                    _c = [i + 1 for i, t in enumerate(_pg) if re.search(r"(?<!\d)%d\s*(?:of|0f|o f)\s*%s\b" % (_n, _tot), t[:700])
                          or re.search(r"Q\w{4,9}\s+%d\b" % _n, t[:400])]
                    _lines.append(f"Q{_n} (of {_tot}): candidate pages {_c if _c else 'koi nahi -- PDF mein shayad capture hi nahi hua'}")
            os.makedirs(os.path.join(out_dir, "ocr_debug"), exist_ok=True)
            with open(os.path.join(out_dir, "ocr_debug", "gaps.txt"), "w", encoding="utf-8") as _gf:
                _gf.write("\n".join(_lines) + "\n")
            print(f"{C.MAGENTA}      -> gap report: {out_dir}/ocr_debug/gaps.txt{C.RESET}")
    except Exception:
        pass
    qa_warnings = {}
    pdf_doc = None
    if HAVE_GEO or pdf_path in _OCR_PDFS:
        try:
            pdf_doc = _open_geo_doc(pdf_path)
        except Exception:
            pdf_doc = None
    if limit:
        question_chunks = question_chunks[:limit]
        print(f"{C.MAGENTA}[2/5] --limit={limit} diya gaya hai -- sirf pehle {limit} questions process honge{C.RESET}")
    total = len(question_chunks)
    print(f"{C.MAGENTA}[2/5] PDF mein {pdf_total} questions hain, {total} process honge,{C.RESET}")
    print(f"{C.MAGENTA}      multi-question mode + per-question diagram mapping ...{C.RESET}")

    img_dir = os.path.join(out_dir, "images")
    work_dir = os.path.join(out_dir, "_work")
    os.makedirs(img_dir, exist_ok=True)
    os.makedirs(work_dir, exist_ok=True)

    page_bounds = compute_page_bounds(real_text)
    total_pages = len(page_bounds)

    ak_path = os.path.join(out_dir, f"{source_name}_answer_key.json")
    sv_path = os.path.join(out_dir, f"{source_name}_student_view.json")

    # Resume support: agar pichli baar ka partial output pehle se mojood hai
    # (Ctrl+C ya crash se ruka hua run), to usay load karke wahin se aage
    # badhte hain -- shuru se dobara process nahi karte, purana data bhi
    # zaya nahi hota. Skip karne ke liye COUNT ki bajaye asal "number" field
    # match karte hain -- taake koi mismatch/gap ho to bhi sahi question se
    # hi resume ho, galat offset se nahi.
    answer_key_questions = []
    student_view_questions = []
    total_diagrams = 0
    done_qnums = set()
    if os.path.exists(ak_path) and os.path.exists(sv_path):
        try:
            with open(ak_path, "r", encoding="utf-8") as f:
                prev_ak = json.load(f)
            with open(sv_path, "r", encoding="utf-8") as f:
                prev_sv = json.load(f)
            answer_key_questions = prev_ak.get("questions", [])
            student_view_questions = prev_sv.get("questions", [])
            if FRESH or prev_ak.get("parser_version") != PARSER_VERSION:
                print(f"{C.YELLOW}      -> purana output {PARSER_VERSION} se nahi bana (ya --fresh) -- sab questions dobara parse honge{C.RESET}")
                answer_key_questions, student_view_questions = [], []
            done_qnums = {q.get("number") for q in answer_key_questions if q.get("number") is not None}
            total_diagrams = sum(
                ((q.get("notes") or {}).get("body_markdown", "") or "").count("![")
                for q in answer_key_questions
            )
            if done_qnums:
                print(f"{C.GREEN}      -> pichla partial output mila -- {len(done_qnums)} questions pehle se ban chuke hain "
                      f"(Q{min(done_qnums)}-Q{max(done_qnums)} range mein), baaki se resume kar raha hoon{C.RESET}")
        except Exception as e:
            # purana JSON corrupt/incomplete ho to safe side pe shuru se karo
            RED = "\033[1;31m"
            print(f"{RED}      [!] WARNING: purani JSON corrupt/unreadable nikli ({e}) -- "
                  f"is run ko processed-questions se shuru kar raha hoon (images already disk pe hain, "
                  f"unka koi nuksan nahi, lekin question-JSON progress reset ho gaya).{C.RESET}")
            answer_key_questions = []
            student_view_questions = []
            total_diagrams = 0
            done_qnums = set()

    # qnum -> list-index maps, taake fill-images pass mein purani entry
    # UPDATE ho (dobara append na ho, warna JSON mein duplicate questions
    # ban jayenge)
    ak_index_by_qnum = {q.get("number"): i for i, q in enumerate(answer_key_questions) if q.get("number") is not None}
    sv_index_by_qnum = {q.get("number"): i for i, q in enumerate(student_view_questions) if q.get("number") is not None}

    if fill_images:
        # --fill-images: pehle se JSON ban chuki hai (bina diagrams ke) --
        # ab har question ko dobara process karke sirf images/notes update
        # karenge, koi naya text-parsing dobara se shuru nahi karte.
        print(f"{C.GREEN}      -> --fill-images mode: {len(ak_index_by_qnum)} existing questions mein images fill ki jayengi{C.RESET}")
        done_qnums = set()  # sabko dobara process karo, lekin end mein UPDATE karenge, append nahi

    if prebuilt_diagram_map is not None:
        link_map = {}  # --offline-images fast-path mein CloudFront link-scan ki zaroorat nahi
        print(f"{C.MAGENTA}[2/5] --offline-images fast-path: diagram hyperlinks ka scan skip -- "
              f"purani JSON se mapping already mil chuki hai.{C.RESET}")
    else:
        print(f"{C.MAGENTA}[2/5] PDF ke andar diagram hyperlinks (asal CloudFront images) scan ho rahe hain ...{C.RESET}")
        link_map = build_page_link_image_map(pdf_path)
        if link_map:
            n_links = sum(len(v) for v in link_map.values())
            print(f"{C.MAGENTA}      -> {n_links} image link(s) mile {len(link_map)} page(s) pe (in se sharp images download hongi){C.RESET}")
        else:
            print(f"{C.MAGENTA}      -> koi image link nahi mila -- pixel-crop fallback use hoga{C.RESET}")

    def _atomic_write_json(path, data):
        # Pehle ek temp file mein poora likhte hain, phir os.replace se
        # asal file ko ek hi atomic step mein update karte hain. Agar
        # beech mein Ctrl+C/force-stop ho jaye, to ya to purani (theek)
        # file bachi rehti hai, ya nayi (poori) -- kabhi bhi adhoori/
        # corrupt JSON save nahi hoti.
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)

    def save_progress():
        _atomic_write_json(ak_path, {
            "source_pdf": source_name,
            "parser_version": PARSER_VERSION,
            "total_questions": total,
            "questions": answer_key_questions,
        })
        _atomic_write_json(sv_path, {
            "source_pdf": source_name,
            "parser_version": PARSER_VERSION,
            "total_questions": total,
            "questions": student_view_questions,
        })

    # JSON files ko shuru mein hi (chahe abhi khaali/partial hon) bana dete
    # hain -- taake output folder mein images/_work ke saath-saath JSON bhi
    # pehle se hi dikhein, sirf pehla question complete hone ka wait na ho.
    save_progress()

    # ============================================================
    # PHASE 1: Sirf images (network-dependent, interruption-prone).
    # Har question ke page-range ke liye link_map se CloudFront URLs
    # download karte hain -- agar file already disk pe maujood hai
    # (pichli baar download ho chuki), to skip kar dete hain, isliye
    # ye phase khud hi per-IMAGE resumable hai (question-level nahi,
    # image-level -- zyada fine-grained aur bandwidth-safe).
    # Iske baad Phase 2 (JSON building) ko INTERNET ki zaroorat nahi
    # padti, isliye JSON ab interruption se corrupt/incomplete nahi
    # hoti.
    # ============================================================
    print(f"{C.CYAN}[3/5] Phase 1: sab diagram images download ho rahi hain (agar file pehle se hai to skip)...{C.RESET}")
    diagram_files_by_qnum = {}  # qnum -> [{"file","caption","page"}, ...] (link-download wali)

    if prebuilt_diagram_map is not None:
        # --offline-images fast-path: purani JSON se diagram-mapping seedha
        # reuse kar rahe hain -- Phase 1 ka poora download/fallback loop
        # skip, kyunki humein pehle se pata hai ye images disk pe maujood
        # hain (in files ka wajood download_image()/render-fallback ke
        # waqt hi verify ho chuka tha jab ye pehli baar bani thi).
        diagram_files_by_qnum = dict(prebuilt_diagram_map)
        print(f"{C.CYAN}      -> Phase 1 skip -- {len(diagram_files_by_qnum)} questions ka diagram-mapping "
              f"purani JSON se reuse kiya.{C.RESET}")
    else:
        # Jo URIs pichle kisi run mein PERMANENTLY fail ho chuki thi (na
        # download hui, na fallback render+crop se recover hui), unko yahan
        # yaad rakhte hain -- taake future runs mein unke liye koi bhi
        # network attempt (ya fallback render) dobara na ho, seedha skip ho
        # jaye. Bina isके, hamesha-dead links har resume par phir se try
        # hoti rehti (bewajah time/network waste).
        dead_links_path = os.path.join(img_dir, "_dead_links.json")
        dead_links = set()
        if os.path.exists(dead_links_path):
            try:
                with open(dead_links_path, "r", encoding="utf-8") as f:
                    dead_links = set(json.load(f))
            except Exception:
                dead_links = set()

        def _mark_dead_link(uri):
            dead_links.add(uri)
            try:
                _atomic_write_json_global(dead_links_path, sorted(dead_links))
            except Exception:
                pass  # cache save fail ho to bhi run rukna nahi chahiye

        if no_diagrams:
            print(f"{C.YELLOW}      -> --no-diagrams diya gaya hai -- images/diagrams is pass mein skip ho rahe hain{C.RESET}")
        else:
            if offline_images:
                print(f"{C.YELLOW}      -> --offline-images diya gaya hai -- koi bhi naya download/fallback attempt "
                      f"nahi hoga, sirf jo images disk pe pehle se maujood hain unhi ko use karenge{C.RESET}")
            for qnum, chunk, char_start, char_end in question_chunks:
                start_page = offset_to_page(page_bounds, char_start)
                end_page = offset_to_page(page_bounds, max(char_start, char_end - 1))
                end_page = min(end_page, total_pages)

                q_diagrams = []
                seen_uris_this_question = set()
                for page_num in range(start_page, end_page + 1):
                    for uri in link_map.get(page_num, []):
                        if uri in seen_uris_this_question:
                            continue
                        seen_uris_this_question.add(uri)
                        idx = len(q_diagrams)
                        ext_match = IMAGE_LINK_EXT_RE.search(uri)
                        ext = "." + ext_match.group(1).lower() if ext_match else ".png"
                        final_name = f"{source_name}_q{qnum}_diagram_{idx}{ext}"
                        final_path = os.path.join(img_dir, final_name)
                        # Pichle run mein agar ye link fail hokar fallback
                        # (render+crop) se recover hui thi, to wo hamesha
                        # ".png" naam se save hoti hai chahe original URL ka
                        # extension kuch bhi ho -- isliye skip-check mein
                        # dono naam check karte hain, warna har resume par
                        # yahi images bewajah dobara try hoti rehti hain.
                        fallback_check_path = os.path.splitext(final_path)[0] + ".png"
                        if ASSUME_IMAGES:
                            q_diagrams.append({
                                "file": os.path.relpath(final_path, out_dir),
                                "caption": "Diagram extracted from source PDF (verify/edit caption manually).",
                                "page": page_num,
                            })
                            continue
                        if uri in dead_links:
                            # Pichle run mein hi confirm ho chuka tha ke ye link
                            # permanently dead hai (download + fallback dono fail)
                            # -- koi bhi naya attempt kiye bina seedha skip.
                            continue
                        elif os.path.exists(final_path) and os.path.getsize(final_path) > 200:
                            pass  # pehle se download ho chuki hai -- skip
                        elif offline_images:
                            # --offline-images mode: is diagram ke liye na koi
                            # network download try karo, na fallback render --
                            # bas is diagram ko is dafa ke liye skip kar do
                            # (jaise ek dead link handle hoti hai).
                            continue
                        else:
                            ok = download_image(uri, final_path)
                            if not ok:
                                # Sirf link se download hoti hai -- PDF se render/crop
                                # fallback band hai. Fail hui to is image ko skip karo
                                # (dead-list mein nahi daalte, taake agla run dobara try kare).
                                print(f"{C.YELLOW}      -> Q{qnum}: link download fail hui, image skip (koi crop fallback nahi){C.RESET}")
                                continue
                        q_diagrams.append({
                            "file": os.path.relpath(final_path, out_dir),
                            "caption": "Diagram extracted from source PDF (verify/edit caption manually).",
                            "page": page_num,
                        })
                if pdf_path in _OCR_PDFS and not q_diagrams:
                    # image-only PDF: page ke andar ke diagrams (ECG / X-ray / photo) page-render se crop karo
                    try:
                        _om = _ocr_mod()
                        _optp = None
                        for _pn in range(start_page, end_page + 1):
                            _qh0 = _om.ocr_page_qheader(pdf_path, _pn)
                            if _qh0 is not None and _qh0 != qnum:
                                break
                            if _om.ocr_expected_options(pdf_path, _pn, _pn):
                                _optp = _pn
                                break
                        _seen_fig = set()
                        for _pn in range(start_page, end_page + 1):
                            _qh = _om.ocr_page_qheader(pdf_path, _pn)
                            if _qh is not None and _qh != qnum:
                                break                  # agle question ka page (range overshoot)
                            for _fg in _om.ocr_find_figures(pdf_path, _pn):
                                _bx = _fg["box"]
                                _key = (_pn, _bx[0] // 20, _bx[1] // 20)
                                if _key in _seen_fig:
                                    continue
                                _seen_fig.add(_key)
                                _idx = len(q_diagrams)
                                _fname = f"{source_name}_q{qnum}_diagram_{_idx}.jpeg"
                                _fpath = os.path.join(img_dir, _fname)
                                if not (os.path.exists(_fpath) and os.path.getsize(_fpath) > 200):
                                    if not _om.ocr_crop_region(pdf_path, _pn, _bx, _fpath):
                                        continue
                                _stem = _optp is not None and (_pn < _optp or (_pn == _optp and _fg["cl_top"] is not None and _bx[3] <= _fg["cl_top"] + 4))
                                q_diagrams.append({
                                    "file": os.path.relpath(_fpath, out_dir),
                                    "caption": "Figure from source PDF",
                                    "page": _pn,
                                    "box": [float(x) for x in _bx],
                                    "stem": bool(_stem),
                                })
                    except Exception as _e:
                        print(f"{C.YELLOW}      -> Q{qnum}: figure detect fail ({_e}){C.RESET}")
                if q_diagrams:
                    diagram_files_by_qnum[qnum] = q_diagrams
            print(f"{C.CYAN}      -> Phase 1 complete, saari images disk pe mojood hain.{C.RESET}")
            if IMAGES_ONLY:
                print(f"{C.GREEN}{C.BOLD}Done (--images-only).{C.RESET}{C.GREEN} Images -> {img_dir}/{C.RESET}")
                return

    # ============================================================
    # PHASE 2: JSON building -- ab yahan se aage koi network call
    # nahi hai (sirf local text-parsing + local pixel-crop fallback
    # jahan link nahi mila), isliye ye phase interruption se JSON ko
    # corrupt nahi karti.
    # ============================================================
    print(f"{C.CYAN}[4/5] Phase 2: questions/notes JSON banaya ja raha hai (koi internet ki zaroorat nahi)...{C.RESET}")
    for qnum, chunk, char_start, char_end in question_chunks:
        if qnum in done_qnums:
            continue
        _sp0 = offset_to_page(page_bounds, char_start)
        _ep0 = min(offset_to_page(page_bounds, max(char_start, char_end - 1)), total_pages)
        chunk, tail_meta = extract_tail_metadata(chunk)
        # PassMedicine-2: asli PDF tables inline markdown table banao
        chunk, tbl_md = splice_tables(chunk, extract_page_tables(pdf_doc, _sp0, _ep0))
        if pdf_path in _OCR_PDFS:
            _om = _ocr_mod()
            for _mk in set(re.findall(r"@@OTBL_\d+_\d+@@", chunk)):
                _rows = _om.OCR_TABLES.get(_mk)
                tbl_md[_mk] = _om.table_rows_md(_rows) if _rows else ""
            # marker lines: indent hatao (warna column-table detector inhe table samajhta hai), khali table ke markers delete
            for _mk, _md in list(tbl_md.items()):
                if _mk.startswith("@@OTBL_"):
                    chunk = re.sub(r"[ \t]*" + re.escape(_mk) + r"[ \t]*", "" if not _md else "\n\n" + _mk + "\n\n", chunk)
                    if not _md:
                        del tbl_md[_mk]
        # links: asli URLs attach karo
        _urls, _textbooks = extract_link_urls(pdf_doc, _sp0, _ep0)
        _links = tail_meta["links"]
        import difflib as _dl
        for _L in _links:
            _L["source"] = _clean_link_source(_L.get("source"))
        _free = []
        for _u in _urls:
            _hit = next((L for L in _links if not L.get("url") and _dl.SequenceMatcher(
                None, re.sub(r"\W+", "", _cell_txt(L["title"]).lower()), re.sub(r"\W+", "", _u["title"].lower())).ratio() >= 0.75), None)
            if _hit:
                _hit["url"] = _u["url"]
            else:
                _free.append(_u)
        _nourl = [L for L in _links if not L.get("url")]
        if _free and len(_free) == len(_nourl):          # titles OCR mein bigad gaye -- order se jodo
            for L, _u in zip(_nourl, _free):
                L["url"] = _u["url"]
        else:
            for _u in _free:
                _links.append({"source": None, "title": _u["title"], "url": _u["url"]})
        tail_meta["links"] = [L for L in _links if L.get("url") or L.get("source")]
        tail_meta["textbooks"] = _textbooks
        # wrap-merge sirf NOTES hisse (pehli "Next question" line ke baad) par --
        # warna options ki wrapped lines (jaise "monitoring") % se jud jati hain
        _nq = re.search(r'^[ \t]*Next question\s*$', chunk, re.M)
        if _nq:
            # notes ka start authoritative marker: pehli "Next question" line.
            # (duplicate "Next question" lines bhi hata do)
            _after = re.sub(r'^(?:[ \t]*Next question\s*\n)+', '', chunk[_nq.end():].lstrip('\n'), flags=re.M)
            chunk = chunk[:_nq.start()] + "\n\n" + NOTES_START_TOKEN + "\n\n" + _after
            _nq = re.search(re.escape(NOTES_START_TOKEN) + r'\s*$', chunk, re.M)
        if _nq:
            chunk = chunk[:_nq.end()] + merge_wrapped_bullet_lines(
                chunk[_nq.end():], compute_continuation_lines(pdf_doc, _sp0, _ep0))
        paragraphs_with_offsets = paragraphs_from_text_with_offsets(chunk)
        paragraphs = [p for p, _ in paragraphs_with_offsets]
        offsets = [o for _, o in paragraphs_with_offsets]
        parsed = parse_question_paragraphs(paragraphs, offsets)
        _st = None
        if pdf_path in _OCR_PDFS:
            # image-only PDF: page geometry (borders / teal headings / bullet dots) se explanation + notes
            try:
                _st = _ocr_mod().ocr_blocks(pdf_path, _sp0, _ep0,
                                            cut_hints=[((L.get("source") or "").strip(), (L.get("title") or "").strip()) for L in tail_meta.get("links", [])],
                                            figs=[{"page": d_["page"], "box": d_["box"], "file": d_["file"], "caption": d_.get("caption", "")}
                                                  for d_ in diagram_files_by_qnum.get(qnum, []) if d_.get("box") and not d_.get("stem")])
            except Exception as _e:
                _st = None
                print(f"{C.YELLOW}      -> Q{qnum}: structure pass fail ({_e}) -- purana tareeka{C.RESET}")
        # OCR: aakhri option ka % miss ho jaye to wo option-text explanation ki pehli chhoti line ban jata hai -- wapas option banao
        _repaired = None
        if pdf_path in _OCR_PDFS and 3 <= len(parsed["options"]) <= 6:
            _er = (parsed.get("explanation_raw") or "").lstrip()
            _fp = re.split(r"\n\s*\n|\n", _er, maxsplit=1)
            _first = re.sub(r"^[>\s]+", "", _fp[0]).strip() if _fp else ""
            _sum0 = sum(o["percentage"] for o in parsed["options"])
            _kfn = re.sub(r"\W+", "", ((_st or {}).get("keyfact") or "")).lower()
            _fn = re.sub(r"\W+", "", _first).lower()
            _is_kf = bool(_fn) and bool(_kfn) and (_fn in _kfn or _kfn in _fn)
            if _first and len(_first.split()) <= 3 and _first[:1].isupper() and not _is_kf and len(parsed["options"]) <= 5 \
                    and not re.search(r"[.?!:;,]$", _first) and 70 <= _sum0 <= 98 and len(_first) >= 3:
                parsed["options"].append({"text": _first, "percentage": 100 - _sum0, "correct": False})
                parsed["explanation_raw"] = _er[len(_fp[0]):].lstrip("\n ")
                _repaired = f"option_{chr(96 + len(parsed['options']))}_recovered_from_explanation_line: '{_first}' (pct={100 - _sum0} sum se nikala -- check karo)"
        option_texts = [o["text"] for o in parsed["options"]]
        explanation_md = bold_explanation(parsed["explanation_raw"], option_texts)
        notes = parse_notes_by_indent(parsed["notes_lines"])
        if _st and _st.get("explanation_md") and len(re.sub(r"\W+", "", _st["explanation_md"])) >= 30:
            _kf = ""
            _mk = re.match(r"^((?:>[^\n]*\n?)+)", explanation_md.lstrip())
            if _st.get("keyfact"):
                _kf = "> " + _st["keyfact"]
            elif _mk:
                _kf = _mk.group(1).strip()
            explanation_md = (_kf + "\n\n" if _kf else "") + bold_explanation(_st["explanation_md"], option_texts)

        # PassMedicine-2: sahi jawab ka sab se bharosemand source = PDF mein
        # drawn GREEN BAR; phir explanation text; phir (aakhri) max-%.
        _sp = offset_to_page(page_bounds, char_start)
        _ep = min(offset_to_page(page_bounds, max(char_start, char_end - 1)), total_pages)
        correct_source = parsed.get("correct_source")
        _g = green_bar_correct_index(pdf_doc, _sp, _ep, parsed["options"])
        if _g is not None:
            for _k, _o in enumerate(parsed["options"]):
                _o["correct"] = (_k == _g)
            correct_source = "green_bar"
        _w = []
        _n = len(parsed["options"])
        _sum = sum(o["percentage"] for o in parsed["options"])
        _exp = None
        if pdf_path in _OCR_PDFS:
            try:
                _exp = _ocr_mod().ocr_expected_options(pdf_path, _sp0, _ep0)
            except Exception:
                _exp = None
        _w.extend(screen_options(parsed["options"], parsed["text"], None if _repaired else _exp))
        if _repaired:
            _w.append(_repaired)
        if _n and correct_source == "max_percentage":
            _w.append("correct_answer_guessed_from_max_percentage (green bar/explanation nahi mila)")
        if len(parsed["text"] or "") < 25:
            _w.append("stem_missing_or_short")
        if _w:
            qa_warnings[qnum] = _w

        for _sf in [d for d in diagram_files_by_qnum.get(qnum, []) if d.get("stem")]:
            parsed["text"] = (parsed["text"] or "") + f"\n\n![{_sf.get('caption', '')}]({_sf['file']})\n\n"
        parsed["text"] = apply_table_markers(parsed["text"], tbl_md)
        explanation_md = apply_table_markers(explanation_md, tbl_md)
        explanation_md = _clean_md_flow(explanation_md)
        options_ak = [
            {"id": chr(97 + i), "text": o["text"], "percentage": o["percentage"], "correct": o["correct"]}
            for i, o in enumerate(parsed["options"])
        ]
        correct_id = next((o["id"] for o in options_ak if o["correct"]), None)

        # is question ka PDF page-range nikalo, high-DPI pe render karo,
        # aur jis page pe diagram-box (beige background) mile usi ko crop
        # karke rakho -- baaki (sirf text wale) pages discard ho jate hain
        start_page = offset_to_page(page_bounds, char_start)
        end_page = offset_to_page(page_bounds, max(char_start, char_end - 1))
        end_page = min(end_page, total_pages)

        diagram_files = list(diagram_files_by_qnum.get(qnum, []))
        # Kabhi kabhi ek hi diagram DO alag jagah se link hota hai (jaise
        # stem aur notes dono mein alag CloudFront URLs, jo visually EXACT
        # same image hain) -- ya kisi purane resume/fallback cycle mein
        # dobara save ho gaya ho. Filename/idx alag hone ke bawajood agar
        # do files ka ASAL content (byte-for-byte) same hai, to unhe
        # duplicate maan kar sirf ek hi rakhte hain, taake same question
        # mein wahi image baar-baar na dikhe.
        diagram_files = _dedup_diagrams_by_content(diagram_files, out_dir)
        diagram_files = [d for d in diagram_files if not d.get("stem")]
        if offline_images:
            # --offline-images mode: humein pehle se pata hai diagram_files
            # (Phase 1 fast-path se) already complete/trustworthy hain --
            # is liye yahan koi bhi extra page-render, autocrop, ya embedded-
            # image-extraction bilkul nahi karte (ye sab naye files img_dir
            # mein likh dete, jo offline mode ka poora maksad hi khatam kar deta).
            pass
        elif no_diagrams:
            pass  # is pass mein diagrams bilkul skip -- baad mein --fill-images se aayengi
        total_diagrams += len(diagram_files)

        # Har diagram ko uske sahi PDF page ke notes-paragraph ke paas
        # (usi jagah jahan asal PDF mein wo dikhta hai) inline rakhte hain --
        # jis paragraph ka page number diagram ke page se match kare, uske
        # theek pehle ek image-marker daal dete hain, phir dobara table-
        # extraction aur line-flattening chalate hain is naye paragraph
        # list pe.
        notes_paragraphs_aug = []
        placed_pages = set()
        for para, offset in parsed["notes_paragraphs_with_offsets"]:
            if offset is not None:
                para_page = offset_to_page(page_bounds, char_start + offset)
                for dg in diagram_files:
                    if dg["page"] == para_page and dg["page"] not in placed_pages:
                        payload = CALLOUT_SEP.join([dg["file"], dg["caption"]])
                        notes_paragraphs_aug.append([(0, IMAGE_MARKER + payload)])
                        placed_pages.add(dg["page"])
            notes_paragraphs_aug.append(para)
        # jo diagrams kisi bhi notes-paragraph ke page se match nahi hue
        # (jaise stem/options wale page pe hon), unhe end mein rakh dete hain
        for dg in diagram_files:
            if dg["page"] not in placed_pages:
                payload = CALLOUT_SEP.join([dg["file"], dg["caption"]])
                notes_paragraphs_aug.append([(0, IMAGE_MARKER + payload)])

        notes_tables_md_aug, notes_paragraphs_aug = extract_tables(notes_paragraphs_aug)
        notes_lines_aug = notes_paragraphs_to_lines(notes_paragraphs_aug)
        notes = parse_notes_by_indent(notes_lines_aug)

        # diagram (agar hai) reference PDF ki tarah notes/RILE-box ke saath,
        # sabse aakhir mein rehta hai -- question ke top pe nahi
        notes_out = None
        if notes["heading"] or notes["sections"] or diagram_files or notes_tables_md_aug:
            body_md = sections_to_markdown_v2(notes)
            if parsed["notes_tables_md"]:
                body_md = (body_md + "\n\n" + "\n\n".join(parsed["notes_tables_md"])).strip()
            body_md = apply_table_markers(body_md, tbl_md)
            notes_out = {
                "heading": notes["heading"],
                "body_markdown": body_md,
                # diagrams ab body_markdown ke andar hi (sahi caption ke
                # paas) inline embed ho chuke hain -- alag flat list mein
                # dobara nahi dete, warna viewer mein duplicate dikhengi
                "images": [],
            }

        if _st and _st.get("notes_md"):
            if len(_st["notes_md"]) >= 20:
                _body = _st["notes_md"]
                for _dg in diagram_files:
                    if _dg["file"] not in _body:
                        _body += f"\n\n![{_dg.get('caption', '')}]({_dg['file']})"
                notes_out = {
                    "heading": _st.get("notes_heading") or (notes_out or {}).get("heading") or notes["heading"],
                    "body_markdown": _body,
                    "images": [],
                }
        if notes_out and notes_out.get("body_markdown"):
            notes_out["body_markdown"] = _clean_md_flow(notes_out["body_markdown"])
        if pdf_path in _OCR_PDFS and not _st:
            try:
                _bd = os.path.join(out_dir, "ocr_debug", "blocks")
                os.makedirs(_bd, exist_ok=True)
                with open(os.path.join(_bd, f"Q{qnum}_FAILED.txt"), "w", encoding="utf-8") as _bf:
                    _bf.write("ocr_blocks fail: " + str(_ocr_mod().OCR_BLOCKS_FAIL[0]) + "\n")
            except Exception:
                pass
        if _st:
            try:
                _bd = os.path.join(out_dir, "ocr_debug", "blocks")
                os.makedirs(_bd, exist_ok=True)
                with open(os.path.join(_bd, f"Q{qnum}.md"), "w", encoding="utf-8") as _bf:
                    _bf.write(f"KEYFACT: {_st.get('keyfact')}\n\n--- EXPLANATION ---\n{_st.get('explanation_md')}\n\n--- NOTES: {_st.get('notes_heading')} ---\n{_st.get('notes_md')}\n")
            except Exception:
                pass

        ak_entry = {
            "id": f"q{qnum}",
            "number": qnum,
            "text": parsed["text"],
            "options": options_ak,
            "attempted": True,
            "correct_answer_id": correct_id,
            "correct_source": correct_source,
            "explanation": explanation_md,
            "notes": notes_out,
            "set_total": CHUNK_META.get(qnum, {}).get("set_total"),
            "set_number": CHUNK_META.get(qnum, {}).get("set_number"),
            "warnings": qa_warnings.get(qnum, []),
            "content_map": tail_meta["content_map"],
            "links": tail_meta["links"],
            "textbooks": tail_meta.get("textbooks", []),
        }
        sv_entry = {
            "id": f"q{qnum}",
            "number": qnum,
            "text": parsed["text"],
            "options": [{"id": o["id"], "text": o["text"]} for o in options_ak],
            "attempted": False,
            "student_answer": None,
            "correct_answer_id": None,
            "explanation": None,
            "notes": notes_out,
            "set_total": CHUNK_META.get(qnum, {}).get("set_total"),
            "set_number": CHUNK_META.get(qnum, {}).get("set_number"),
            "content_map": tail_meta["content_map"],
            "links": tail_meta["links"],
            "textbooks": tail_meta.get("textbooks", []),
        }
        if qnum in ak_index_by_qnum:
            # --fill-images: pehle se entry maujood thi -- update karo, naya append mat karo
            answer_key_questions[ak_index_by_qnum[qnum]] = ak_entry
            student_view_questions[sv_index_by_qnum[qnum]] = sv_entry
        else:
            ak_index_by_qnum[qnum] = len(answer_key_questions)
            sv_index_by_qnum[qnum] = len(student_view_questions)
            answer_key_questions.append(ak_entry)
            student_view_questions.append(sv_entry)

        save_progress()  # har question ke baad turant save -- Ctrl+C pe bhi data safe
        print(f"\r{C.CYAN}      ... Q{qnum} processed ({len(answer_key_questions)}/{total}){C.RESET}", end="", flush=True)

    print()  # counter line ke baad newline
    print(f"{C.YELLOW}      -> {total_diagrams} diagram image(s) mapped to their questions in {img_dir}/{C.RESET}")

    save_progress()

    from collections import Counter as _Counter
    _src = _Counter(q.get("correct_source") for q in answer_key_questions)
    print(f"{C.CYAN}      -> correct-answer source: {dict(_src)}{C.RESET}")
    if qa_warnings:
        print(f"{C.YELLOW}      -> {len(qa_warnings)} question(s) mein warning (JSON ke 'warnings' field mein bhi):{C.RESET}")
        for _q, _ws in list(qa_warnings.items())[:25]:
            print(f"{C.YELLOW}         Q{_q}: {'; '.join(_ws)}{C.RESET}")
        _atomic_write_json(os.path.join(out_dir, f"{source_name}_qa_report.json"), qa_warnings)

    print(f"\n{C.GREEN}{C.BOLD}Done.{C.RESET}\n{C.GREEN}  Answer key   -> {ak_path}\n  Student view -> {sv_path}\n  Images       -> {img_dir}/{C.RESET}")
    write_bundled_viewer(out_dir, ak_path)
    if pdf_path in _OCR_PDFS:
        try:
            _dd = _ocr_mod().ocr_debug_dump(pdf_path, out_dir)
            print(f"{C.CYAN}  OCR debug    -> {_dd}/  (ocr_raw_layout.txt + pages/ + question_pages.json){C.RESET}")
        except Exception as _e:
            print(f"{C.YELLOW}  [ocr_debug] nahi bana: {_e}{C.RESET}")


_VIEWER_TPL = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>M2 QBank Viewer</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#272c36;--tx:#e8eaf0;--mut:#8b93a5;--ac:#4c8dff;--ok:#2ecc71;--bad:#ff5c5c;--warn:#f5b942}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
button,input,select{font:inherit;color:inherit}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 14px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
header h1{font-size:16px;margin:0 8px 0 0}
.btn{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:7px 12px;cursor:pointer}
.btn:hover{border-color:var(--ac)}
.btn.pri{background:var(--ac);border-color:var(--ac);color:#fff}
.btn:disabled{opacity:.4;cursor:default}
select,input[type=search]{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
.score{margin-left:auto;color:var(--mut);font-size:14px}
main{max-width:860px;margin:0 auto;padding:14px 14px 90px}
.empty{text-align:center;margin-top:12vh;color:var(--mut)}
.empty .btn{margin:6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.meta{color:var(--mut);font-size:13px;margin-bottom:8px;display:flex;gap:10px;flex-wrap:wrap}
.tag{border:1px solid var(--line);border-radius:99px;padding:0 9px;font-size:12px}
.tag.w{color:var(--warn);border-color:var(--warn)}
.stem{margin-bottom:12px}
.opt{display:flex;gap:10px;align-items:flex-start;width:100%;text-align:left;background:transparent;border:1px solid var(--line);border-radius:10px;padding:11px 12px;margin:7px 0;cursor:pointer;position:relative;overflow:hidden}
.opt:hover{border-color:var(--ac)}
.opt .l{font-weight:600;color:var(--mut);min-width:20px}
.opt .t{flex:1}
.opt .p{color:var(--mut);font-size:13px;white-space:nowrap}
.opt.correct{border-color:var(--ok);background:rgba(46,204,113,.12)}
.opt.wrong{border-color:var(--bad);background:rgba(255,92,92,.12)}
.opt.locked{cursor:default}
.opt .bar{position:absolute;left:0;bottom:0;height:3px;background:var(--ac);opacity:.55}
h3{margin:18px 0 6px;font-size:17px}
.md h2,.md h4{margin:16px 0 4px;font-size:16px;color:var(--ac)}
.md ul{margin:4px 0 8px;padding-left:22px}
.md ul ul{margin:2px 0}
.md blockquote{margin:8px 0;padding:8px 12px;border-left:3px solid var(--ac);background:rgba(76,141,255,.08);border-radius:0 8px 8px 0}
.md p{margin:8px 0}
.md img{max-width:100%;border-radius:8px;background:#fff;margin:8px 0;display:block}
.md table{border-collapse:collapse;margin:8px 0;display:block;overflow-x:auto}
.md th,.md td{border:1px solid var(--line);padding:5px 9px}
.md th{background:#1d212b}
.chips span{display:inline-block;background:#1d212b;border:1px solid var(--line);border-radius:99px;padding:1px 10px;margin:2px 4px 2px 0;font-size:13px}
.nav{position:fixed;left:0;right:0;bottom:0;background:var(--bg);border-top:1px solid var(--line);padding:10px 14px;display:flex;gap:8px;justify-content:center}
.nav .btn{flex:1;max-width:200px}
.list{display:grid;gap:6px}
.row{display:flex;gap:10px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 12px;cursor:pointer;align-items:center}
.row:hover{border-color:var(--ac)}
.row .n{color:var(--mut);min-width:44px;font-variant-numeric:tabular-nums}
.row .s{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dot{width:10px;height:10px;border-radius:50%;background:var(--line)}
.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
a{color:var(--ac)}
</style>
</head>
<body>
<header>
  <h1>M2 QBank</h1>
  <button class="btn pri" id="bJson">Open JSON</button>
  <button class="btn" id="bImg">Images (select all)</button>
  <select id="fSet"></select>
  <select id="fMode"><option value="all">All</option><option value="warn">Flagged</option><option value="todo">Unanswered</option><option value="wrong">Wrong</option></select>
  <input type="search" id="fQ" placeholder="Search&#8230;" size="12">
  <button class="btn" id="bList">List</button>
  <span class="score" id="score"></span>
  <input type="file" id="fJson" hidden>
  <input type="file" id="fImg" multiple hidden>
</header>
<main id="main"></main>
<div class="nav" id="nav" hidden>
  <button class="btn" id="bPrev">&#8592; Prev</button>
  <button class="btn" id="bShow">Show answer</button>
  <button class="btn pri" id="bNext">Next &#8594;</button>
</div>

<script>
const $=s=>document.querySelector(s);
let ALL=[],VIEW=[],idx=0,imgMap={},listMode=false,srcName='';
let ST={}; // number -> {pick, revealed}
const key=()=>'m2v:'+srcName;
function load(){try{ST=JSON.parse(localStorage.getItem(key())||'{}')}catch(e){ST={}}}
function save(){try{localStorage.setItem(key(),JSON.stringify(ST))}catch(e){}}

$('#bJson').onclick=()=>$('#fJson').click();
$('#bImg').onclick=()=>$('#fImg').click();
$('#fJson').onchange=async e=>{
  const f=e.target.files[0]; if(!f)return;
  try{
    const d=JSON.parse(await f.text());
    ALL=(d.questions||(Array.isArray(d)?d:[d.question&&{...d.question,notes:d.notes}])).filter(Boolean);
    srcName=f.name; load(); buildSets(); applyFilter(); idx=0; listMode=false; render();
  }catch(err){alert('JSON read error: '+err.message)}
};
$('#fImg').onchange=e=>{
  imgMap={};
  for(const f of e.target.files) imgMap[f.name]=URL.createObjectURL(f);
  render();
};
function buildSets(){
  const sets=[...new Set(ALL.map(q=>q.set_total).filter(x=>x))];
  $('#fSet').innerHTML='<option value="">All banks</option>'+sets.map(s=>`<option value="${s}">Bank of ${s}</option>`).join('');
}
['fSet','fMode','fQ'].forEach(id=>$('#'+id).addEventListener('input',()=>{applyFilter();idx=0;render()}));
$('#bList').onclick=()=>{listMode=!listMode;render()};
$('#bPrev').onclick=()=>go(-1);
$('#bNext').onclick=()=>go(1);
$('#bShow').onclick=()=>{const q=VIEW[idx];if(!q)return;(ST[q.number]=ST[q.number]||{}).revealed=true;save();render()};
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT')return;
  if(e.key==='ArrowRight')go(1);else if(e.key==='ArrowLeft')go(-1);
  else if('12345678'.includes(e.key)&&e.key)pick(+e.key-1);
});
function go(d){idx=Math.max(0,Math.min(VIEW.length-1,idx+d));render();scrollTo(0,0)}

function applyFilter(){
  const s=$('#fSet').value,m=$('#fMode').value,t=$('#fQ').value.trim().toLowerCase();
  VIEW=ALL.filter(q=>{
    if(s&&String(q.set_total)!==s)return false;
    const st=ST[q.number]||{};
    if(m==='warn'&&!(q.warnings&&q.warnings.length))return false;
    if(m==='todo'&&st.pick!=null)return false;
    if(m==='wrong'&&!(st.pick!=null&&st.pick!==q.correct_answer_id))return false;
    if(t){const hay=(q.text+' '+(q.options||[]).map(o=>o.text).join(' ')+' '+(q.notes&&q.notes.heading||'')).toLowerCase();
      if(!hay.includes(t)&&String(q.number)!==t)return false}
    return true;
  });
}
function pick(i){
  const q=VIEW[idx]; if(!q||!q.options[i])return;
  const st=ST[q.number]=ST[q.number]||{};
  if(st.pick!=null)return;
  st.pick=q.options[i].id; st.revealed=true; save(); render();
}

function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function inline(s){
  s=esc(s).replace(/&lt;br&gt;/g,'<br>');
  s=s.replace(/!\[([^\]]*)\]\(([^)]+)\)/g,(m,a,u)=>{
    const name=u.split('/').pop();
    const src=imgMap[name]||u;
    return `<img alt="${a}" src="${src}" onerror="this.alt='[missing image: ${name}] \u2013 tap Images'">`});
  s=s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,'$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  s=s.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/(^|[^*])\*(?!\s)(.+?)\*(?!\*)/g,'$1<i>$2</i>');
  return s;
}
function md(src){
  if(!src)return '';
  const L=src.split('\n');let out='',stack=[],i=0;
  const closeTo=n=>{while(stack.length>n){out+='</ul>';stack.pop()}};
  while(i<L.length){
    const l=L[i];
    if(/^\s*\|.*\|\s*$/.test(l)&&i+1<L.length&&/^\s*\|[\s:|-]+\|\s*$/.test(L[i+1])){
      closeTo(0);const row=r=>r.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
      {const hc=row(l);out+='<table>'+(hc.every(c=>!String(c).trim())?'':'<tr>'+hc.map(c=>`<th>${inline(c)}</th>`).join('')+'</tr>');i+=2;}
      while(i<L.length&&/^\s*\|.*\|\s*$/.test(L[i])){out+='<tr>'+row(L[i]).map(c=>`<td>${inline(c)}</td>`).join('')+'</tr>';i++}
      out+='</table>';continue}
    let m;
    if((m=l.match(/^(#{1,4})\s+(.*)/))){closeTo(0);out+=`<h2>${inline(m[2])}</h2>`}
    else if((m=l.match(/^(\s*)-\s+(.*)/))){const d=Math.floor(m[1].length/2)+1;
      while(stack.length<d){out+='<ul>';stack.push(1)}closeTo(d);out+=`<li>${inline(m[2])}</li>`}
    else if(l.startsWith('>')){closeTo(0);let b=[];while(i<L.length&&L[i].startsWith('>')){b.push(L[i].replace(/^>\s?/,''));i++}
      out+=`<blockquote>${inline(b.join(' '))}</blockquote>`;continue}
    else if(l.trim()===''){closeTo(0)}
    else{closeTo(0);out+=`<p>${inline(l)}</p>`}
    i++;
  }
  closeTo(0);return out;
}

function render(){
  const M=$('#main'),nav=$('#nav');
  if(!ALL.length){
    nav.hidden=true;
    M.innerHTML=`<div class="empty"><h2>Open your parsed JSON</h2>
    <p>Pick <code>\u2026_answer_key.json</code> from the output folder.<br>Then tap <b>Images (select all)</b>, open the <code>images</code> folder and select all files so diagrams show.</p>
    <button class="btn pri" onclick="$('#bJson').click()">Open JSON</button></div>`;return}
  const done=ALL.filter(q=>(ST[q.number]||{}).pick!=null);
  const ok=done.filter(q=>ST[q.number].pick===q.correct_answer_id).length;
  $('#score').textContent=`${ok}/${done.length} correct \u00b7 ${VIEW.length} shown`;
  if(listMode){
    nav.hidden=true;
    M.innerHTML='<div class="list">'+VIEW.map((q,i)=>{
      const st=ST[q.number]||{};const c=st.pick==null?'':(st.pick===q.correct_answer_id?'ok':'bad');
      return `<div class="row" data-i="${i}"><span class="n">${q.set_total?'B'+q.set_total+'\u00b7':''}${q.set_number||q.number}</span><span class="s">${esc(q.text.replace(/\s+/g,' '))}</span>${q.warnings&&q.warnings.length?'<span class="tag w">!</span>':''}<span class="dot ${c}"></span></div>`}).join('')+'</div>';
    M.querySelectorAll('.row').forEach(r=>r.onclick=()=>{idx=+r.dataset.i;listMode=false;render();scrollTo(0,0)});
    return}
  if(!VIEW.length){nav.hidden=true;M.innerHTML='<div class="empty">No questions match this filter.</div>';return}
  idx=Math.min(idx,VIEW.length-1);
  const q=VIEW[idx],st=ST[q.number]||{},rev=!!st.revealed;
  const warn=(q.warnings||[]).map(w=>`<div class="tag w" style="border-radius:8px;margin:2px 0">\u26a0 ${esc(w)}</div>`).join('');
  let h=`<div class="card"><div class="meta"><span>Q${idx+1} / ${VIEW.length}</span>
    ${q.set_total?`<span class="tag">Bank of ${q.set_total} \u00b7 #${q.set_number}</span>`:''}
    ${q.correct_source?`<span class="tag">answer: ${esc(q.correct_source)}</span>`:''}</div>${warn}
    <div class="stem md">${md(q.text)}</div>`;
  (q.options||[]).forEach((o,i)=>{
    let c='';if(rev){c=o.correct?' correct':(st.pick===o.id?' wrong':'')}
    h+=`<button class="opt${c}${rev?' locked':''}" data-i="${i}"><span class="l">${String.fromCharCode(65+i)}</span><span class="t md">${inline(o.text)}</span>${rev&&o.percentage!=null?`<span class="p">${o.percentage}%</span><span class="bar" style="width:${o.percentage}%"></span>`:''}</button>`});
  h+='</div>';
  if(rev){
    if(q.explanation)h+=`<div class="card"><h3 style="margin-top:0">Explanation</h3><div class="md">${md(q.explanation)}</div></div>`;
    const n=q.notes;
    if(n&&(n.heading||n.body_markdown))h+=`<div class="card"><h3 style="margin-top:0">${esc(n.heading||'Notes')}</h3><div class="md">${md((n.body_markdown||'').replace(/^#\s+.*\n+/,''))}</div></div>`;
    const cm=q.content_map||{};
    const cmk=Object.keys(cm).filter(k=>cm[k].length);
    if(cmk.length)h+='<div class="card chips"><h3 style="margin-top:0">MRCP content map</h3>'+cmk.map(k=>`<div><small style="color:var(--mut)">${esc(k)}</small><br>${cm[k].map(v=>`<span>${esc(v)}</span>`).join('')}</div>`).join('')+'</div>';
    if((q.links||[]).length)h+='<div class="card"><h3 style="margin-top:0">Links</h3>'+q.links.map(l=>`<div style="margin:6px 0">${l.source?`<b>${esc(l.source)}</b> \u2013 `:''}${l.url?`<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.title)}</a>`:esc(l.title)}</div>`).join('')+'</div>';
  }
  M.innerHTML=h;
  M.querySelectorAll('.opt').forEach(b=>b.onclick=()=>pick(+b.dataset.i));
  nav.hidden=false;
  $('#bPrev').disabled=idx===0;$('#bNext').disabled=idx===VIEW.length-1;
  $('#bShow').style.display=rev?'none':'';
}
/*M2_EMBED*/
if(window.M2_DATA){
  ALL=window.M2_DATA.questions||[];srcName=window.M2_NAME||'embedded';
  imgMap=window.M2_IMGS||{};
  load();buildSets();applyFilter();
  document.getElementById('bJson').style.display='none';document.getElementById('bImg').style.display='none';
}
render();
</script>
</body>
</html>
'''


def write_bundled_viewer(out_dir, ak_path):
    """Sab kuch (JSON + images) ek hi viewer.html mein bhar deta hai -- file-picker ki zaroorat nahi."""
    try:
        import base64, mimetypes
        data = json.load(open(ak_path, encoding="utf-8"))
        imgs = {}
        img_dir = os.path.join(out_dir, "images")

        def _pack(fp, maxpx, q):
            # viewer ke liye image chhoti karo (JPEG) taake saari images embed ho jayein
            try:
                from io import BytesIO
                im = Image.open(fp)
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                else:
                    im = im.convert("RGB")
                im.thumbnail((maxpx, maxpx))
                buf = BytesIO()
                im.save(buf, "JPEG", quality=q, optimize=True)
                return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
            except Exception:
                mt = mimetypes.guess_type(fp)[0] or "image/png"
                return "data:%s;base64,%s" % (mt, base64.b64encode(open(fp, "rb").read()).decode())

        files = []
        if os.path.isdir(img_dir):
            files = [f for f in sorted(os.listdir(img_dir))
                     if (mimetypes.guess_type(f)[0] or "").startswith("image/")]
        for maxpx, q in ((2400, 88), (1600, 80), (1000, 72), (800, 60), (600, 50)):
            imgs, total = {}, 0
            for fn in files:
                s = _pack(os.path.join(img_dir, fn), maxpx, q)
                imgs[fn] = s
                total += len(s)
            if total < 60e6:
                break
        print(f"{C.CYAN}[viewer] {len(imgs)} images embed hui ({total/1e6:.0f} MB, chhoti karke){C.RESET}")
        emb = "window.M2_NAME=%s;window.M2_DATA=%s;window.M2_IMGS=%s;" % (
            json.dumps(os.path.basename(ak_path)),
            json.dumps(data, ensure_ascii=True).replace("</", "<\\/"), json.dumps(imgs))
        html = _VIEWER_TPL.replace("/*M2_EMBED*/", emb)
        vp = os.path.join(out_dir, "viewer.html")
        open(vp, "w", encoding="utf-8").write(html)  # sab ASCII hai -> charset ka masla nahi
        print(f"{C.GREEN}  Viewer       -> {vp}  ({os.path.getsize(vp)/1e6:.1f} MB, seedha Chrome mein kholo){C.RESET}")
    except Exception as e:
        print(f"{C.YELLOW}[viewer] bundle nahi bana: {e}{C.RESET}")


# ---------------------------------------------------------------------------
# AMEDEX MODE (--amedex)
# Amedex AMC QBank PDFs are iPhone screenshots of the Amedex app (no text layer).
# Each question spans several screens: stem (+image), options, the same options
# with green ✓ / red ✗ marks, then "Option X is correct" explanation screens.
# Zoomed landscape screens repeat an image or table. Screens overlap when the
# user scrolled, so text from consecutive screens is merged on the overlap.
# ---------------------------------------------------------------------------

AMX_HEADER = 205          # app bar height in a 1170-wide portrait screenshot
AMX_DIV_X = (250, 290)    # grey divider between the option letter cell and the option text
AMX_OPT_TEXT_X = 285      # option text starts right of this x
AMX_BULLETS = set("•●◦∙·«»*°©eo-–")


_AMX_NOTES = []


_AMX_REPAIRS = []
_AMX_STACK_FAILED = False


def _amx_note(msg):
    """Review flag for the question being built: something the parser could not settle for sure."""
    if msg not in _AMX_NOTES:
        _AMX_NOTES.append(msg)


def _amx_repair(msg):
    """A repair the parser is sure about (the full text was seen on another screen, a known OCR slip, ...):
    recorded in the question's `repairs`, but the question is not flagged."""
    if msg not in _AMX_REPAIRS:
        _AMX_REPAIRS.append(msg)


import glob


def _amx_work_dir(pdf_path):
    d = os.environ.get("M2_AMX_WORK") or os.path.join(
        os.path.expanduser("~/.m2_ocr"), "amedex_" + Path(pdf_path).stem.replace(" ", "_"))
    os.makedirs(os.path.join(d, "pages"), exist_ok=True)
    os.makedirs(os.path.join(d, "ocr"), exist_ok=True)
    return d


def _amx_extract_pages(pdf_path, work):
    pages = sorted(glob.glob(os.path.join(work, "pages", "p-*.*")))
    if pages:
        return pages
    # -j keeps the original JPEG screenshots (no re-encode, takes about a second)
    subprocess.run(["pdfimages", "-j", pdf_path, os.path.join(work, "pages", "p")], check=True)
    return sorted(glob.glob(os.path.join(work, "pages", "p-*.*")))


def _amx_ocr_pages(pages, work, workers=8):
    from concurrent.futures import ThreadPoolExecutor
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    todo = []
    for p in pages:
        base = os.path.join(work, "ocr", Path(p).stem)
        tsv = base + ".tsv"
        if not (os.path.exists(tsv) and sum(1 for _ in open(tsv, encoding="utf-8", errors="replace")) > 1):
            todo.append((p, base))
    done = [len(pages) - len(todo)]

    def one(a):
        p, base = a
        subprocess.run(["tesseract", p, base, "--psm", "4", "-l", "eng", "tsv", "txt"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        done[0] += 1
        print(f"OCR: {done[0]}/{len(pages)} pages", flush=True)
        _amx_prog("amx-ocr", "Amedex OCR", done[0], len(pages), "pages", Path(p).name)

    if todo:
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(one, todo))


def _amx_words(tsv_path):
    words = []
    try:
        rows = open(tsv_path, encoding="utf-8", errors="replace").read().splitlines()[1:]
    except OSError:
        return words
    for r in rows:
        c = r.split("\t")
        if len(c) < 12 or c[0] != "5" or not c[11].strip():
            continue
        if float(c[10]) < 30 and not any(ch.isalnum() for ch in c[11]):
            continue                      # OCR noise from icons / the chat bubble
        conf = float(c[10]) if float(c[10]) != 0 else 60.0      # 0 = tesseract's no-score quirk on first words
        words.append({"t": c[11].strip(), "l": int(c[6]), "y": int(c[7]), "w": int(c[8]), "h": int(c[9]),
                      "c": conf, "k": (int(c[2]), int(c[3]), int(c[4]))})
    return words


def _amx_lines(words):
    """Groups OCR words into lines, ordered top to bottom."""
    by = {}
    for w in words:
        by.setdefault(w["k"], []).append(w)
    lines = []
    for ws in by.values():
        ws.sort(key=lambda w: w["l"])
        good = [w for w in ws if w["c"] >= 60] or ws
        top = min(w["y"] for w in good); bot = max(w["y"] + w["h"] for w in good)
        lines.append({"words": ws, "text": " ".join(w["t"] for w in ws), "left": ws[0]["l"],
                      "right": max(w["l"] + w["w"] for w in ws), "top": top, "bottom": bot,
                      "conf": sum(w["c"] for w in ws) / len(ws)})
    # tesseract sometimes splits one visual line into two blocks: merge lines that share a baseline
    lines.sort(key=lambda L: (L["top"], L["left"]))
    out = []
    for L in lines:
        if out and abs(L["top"] - out[-1]["top"]) < 12 and L["left"] > out[-1]["right"]:
            P = out[-1]
            P["words"] += L["words"]; P["text"] += " " + L["text"]
            P["right"] = L["right"]; P["bottom"] = max(P["bottom"], L["bottom"])
        else:
            out.append(L)
    return out


def _amx_norm(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _amx_sim(a, b):
    import difflib
    a, b = _amx_norm(a), _amx_norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _amx_is_text(L):
    t = L["text"]
    alnum = sum(ch.isalnum() for ch in t)
    chars = t.replace(" ", "")
    if any(ch.isdigit() for ch in t):   # "80-90%." / "<0.4" / "(≥2)": number punctuation is real text
        chars = "".join(ch for ch in chars if ch not in "-–%.,/<>≥≤():")
    ratio = alnum / max(1, len(chars))
    return (alnum >= 3 and ratio >= 0.6 and L["conf"] >= 35) or (alnum == 2 and ratio >= 0.6 and L["conf"] >= 70)


def _amx_table_zones(px, W, H):
    """Tables (and their grey footnote box) have a dark left border near x=108; returns [(top, bottom)]."""
    best = None
    for x in range(95, 125):
        runs = []; s = None
        for y in range(AMX_HEADER - 10, H):
            p = px[x, y]
            d = (p[0] + p[1] + p[2]) / 3 < 110
            if d and s is None:
                s = y
            elif not d and s is not None:
                if y - s > 150:
                    runs.append([s, y])
                s = None
        if s is not None and H - s > 150:
            runs.append([s, H])
        tot = sum(b - a for a, b in runs)
        if runs and (not best or tot > best[0]):
            best = (tot, runs)
    zones = best[1] if best else []
    for z in zones:
        if z[1] > H - 160:          # cut by the screen bottom (the grey "up" button breaks the border)
            z[1] = H
    return zones


def _amx_bar_kind(px, W):
    """Colour of the app bar: teal = normal screen, dark grey = dimmed overlay."""
    r = g = b = 0; n = 0
    try:
        for x in range(W // 3, W * 2 // 3, 7):
            for y in range(70, 130, 7):
                p = px[x, y]; r += p[0]; g += p[1]; b += p[2]; n += 1
    except IndexError:
        return "other"      # image shorter than the app bar (icon / thin strip), not a phone screenshot
    if not n:
        return "other"
    r, g, b = r / n, g / n, b / n
    if b > 90 and g > 60 and r < 70 and b > r + 40:
        return "app"
    if max(r, g, b) < 90 and max(r, g, b) - min(r, g, b) < 25:
        return "dim"
    return "other"


def _amx_option_boxes(px, W, H):
    """Finds option boxes by their grey letter/text divider and reads the ✓/✗ mark of each."""
    best = None
    for x in range(*AMX_DIV_X):
        n = 0
        for y in range(AMX_HEADER, H - 80, 3):
            p = px[x, y]
            if 195 <= p[0] <= 238 and abs(p[0] - p[2]) < 14 and abs(p[0] - p[1]) < 14:
                n += 1
        if not best or n > best[1]:
            best = (x, n)
    if not best or best[1] < 25:
        return []
    x = best[0]; boxes = []; s = None
    for y in range(AMX_HEADER - 5, H):
        p = px[x, y]
        grey = 195 <= p[0] <= 238 and abs(p[0] - p[2]) < 14 and abs(p[0] - p[1]) < 14
        if grey and s is None:
            s = y
        elif not grey and s is not None:
            if y - s > 80:
                boxes.append([s, y])
            s = None
    if s is not None and H - s > 80:
        boxes.append([s, H])
    # a divider is a thin grey line between two white cells; a grey-filled panel (NOTE box, references) is not
    def _thin(top, bot):
        ys = range(top + 5, bot - 5, 6)
        ok = sum(1 for y in ys if min(px[x - 9, y][:3]) > 238 and min(px[x + 9, y][:3]) > 238)
        return ok >= 0.6 * max(1, len(ys))
    boxes = [b for b in boxes if _thin(*b)]
    out = []
    for top, bot in boxes:
        gr = rd = 0
        for yy in range(top, bot, 2):
            for xx in range(35, 125, 2):
                r, g, b = px[xx, yy][:3]
                if g > 100 and r < 90 and b < 90:
                    gr += 1
                elif r > 190 and g < 90 and b < 90:
                    rd += 1
        mark = "correct" if gr > 20 else "wrong" if rd > 20 else None
        out.append({"top": top, "bottom": bot, "mark": mark,
                    "cut": top <= AMX_HEADER + 2 or bot >= H - 2})
    return out


def _amx_content_bbox(px, x0, y0, x1, y1, min_run=120):
    """Largest block of non-white rows (a picture) inside a region; None if nothing big."""
    def ink(p):
        return (max(p[:3]) - min(p[:3]) > 30) or p[0] < 185
    rows = []
    for y in range(y0, y1):
        c = 0
        for x in range(x0, x1, 4):
            if ink(px[x, y]):
                c += 1
        rows.append(c)
    best = None; s = None
    for i, c in enumerate(rows + [0]):
        if c > 12 and s is None:
            s = i
        elif c <= 12 and s is not None:
            if i - s >= min_run and (not best or i - s > best[1] - best[0]):
                best = (s, i)
            s = None
    if not best:
        return None
    ya, yb = y0 + best[0], y0 + best[1]
    cols = [x for x in range(x0, x1, 2) if sum(1 for y in range(ya, yb, 6) if ink(px[x, y])) > 3]
    if not cols:
        return None
    return (min(cols), ya, max(cols) + 2, yb)


def _amx_analyse(img_path, tsv_path):
    im = Image.open(img_path).convert("RGB"); W, H = im.size; px = im.load()
    pg = {"path": img_path, "W": W, "H": H, "img": im}
    if W < 140 or H < 140:       # tiny embedded image (icon, divider strip): not a screenshot, skip it
        pg["kind"] = "other"; return pg
    if W > H:
        pg["kind"] = "land"; return pg
    bar = _amx_bar_kind(px, W)
    if bar != "app":
        pg["kind"] = "dim" if bar == "dim" else "other"; return pg
    all_lines = _amx_lines(_amx_words(tsv_path))
    lines = [L for L in all_lines
             if (L["top"] >= AMX_HEADER or (L["top"] >= AMX_HEADER - 25 and L["conf"] >= 88))
             and L["bottom"] <= H - 55]
    pg["tail_lines"] = [L for L in all_lines if L["bottom"] > H - 55 and L["top"] > H - 140]
    lines = [L for L in lines if not re.search(r"AMEDEX\s*PTY|All\s+Rights\s+Reserved", L["text"], re.I)]
    import numpy as np
    arr = np.asarray(im, dtype=np.int16)[H - 700:]
    bub = (arr[..., 2] > 170) & (arr[..., 0] < 70) & (arr[..., 1] > 90) & (arr[..., 1] < 150)
    ys, xs = np.where(bub)
    if len(xs) > 2000 and xs.min() > W * 0.7:
        bx0, by0, by1 = int(xs.min()), int(ys.min()) + H - 700, int(ys.max()) + H - 700
        rights = sorted(L["right"] for L in lines if L["bottom"] < by0 - 10 or L["top"] > by1 + 10)   # lines clear of the bubble
        margin = rights[int(len(rights) * 0.95)] if rights else W - 100     # right edge of full text lines
        for k, L in enumerate(lines):
            ov = min(L["bottom"], by1) - max(L["top"], by0)          # vertical overlap with the bubble
            if not (L["right"] > bx0 - 45 and ov > 0.4 * max(1, L["bottom"] - L["top"])):
                continue
            # Text wraps greedily: if the next line's first word would have fit after what is visible here,
            # this line must go on under the bubble (hidden words). Otherwise it really ends here.
            cands = lines[k + 1:k + 3] + [M for M in all_lines if M["top"] > L["bottom"] - 5 and M not in lines]
            nxt = next((M for M in cands if M["top"] > L["bottom"] - 5 and abs(M["left"] - L["left"]) < 80
                        and M["words"]), None)
            space = 14
            gap_n = (nxt["top"] - L["bottom"]) if nxt else 0
            if nxt and L["text"].rstrip().endswith((".", ":", "?")) and (gap_n > 40 or nxt["left"] > L["left"] + 30):
                continue                                # sentence ended and the next line starts a new block
            if nxt is None:
                L["bubble"] = True                      # nothing to compare with: cannot tell
            elif L["right"] + space + nxt["words"][0]["w"] <= max(margin, bx0 - 5):
                L["bubble"] = True
            elif L["right"] >= bx0 - 6:
                L["bubble"] = True                      # last word itself runs into the bubble
    full = np.asarray(im, dtype=np.int16)
    lum = full.mean(axis=2)
    for L in lines:
        if L["top"] < AMX_HEADER:
            L["edge"] = True
        yc = (L["top"] + L["bottom"]) // 2
        xa, xb = max(0, L["left"] - 62), max(0, L["left"] - 12)
        reg = lum[max(0, yc - 10):yc + 10, xa:xb] < 100
        n = int(reg.sum())
        if 25 <= n <= 450 and int((reg.mean(axis=0) > 0.9).sum()) == 0 and int(reg.any(axis=1).sum()) < 18:
            L["dot"] = True
        box = full[L["top"]:L["bottom"], L["left"]:L["right"]].reshape(-1, 3)
        ink = box[box.mean(axis=1) < 170]
        if len(ink) > 50:
            r, g, b = ink.mean(axis=0)
            if b > r + 45 and b > 120:
                L["blue"] = True
    pg["boxes"] = _amx_option_boxes(px, W, H)
    # an option box has only its letter (or nothing) left of the divider; "Reference(s)" etc. means another panel
    def _left_words(b):
        return [w for L in lines for w in L["words"] if w["l"] + w["w"] < AMX_DIV_X[0]
                and b["top"] <= w["y"] + w["h"] / 2 <= b["bottom"] and re.search(r"[A-Za-z]{4,}", w["t"])]
    pg["boxes"] = [b for b in pg["boxes"] if not _left_words(b)]
    pg["raw_boxes"] = pg["boxes"]
    if len(pg["boxes"]) < 2:        # a lone grey box is the Reference(s) panel, not an option
        pg["boxes"] = []
    pg["lines"] = lines
    pg["zones"] = []
    for L in lines:
        m = re.match(r"^\W*Option\s+([A-E])\s+is\s+correct", L["text"], re.I)
        if m:
            pg["kind"] = "expl"; pg["answer"] = m.group(1).upper(); pg["head_y"] = L["bottom"]
            pg["zones"] = _amx_table_zones(px, W, H)
            return pg
    if pg["boxes"]:
        pg["kind"] = "opts"; return pg
    txt = [L for L in lines if _amx_is_text(L)]
    lefts = sorted(L["left"] for L in txt)
    pg["font_size"] = any(re.search(r"Font\s*Siz|Answer later", L["text"], re.I) for L in lines)
    if pg["font_size"] or (lefts and lefts[len(lefts) // 2] < 92):
        pg["kind"] = "stem"
    elif txt:
        pg["kind"] = "exptail"
    else:
        pg["kind"] = "other"
    if pg["kind"] != "stem":
        pg["zones"] = _amx_table_zones(px, W, H)
    return pg


def _amx_drop_seen(acc, new):
    """Drops text lines of `new` already present in the last part of `acc` even when wrapped differently
    (a zoomed landscape screen wraps lines wider than the portrait screens)."""
    tail = "".join(_amx_norm(L["text"]) for L in acc[-40:] if "row" not in L)
    out = []
    for L in new:
        n = _amx_norm(L["text"])
        if "row" not in L and len(n) >= 15 and (n in tail or (len(n) > 40 and n[:40] in tail and n[-25:] in tail)):
            continue
        out.append(L)
    return out


def _amx_merge(acc, new):
    """Appends `new` lines to `acc`, skipping the part both screenshots show (scroll overlap)."""
    if not acc:
        return list(new), True
    if not new:
        return acc, True
    lo = max(0, len(acc) - 40)
    for j in range(lo, len(acc)):
        if _amx_sim(acc[j]["text"], new[0]["text"]) < 0.85:
            continue
        span = min(len(acc) - j, len(new))
        ok = sum(1 for i in range(span) if _amx_sim(acc[j + i]["text"], new[i]["text"]) >= 0.8)
        if ok >= max(1, int(span * 0.8)):
            acc = list(acc)
            for i in range(span):
                a, b = acc[j + i], new[i]
                if "row" not in a and "row" not in b and _amx_sim(a["text"], b["text"]) >= 0.75 \
                        and (len(b["text"]) > len(a["text"]) + 1 or (a.get("edge") and not b.get("edge"))):
                    _amx_repair(f"line ka aakhri hissa ek screen par chhupa tha, doosri screen se poora liya: '{b['text'][-40:]}'")
                    acc[j + i] = dict(b, gap=a.get("gap"))
            return acc + new[span:], True
    return acc + new, False


def _amx_scroll_continues(prev, new):
    """True when the top of `new` (just under the app bar) is a strip that `prev` also shows: same scroll, no gap."""
    from PIL import ImageChops
    W = prev["W"]
    a = prev["img"].convert("L").resize((W // 4, prev["H"] // 4))
    b = new["img"].convert("L").resize((W // 4, new["H"] // 4))
    first = min((L["top"] for L in new.get("lines", []) if L["top"] >= AMX_HEADER - 25), default=AMX_HEADER)
    h0, h = first // 4 + 2, 8                 # strip through the first text line of the new screen
    x0, x1 = 55, 95            # between the grey "up" button (left) and the home bar / chat bubble
    strip = b.crop((x0, h0, x1, h0 + h))
    if strip.getextrema()[0] > 150:      # blank strip proves nothing
        return False
    for y in range(AMX_HEADER // 4, a.size[1] - h):
        d = ImageChops.difference(a.crop((x0, y, x1, y + h)), strip)
        hist = d.histogram()
        if sum(i * n for i, n in enumerate(hist)) / (strip.size[0] * h) < 20:
            return True
    # the shared line may be cut in half at the very bottom of the screen above: compare only the top
    # of its letters, and accept the match only right at that bottom edge
    h2 = 4; strip2 = b.crop((x0, first // 4 + 1, x1, first // 4 + 1 + h2))
    for y in range(a.size[1] - 25, a.size[1] - h2):
        d = ImageChops.difference(a.crop((x0, y, x1, y + h2)), strip2)
        hist = d.histogram()
        if sum(i * n for i, n in enumerate(hist)) / (strip2.size[0] * h2) < 35:
            return True
    return False


def _amx_stem_lines(pg):
    rb = pg.get("raw_boxes") or pg.get("boxes") or []
    top_limit = rb[0]["top"] - 10 if rb else pg["H"]
    # scrolled to the top, the screen shows the question's info box ("ID" / "Title …") and the
    # "Flag as important" / "Answer later" boxes above the "Font Size" bar: the stem starts below that bar
    fs = next((L["bottom"] for L in pg["lines"] if re.search(r"Font\s*Siz", L["text"], re.I)), None)
    if fs is None:
        fs = next((L["bottom"] for L in pg["lines"] if re.search(r"Answer later|Flag as important", L["text"], re.I)), 0)
    return [L for L in pg["lines"] if fs < L["top"] and L["bottom"] < top_limit and _amx_is_text(L)
            and not re.search(r"Font\s*Siz|Answer later|Flag as important|^\W*(ID|Title)\b", L["text"], re.I)]


def _amx_box_text(pg, box):
    parts = []
    for L in pg["lines"]:            # lines are already top-to-bottom, words left-to-right
        ws = [w for w in L["words"] if w["l"] >= AMX_OPT_TEXT_X
              and w["y"] >= box["top"] - 4 and w["y"] + w["h"] <= box["bottom"] + 4]
        if ws:
            parts.append(" ".join(w["t"] for w in ws))
    return " ".join(parts).strip()


def _amx_box_letter(pg, box):
    for L in pg["lines"]:
        for w in L["words"]:
            if 90 <= w["l"] <= 240 and box["top"] <= w["y"] <= box["bottom"] and re.fullmatch(r"[A-Ea-e]", w["t"]):
                return w["t"].upper()
    return None


def _amx_save_crop(im, box, out_path, pad=4):
    x0, y0, x1, y1 = box
    im.crop((max(0, x0 - pad), max(0, y0 - pad), min(im.size[0], x1 + pad), min(im.size[1], y1 + pad))).save(out_path)


def _amx_land_crop(pg):
    """Zoomed landscape screen: keep the white app card, then the picture/table inside it."""
    im, W, H = pg["img"], pg["W"], pg["H"]; px = im.load()
    cols = [x for x in range(0, W, 4) if sum(px[x, y][0] for y in range(H // 4, H * 3 // 4, 40)) / len(range(H // 4, H * 3 // 4, 40)) > 200]
    if not cols:
        return None
    x0, x1 = min(cols), max(cols)
    return _amx_content_bbox(px, x0 + 10, 10, x1 - 10, H - 40, min_run=200)


def _amx_lines_to_md(lines, bold_opts=True):
    """Explanation lines -> markdown: paragraphs from vertical gaps, bullets from indentation."""
    if not lines:
        return ""
    tl = [L for L in lines if "row" not in L and not L.get("img") and _amx_is_text(L)]
    base = min(L["left"] for L in tl) if tl else 0
    gaps = sorted(L["gap"] for L in lines if L.get("gap") is not None and L["gap"] >= 0)
    norm_gap = gaps[len(gaps) // 4] if gaps else 30
    out = []; cur = None; rowbuf = []; prev_blue = False
    for L in lines + [{"end": True}]:
        last_heading, prev_blue = prev_blue, False     # was the previous line a blue heading?
        new_para = L.get("gap") is not None and L["gap"] > norm_gap * 1.7
        if "row" in L:
            if cur: out.append(cur); cur = None
            rowbuf.append(L); continue
        if rowbuf:
            out.append(_amx_rows_to_md(rowbuf)); rowbuf = []
        if L.get("end"):
            break
        if L.get("img"):
            if cur: out.append(cur); cur = None
            out.append(L["img"]); continue
        ws = L["words"]
        first = ws[0]["t"]
        is_bullet = (len(first) <= 2 and (first in AMX_BULLETS or not first.isalnum()) and len(ws) > 1
                     and ws[1]["l"] - (ws[0]["l"] + ws[0]["w"]) > 18 and L["left"] - base > 30)
        if L.get("dot") and not is_bullet:          # bullet glyph not read by OCR, but drawn left of the text
            if cur: out.append(cur)
            cur = "- " + L["text"]; continue
        if re.match(r"^\d{1,2}[.)]\s+\S", L["text"]):
            if cur: out.append(cur)
            out.append(L["text"]); cur = None; continue
        if L.get("blue") and len(L["text"]) < 70 and not L["text"].rstrip().endswith(("-", "–")):
            if cur and not cur.rstrip().endswith((".", ":", ")")) and not cur.lstrip().startswith("- "):
                cur += " " + L["text"]; continue      # blue words inside a running sentence (a link)
            if cur is None and out and last_heading and out[-1].endswith("**") and not new_para:
                out[-1] = out[-1][:-2] + " " + L["text"].rstrip() + "**"; prev_blue = True; continue   # heading wrapped
            if cur: out.append(cur)
            out.append("**" + L["text"].rstrip() + "**"); cur = None; prev_blue = True; continue
        indent = L["left"] - base
        if is_bullet:
            if cur: out.append(cur)
            level = 2 if indent > 160 else 1
            cur = ("  " * (level - 1)) + "- " + " ".join(w["t"] for w in ws[1:])
            continue
        text = L["text"]
        if cur and cur.lstrip().startswith("- ") and indent > 60 and not new_para:
            cur += " " + text; continue
        if cur is None or new_para or (cur.lstrip().startswith("- ") and indent <= 60):
            if cur: out.append(cur)
            cur = text
        else:
            cur += " " + text
    if cur:
        out.append(cur)
    md = "\n\n".join(p if not p.lstrip().startswith("- ") else p for p in out)
    md = re.sub(r"\n\n(\s*- )", r"\n\1", md)            # keep bullet lists tight
    md = re.sub(r"(^|\n)(\s*- [^\n]*)\n(?!\s*- )", r"\1\2\n\n", md)  # blank line after a list
    if bold_opts:
        md = re.sub(r"\((Options?\s+[A-E](?:\s*(?:,\s*and|,|and|&)\s*[A-E])*)\)", r"**(\1)**", md)
        md = re.sub(r"(^|\n)(NOTE(?:\s*\(\d+\))?)\s*[-–:]", r"\1**\2** –", md)
    md = re.sub(r"\n\n(\d{1,2}[.)] )", r"\n\1", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"(?m)^([A-Z][A-Z0-9 ()/,-]{3,40})$", r"**\1**", md)
    md = re.sub(r"\*\*\*\*([^*\n]+)\*\*\*\*", r"**\1**", md)
    md = _amx_fix_ocr(md)
    return md.strip()


_AMX_OCR_RULES = [
    (r"\bCHA2DS[013-9]\b", "CHA2DS2"),
    (r"\bBM\]", "BMJ"),
    (r"\b(Age|age|score|Score|INR)\s+2(\d{1,2})\b", r"\1 ≥\2"),
    (r"^\$(\d)$", r"S\1"),
    (r"^6\)$", "0"),
    (r"\bGl\b", "GI"),
    (r"\b(score|Score|of|than|is|be|was)\s+=\s?(\d)", r"\1 ≥\2"),
]


def _amx_fix_ocr(t):
    for pat, sub in _AMX_OCR_RULES:
        new = re.sub(pat, sub, t)
        if new != t:
            for m in re.finditer(pat, t):
                _amx_repair(f"OCR galti sudhaari: '{m.group(0)}' -> '{re.sub(pat, sub, m.group(0))}'")
            t = new
    return t


AMX_URL_FIXES = [("ncbi.nim.nih", "ncbi.nlm.nih"), ("www.nim.nih", "www.nlm.nih")]


def _amx_refs(lines):
    """Lines of the grey 'Reference(s)' box -> list of reference strings."""
    refs = []; cur = ""; prev_right = 0
    for L in lines:
        ws = [w for w in L["words"] if w["l"] > 330 and not re.fullmatch(r"\w{0,2}e\w?erence\(?s?\)?", w["t"], re.I)]
        if not ws:
            continue
        t = " ".join(w["t"] for w in ws)
        if len(ws[0]["t"]) <= 2 and not ws[0]["t"].isalnum():      # bullet glyph: • read as + » « etc.
            if cur.strip(): refs.append(cur.strip())
            cur = " ".join(w["t"] for w in ws[1:])
        elif ws[0]["l"] > 405 and cur.strip():                     # glyph missed: text at bullet-text indent
            refs.append(cur.strip()); cur = t
        else:
            # the box wraps words and URLs mid-word: a full-width line continues without a space
            cur += ("" if prev_right >= 1030 and (t[:1].islower() or cur.endswith(("/", "-", "."))) else " ") + t
        prev_right = L["right"]
    if cur.strip():
        refs.append(cur.strip())
    out = []
    for r in refs:
        r = re.sub(r"^\W+", "", r).replace(" – ", " – ")
        if r.startswith(("http", "www.")):
            r = r.replace(" ", "")             # the box wraps URLs mid-way
            parts = [p for p in re.split(r"(?=https?://)", r) if p]
            if len(parts) > 1:
                out.extend(p for p in parts[:-1] if p not in out); r = parts[-1]
        for a, b in AMX_URL_FIXES:
            r = r.replace(a, b)
        r = _amx_fix_ocr(r)
        if r and r not in out:
            out.append(r)
    return out


AMX_BUTTONS = re.compile(r"Hide the correct|See the correct|Hide explanations|See explanations|Finish now|Continue later", re.I)


def _amx_stitch(frames):
    """Stacks crops of consecutive zoomed screens, dropping the rows two screens share."""
    from PIL import ImageChops
    if not frames:
        return None
    out = frames[0]
    for fr in frames[1:]:
        w = min(out.size[0], fr.size[0])
        a = out.crop((0, 0, w, out.size[1])).convert("L").resize((w // 4, out.size[1] // 4))
        b = fr.crop((0, 0, w, fr.size[1])).convert("L").resize((w // 4, fr.size[1] // 4))
        best = (None, 1e9)
        for off in range(10, min(a.size[1], b.size[1]) - 2):
            d = ImageChops.difference(a.crop((0, a.size[1] - off, a.size[0], a.size[1])), b.crop((0, 0, b.size[0], off)))
            h = d.histogram(); s = sum(i * n for i, n in enumerate(h)) / (off * a.size[0])
            if s < best[1]:
                best = (off, s)
        off = best[0] * 4 if best[0] and best[1] < 6 else 0
        new = Image.new("RGB", (max(out.size[0], fr.size[0]), out.size[1] + fr.size[1] - off), "white")
        new.paste(out, (0, 0)); new.paste(fr.crop((0, off, fr.size[0], fr.size[1])), (0, out.size[1]))
        out = new
    return out


def _amx_land_frame(pg):
    """Zoomed landscape screen -> the app card below the bar, without the home-bar strip."""
    im, W, H = pg["img"], pg["W"], pg["H"]; px = im.load()
    ys = range(H // 4, H * 3 // 4, 40)
    cols = [x for x in range(0, W, 4) if sum(px[x, y][0] for y in ys) / len(ys) > 200]
    if not cols:
        return None
    x0, x1 = min(cols), max(cols)
    bb = _amx_content_bbox(px, x0 + 10, AMX_HEADER - 5, x1 - 10, H - 50, min_run=60)
    if not bb:
        return None
    return im.crop((bb[0] - 4, AMX_HEADER - 5, bb[2] + 4, H - 50))


def _amx_line_notes(lines, where):
    for L in lines:
        if L.get("bubble"):
            _amx_note(f"{where}: chat bubble ke neeche line ka hissa chhupa ho sakta hai: '{L['text'][-40:]}'")
        if L.get("edge"):
            _amx_note(f"{where}: app bar ke neeche kati hui line OCR se li: '{L['text'][:40]}'")


def _amx_pic_options(group):
    """True when most fully visible option boxes hold a picture instead of text."""
    n = pics = 0
    for pg in group["qpages"]:
        for bx in pg.get("boxes", []):
            if bx["cut"]:
                continue
            n += 1
            bb = _amx_content_bbox(pg["img"].load(), AMX_OPT_TEXT_X, bx["top"] + 4, pg["W"] - 60, bx["bottom"] - 4, min_run=40)
            if bb and bb[3] - bb[1] > (bx["bottom"] - bx["top"]) * 0.5:
                pics += 1
    return n and pics >= max(2, n // 2)


_AMX_KNOWN = None


def _amx_known_refs():
    """Complete reference titles seen anywhere in the bank (M2_AMX_REFS, a JSON list)."""
    global _AMX_KNOWN
    if _AMX_KNOWN is None:
        try:
            _AMX_KNOWN = json.load(open(os.environ.get("M2_AMX_REFS", "/root/amedex_work/known_refs.json"), encoding="utf-8"))
        except Exception:
            _AMX_KNOWN = []
    return _AMX_KNOWN


def _amx_build_question(qn, group, img_dir, stem_name):
    warnings = []
    del _AMX_NOTES[:]; del _AMX_REPAIRS[:]
    # ---- stem text
    stem = []
    for pg in group["qpages"]:
        new = _amx_stem_lines(pg)
        stem, ok = _amx_merge(stem, new)
    # after the question mark only solid text can still be stem (OCR of an ECG/picture gives junk lines)
    qm = next((i for i, L in enumerate(stem) if L["text"].rstrip().endswith("?")), None)
    if qm is not None:
        def solid(L):
            toks = re.findall(r"[A-Za-z]+", L["text"])
            return L["conf"] >= 85 and len(toks) >= 3 and sum(len(x) >= 3 for x in toks) >= 0.7 * len(toks)
        dropped = [L for L in stem[qm + 1:] if not solid(L)]
        if dropped:
            _amx_repair(f"stem ke '?' ke baad picture ka OCR kachra hataya: '{dropped[0]['text'][:30]}'")
        stem = stem[:qm + 1] + [L for L in stem[qm + 1:] if solid(L)]
    _amx_line_notes(stem, "stem")
    stem_md = " ".join(L["text"] for L in stem)
    stem_md = _amx_fix_ocr(re.sub(r"\s+", " ", stem_md).strip())
    # the whole stem read twice (two screens that did not overlap cleanly): keep one copy
    k = min(60, len(stem_md) // 3)
    j = stem_md.find(stem_md[:k], 1) if k >= 25 else -1
    if j > 0:
        second = stem_md[j:]
        if _amx_sim(second, stem_md[:len(second)]) > 0.9 or _amx_sim(stem_md[:j], second[:j]) > 0.9:
            first = stem_md[:j].strip()
            _amx_repair("stem do baar jur gaya tha — ek copy rakhi")
            stem_md = second if second.rstrip().endswith(("?", ".")) or len(second) >= len(first) else first
    # ---- stem image: a complete picture inside the stem card of a portrait screen
    best = None
    for pg in group["qpages"]:
        px = pg["img"].load(); W, H = pg["W"], pg["H"]
        sl = _amx_stem_lines(pg)
        if not sl or not sl[-1]["text"].rstrip().endswith(("?", ".", ":")):
            continue                          # no complete stem above: a scrolled options screen (picture = an option)
        y0 = sl[-1]["bottom"] + 15
        rb = pg.get("raw_boxes") or []
        y1 = (rb[0]["top"] - 10) if rb else H - 60      # never past the first option box
        if y1 - y0 < 150:
            continue
        bb = _amx_content_bbox(px, 70, y0, W - 70, y1)
        if not bb:
            continue
        complete = bb[1] > y0 + 2 and bb[3] < y1 - 2
        area = (bb[2] - bb[0]) * (bb[3] - bb[1])
        if complete and (not best or area > best[2]):
            best = (pg, bb, area)
    stem_imgs = []
    def _opts_zoom(p):
        tsv = os.path.join(os.path.dirname(os.path.dirname(p["path"])), "ocr", Path(p["path"]).stem + ".tsv")
        return sum(1 for w in _amx_words(tsv) if re.fullmatch(r"[A-E]", w["t"]) and w["l"] < p["W"] * 0.35) >= 2
    group["qland"] = [p for p in group["qland"] if not _opts_zoom(p)]
    if group["qland"] and _amx_pic_options(group):
        group["qland"] = []                   # zooms of picture options, not of a stem picture
    if group["qland"] and _amx_land_picture(group["qland"], os.path.join(img_dir, f"{stem_name}_q{qn}_stem.png")):
        stem_imgs.append(f"{stem_name}_q{qn}_stem.png"); best = None
        if _AMX_STACK_FAILED:
            _amx_note(f"stem image ke {len(group['qland'])} zoom screenshots ka jor nahi mila — image check karo")
        else:
            _amx_repair(f"stem image {len(group['qland'])} zoom screenshots jod kar HD banayi")
    if best:
        fn = f"{stem_name}_q{qn}_stem.png"
        _amx_save_crop(best[0]["img"], best[1], os.path.join(img_dir, fn))
        stem_imgs.append(fn)
    # ---- options: prefer the marked screen; take each box's text where the box is fully visible
    opt_pages = [pg for pg in group["qpages"] if pg.get("boxes")]
    letters = {}
    for pg in opt_pages:
        boxes = pg["boxes"]
        # letter = position + offset; OCR letters vote for the offset (a single misread letter cannot win)
        votes = {}
        for i, bx in enumerate(boxes):
            l = _amx_box_letter(pg, bx)
            if l:
                votes[ord(l) - 65 - i] = votes.get(ord(l) - 65 - i, 0) + 1
        if votes:
            off = max(votes.items(), key=lambda kv: (kv[1], -abs(kv[0])))[0]
        elif not any(b["cut"] for b in boxes) and len(boxes) >= 4:
            off = 0
        else:
            continue
        if any(l and ord(l) - 65 - i != off for i, l in ((i, _amx_box_letter(pg, b)) for i, b in enumerate(boxes))):
            _amx_repair("option ka letter OCR ne ghalat parha, box ki tarteeb se letter rakha")
        for i, bx in enumerate(boxes):
            if not 0 <= i + off < 8:
                continue
            lt = "ABCDEFGH"[i + off]
            d = letters.setdefault(lt, {"text": "", "mark": None, "img": None, "pic": None, "box": None, "conf": 100})
            if not bx["cut"]:
                t = _amx_box_text(pg, bx)
                bws = [w for L in pg["lines"] for w in L["words"] if w["l"] >= AMX_OPT_TEXT_X
                       and bx["top"] - 4 <= w["y"] and w["y"] + w["h"] <= bx["bottom"] + 4]
                pic = _amx_content_bbox(pg["img"].load(), AMX_OPT_TEXT_X, bx["top"] + 4, pg["W"] - 60, bx["bottom"] - 4, min_run=40)
                d["box"] = (pg, (AMX_OPT_TEXT_X + 6, bx["top"] + 6, pg["W"] - 60, bx["bottom"] - 6))
                d["conf"] = sum(w["c"] for w in bws) / len(bws) if bws else 0
                if pic and pic[3] - pic[1] > (bx["bottom"] - bx["top"]) * 0.5:
                    d["pic"] = (pg, pic)
                    if bws and sum(w["c"] for w in bws) / len(bws) < 60:
                        t = ""                 # OCR read lines of a picture (ECG strip) as text
                if len(t) > len(d["text"]):
                    d["text"] = t
                if not t and not d["img"]:
                    bb = _amx_content_bbox(pg["img"].load(), AMX_OPT_TEXT_X, bx["top"] + 4, pg["W"] - 60, bx["bottom"] - 4, min_run=40)
                    if bb:
                        fn = f"{stem_name}_q{qn}_opt{lt.lower()}.png"
                        _amx_save_crop(pg["img"], bb, os.path.join(img_dir, fn))
                        d["img"] = fn
            if bx["mark"]:
                d["mark"] = bx["mark"]
    if sum(1 for d in letters.values() if d["img"]) >= 2:
        for lt, d in letters.items():
            toks = re.findall(r"\S+", d["text"])
            junk = not toks or d.get("conf", 100) < 90 or sum(len(x) <= 2 for x in toks) >= 0.4 * len(toks)
            src = d.get("pic") or (d.get("box") if junk else None)
            if d["img"] and d["text"] and junk:
                d["text"] = ""                     # picture already cut from another screen; drop OCR junk
            if not d["img"] and src:
                fn = f"{stem_name}_q{qn}_opt{lt.lower()}.png"
                _amx_save_crop(src[0]["img"], src[1], os.path.join(img_dir, fn))
                d["img"] = fn; d["text"] = ""
                _amx_repair(f"option {lt}: OCR text ki jagah tasveer rakhi (baqi options bhi tasveer hain)")
    for d in letters.values():
        d["text"] = re.sub(r"^[_~=|]+\s*", "", d["text"])
    order = sorted(letters)
    # a period read as a comma at the end of one option, when the others end with '.'
    ends = [letters[l]["text"][-1:] for l in order if letters[l]["text"]]
    if ends.count(".") >= len(ends) - 1:
        for l in order:
            if letters[l]["text"].endswith(","):
                letters[l]["text"] = letters[l]["text"][:-1] + "."
                _amx_repair(f"option {l}: aakhri ',' ko '.' kiya")
    options = []
    for lt in order:
        d = letters[lt]
        text = d["text"] or (f"![Option {lt}](images/{d['img']})" if d["img"] else "")
        if not text:
            warnings.append(f"option {lt}: text nahi mila")
        options.append({"id": lt.lower(), "text": text, "correct": d["mark"] == "correct"})
    if len(options) < 4:
        warnings.append(f"sirf {len(options)} options mile")
    marked = [o["id"] for o in options if o["correct"]]
    head = next((pg["answer"].lower() for pg in group["epages"] if pg["kind"] == "expl"), None)
    if marked and head and marked[0] != head:
        warnings.append(f"✓ mark {marked[0].upper()} vs explanation 'Option {head.upper()}'")
    correct = marked[0] if marked else head
    source = "check_mark" if marked else ("explanation_heading" if head else None)
    if not marked and head:
        for o in options:
            o["correct"] = o["id"] == head
    if not correct:
        warnings.append("sahi jawab nahi mila")
    # ---- explanation (+ references), merged across scrolled screens
    exp = []; refs_lines = []
    tables = []                       # one entry per table: {"frames": [...]} filled from zoomed screens
    epages = group["epages"]
    i = 0; prev_pg = None; pending_table = False
    while i < len(epages):
        pg = epages[i]
        if pg["kind"] == "land":
            run = []
            while i < len(epages) and epages[i]["kind"] == "land":
                run.append(epages[i]); i += 1
            land = []
            for k, p in enumerate(run):
                tsv = os.path.join(os.path.dirname(os.path.dirname(p["path"])), "ocr", Path(p["path"]).stem + ".tsv")
                land = _amx_merge_screens(land, _amx_land_items(p, tsv, last_screen=k == len(run) - 1))
            for it in land:
                if "row" in it and it.get("part"):
                    _amx_note(f"table row '{it['row'][0][:25]}' kisi screen par poori nazar nahi aayi (kinare se kati) — check karo")
            if any("row" in L for L in land):
                _amx_repair(f"table zoom screenshots ({', '.join(Path(p['path']).name for p in run)}) se OCR karke banayi")
            exp, _ = _amx_merge(exp, _amx_drop_seen(exp, land))
            pending_table = False
            continue
        i += 1
        if pg["kind"] == "stem" and "zones_full" not in pg:
            pg["zones_full"] = _amx_table_zones(pg["img"].load(), pg["W"], pg["H"])
        zones = pg.get("zones_full") if pg["kind"] == "stem" else (pg.get("zones") or [])
        in_zone = lambda L: any(a - 5 <= (L["top"] + L["bottom"]) / 2 <= b + 5 for a, b in zones)
        items = []
        ptsv = os.path.join(os.path.dirname(os.path.dirname(pg["path"])), "ocr", Path(pg["path"]).stem + ".tsv")
        for z in zones:
            # a table narrow enough for the portrait screen (right border visible) is read right here
            rows = [it for it in _amx_table_items(pg["img"], _amx_words(ptsv), max(AMX_HEADER, z[0] - 6),
                                                  min(pg["H"] - 55, z[1] + 6)) if "row" in it]
            if rows:
                items.extend(rows)
                _amx_repair("chhoti table portrait screen se hi parhi (poori nazar aa rahi thi)")
            elif z[0] > AMX_HEADER + 5:  # a wide table starting here: its content comes from the zoomed screens
                pending_table = True
        pic_top = max(AMX_HEADER, pg.get("head_y", 0) + 5) if pg["kind"] == "expl" else AMX_HEADER
        rb = [(b["top"], b["bottom"]) for b in pg.get("raw_boxes", [])]
        refs_y = next((L["top"] for L in pg["lines"] if re.match(r"^\W*\w{0,2}e\w?erence", L["text"], re.I)), pg["H"])
        boxes_pic = [_amx_pic_item(pg["img"], b, pg["H"], pic_top, min(refs_y, pg["H"] - 60))
                     for b in _amx_pictures(pg["img"], _amx_words(ptsv), 90, pg["W"] - 140, pic_top, min(refs_y, pg["H"] - 60),
                                            skip=[(a - 4, b + 4) for a, b in zones] + rb)]
        items.extend(boxes_pic)
        in_pic = lambda L: any(p["top"] - 4 <= (L["top"] + L["bottom"]) / 2 <= p["bottom"] + 4 and L["left"] < p["left"] + (p["pic"].size[0]) for p in boxes_pic)
        ls = [L for L in pg["lines"] if not (pg["kind"] == "expl" and L["bottom"] <= pg.get("head_y", 0)) and not in_pic(L)]
        in_refs = False; prev = None; body = []
        for L in sorted(ls + items, key=lambda L: L["top"]):
            if "row" in L or "pic" in L:           # a table row / picture read from this screen
                body.append(L); prev = None; continue
            if AMX_BUTTONS.search(L["text"]):
                in_refs = False; continue
            if re.match(r"^\W*\w{0,2}e\w?erence", L["text"], re.I) or (in_refs and L["left"] > 330):
                in_refs = True; refs_lines.append(L); continue
            in_refs = False
            if in_zone(L) or not _amx_is_text(L):
                continue
            L = dict(L); L["gap"] = (L["top"] - prev["bottom"]) if prev else None
            body.append(L); prev = L
        carried = (bool(zones) and zones[0][0] <= AMX_HEADER + 5) or bool(exp and "row" in exp[-1])
        if exp and any("row" in L for L in exp[-30:]):
            body = _amx_drop_seen(exp, body)
        exp, ok = (_amx_merge_screens(exp, body), True) if any("row" in L for L in body[:1]) else _amx_merge(exp, body)
        tail_join = bool(prev_pg and body and any(_amx_sim(T["text"], body[0]["text"][:len(T["text"]) + 5]) >= 0.7
                                                   for T in prev_pg.get("tail_lines", [])))
        if not ok and body and not carried:
            if tail_join:
                pass                          # the screen above ended on this same line, cut in half
            elif prev_pg and _amx_scroll_continues(prev_pg, pg):
                _amx_repair(f"{Path(prev_pg['path']).name} -> {Path(pg['path']).name}: text overlap nahi mila, tasveer se jora (beech ka kuch text chhupa ho sakta hai)")
            else:
                warnings.append(f"explanation screens ka overlap nahi mila ({Path(pg['path']).name})")
        prev_pg = pg
    if pending_table:
        warnings.append("table ka zoomed (landscape) screenshot nahi mila")
    # pictures: a copy cut by the screen edge goes when a complete copy of the same picture is near
    def _hd(a, b):
        return sum(x != y for x, y in zip(a, b))
    def _bubble(im):
        """The blue chat-bubble button cut at the screen edge is not a picture."""
        sm = im.convert("RGB").resize((24, 24))
        px = list(sm.getdata())
        blue = sum(1 for r, g, b in px if b > 150 and r < 90 and 80 < g < 150)
        if im.size[0] < 200 and blue > 0.4 * len(px):
            return True
        # a screen-wide strip (scroll button + the bubble's left half) with no picture in it
        w, h = im.size
        if 900 <= w <= 1000 and 110 <= h <= 180:
            sm = im.convert("RGB").resize((95, 14))
            px = list(sm.getdata())
            return sum(1 for r, g, b in px if b > 150 and r < 70 and 90 < g < 150) > 0.04 * len(px)
        return False
    keep = []
    for k, L in enumerate(exp):
        if "pic" in L:
            if _bubble(L["pic"]):
                _amx_repair("chat bubble ka tukda tasveer samjha gaya tha — hataya")
                continue
            near = [M for M in exp[max(0, k - 6):k + 7] if "pic" in M and M is not L]
            if L["edge"] and any(not M["edge"] for M in near):
                continue
            if any(M in keep and _hd(M["hash"], L["hash"]) < 40 for M in near):
                continue                      # the same picture again (scroll overlap)
        keep.append(L)
    exp = keep; npic = 0
    for k, L in enumerate(exp):
        if "pic" in L:
            npic += 1
            fn = f"{stem_name}_q{qn}_exp{npic}.png"
            L["pic"].save(os.path.join(img_dir, fn))
            exp[k] = {"img": f"![](images/{fn})", "text": fn, "words": [], "left": 0, "top": L["top"], "bottom": L["bottom"], "conf": 100}
            if L["edge"]:
                _amx_note(f"explanation ki tasveer {npic} sirf kati hui mili (screen ke kinare) — check karo")
    # dedupe reference lines repeated on overlapping screens
    rl = []
    for L in refs_lines:
        if not any(_amx_sim(L["text"], R["text"]) > 0.9 for R in rl):
            rl.append(L)
    refs = _amx_refs(rl)
    # a reference cut by the chat bubble is completed from the bank's list of complete references
    known = _amx_known_refs(); cut = []
    for i, r in enumerate(refs):
        n = _amx_norm(r)
        full = {k for k in known if len(n) >= 12 and _amx_norm(k).startswith(n) and _amx_norm(k) != n}
        if len(full) == 1:
            refs[i] = full.pop(); cut.append(n)
            _amx_repair(f"reference poora kiya (bubble ke neeche kata tha): '{r}' -> '{refs[i]}'")
    _amx_line_notes([L for L in rl if not any(_amx_norm(L["text"])[-12:] in c for c in cut)], "reference")
    # the "Option X is correct" heading is the answer, not explanation text; when its screen was not
    # recognised as the explanation head (half under the app bar: "ption D Is correct") it lands here
    head_rx = re.compile(r"^\W*(?:O|0)?p?tion\s+[A-E]\s+[Ii1l]s\s+correct\W*$")
    if exp and "row" not in exp[0] and head_rx.match(exp[0]["text"].strip()):
        _amx_repair(f"explanation ke shuru se answer heading hataya: '{exp[0]['text'].strip()}'")
        _amx_note("explanation ki pehli screen 'Option X is correct' wali nahi pehchani gayi thi — jawab aur explanation ka shuru check karo")
        exp = exp[1:]
    _amx_line_notes([L for L in exp if "row" not in L], "explanation")
    explanation = _amx_lines_to_md(exp)
    # a bold bullet heading ("• Nitrates") whose bullet the OCR read as 'e' / '¢' / '«'
    fixed = re.sub(r"(?m)^\*\*[e¢«°]\s+(?=[A-Z])", "- **", explanation)
    if fixed != explanation:
        _amx_repair("bold bullet headings ka '•' OCR ne 'e' parha tha — '- **' banaya")
        explanation = fixed
    warnings += ["check: " + n for n in _AMX_NOTES]
    repairs = list(_AMX_REPAIRS)
    links = [{"source": "Reference", "title": r, "url": r if r.startswith("http") else ""} for r in refs]
    text_md = stem_md + "".join(f"\n\n![](images/{fn})" for fn in stem_imgs)
    return {
        "id": f"q{qn}", "number": qn, "text": text_md, "options": options, "attempted": True,
        "correct_answer_id": correct, "correct_source": source, "explanation": explanation,
        "notes": {"heading": "", "body_markdown": "", "images": []},
        "links": links, "warnings": warnings, "repairs": repairs,
        "source_pages": [int(Path(p["path"]).stem.split("-")[-1]) + 1 for p in group["qpages"] + group["qland"] + group["epages"]],
    }


def _amx_starts_question(pages, i):
    """After an explanation, a portrait text screen is a new stem only if the next options screen shows the same text."""
    pg = pages[i]
    if pg.get("font_size"):
        return True
    # the explanation ends with the "Reference(s)" box: a text screen after it is the next question
    if i > 0 and any(re.match(r"\W*Reference\(?s?\)?", L["text"]) for L in pages[i - 1].get("lines", [])):
        return True
    # a stem with a picture is often followed by its dimmed zoom popup ("dim"), never an explanation
    if i + 1 < len(pages) and pages[i + 1]["kind"] == "dim":
        return True
    mine = [L for L in _amx_stem_lines(pg) if len(L["text"]) > 20]
    for nx in pages[i + 1:i + 5]:
        if nx["kind"] == "opts":
            theirs = [L for L in _amx_stem_lines(nx) if len(L["text"]) > 20]
            return any(_amx_sim(a["text"], b["text"]) >= 0.88 for a in mine for b in theirs)
        if nx["kind"] == "expl":
            break
    return False


def _amx_selfcheck(q):
    """Sanity checks on a finished question: anything odd becomes a warning (the question is flagged)."""
    w = []
    texts = [o["text"] for o in q["options"] if not o["text"].startswith("![")]
    seen = {}
    for o in q["options"]:
        k = _amx_norm(o["text"])
        if k and not o["text"].startswith("![") and k in seen:
            w.append(f"options {seen[k].upper()} aur {o['id'].upper()} ka text ek jaisa hai")
        seen.setdefault(k, o["id"])
        toks = [x for x in re.findall(r"[A-Za-z]+", o["text"]) if not x.isupper()]   # CT, ECG, MRI are fine
        if len(toks) >= 2 and not o["text"].startswith("![") and \
                sum(1 for x in toks if len(x) > 2 and not re.search(r"[aeiouyAEIOUY]", x)) > len(toks) * 0.4:
            w.append(f"option {o['id'].upper()} ka text OCR kachra lagta hai")
    ends = [o["text"].rstrip()[-1:] for o in q["options"] if o["text"] and not o["text"].startswith("![")]
    if ends and ends.count(".") >= len(ends) - 1:
        for o in q["options"]:
            if o["text"] and not o["text"].startswith("![") and not o["text"].rstrip().endswith((".", ")", "?")):
                w.append(f"option {o['id'].upper()} adhoora lagta hai (baqi '.' par khatam)")
    if len(q["options"]) < 4:
        w.append(f"sirf {len(q['options'])} options")
    stem = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", q["text"]).strip()
    if not stem.endswith(("?", ".", ":")):
        w.append("stem '?' ya '.' par khatam nahi hota (adhoora ho sakta hai)")
    exp = q["explanation"].strip()
    if len(exp) < 80:
        w.append("explanation bohat chhoti / khaali")
    elif not re.search(r"[.)\]:|*>]$|<br>$", exp) and not re.search(r"\n\s*- [^\n]+$", exp):
        w.append("explanation adhoori khatam hoti lagti hai")
    return w


def _amx_answered(pg):
    """An options screen after answering shows a green tick / red cross on the option boxes."""
    return any(b.get("mark") for b in pg.get("boxes", []))


def _amx_same_stem(a, b):
    la = [L["text"] for L in _amx_stem_lines(a) if len(L["text"]) > 20]
    lb = [L["text"] for L in _amx_stem_lines(b) if len(L["text"]) > 20]
    if not la or not lb:
        return True                 # nothing to compare: do not split on a guess
    return any(_amx_sim(x, y) >= 0.88 for x in la for y in lb)


def _amx_group(pages):
    groups = []; cur = None
    for i, pg in enumerate(pages):
        k = pg["kind"]
        if k == "stem" and cur and cur["phase"] == "expl" and not _amx_starts_question(pages, i):
            k = "exptail"           # explanation scrolled to a screen that only looks like a stem
        answered = bool(cur) and cur["phase"] == "q" and any(_amx_answered(p) for p in cur["qpages"])
        if k in ("stem", "opts"):
            # a fresh, unanswered question after an answered one whose explanation screen was missed
            # (its "Option X is correct" head read as exptail, or opened only in landscape): new group
            new_q = answered and not _amx_answered(pg) and not _amx_same_stem(cur["qpages"][0], pg)
            if new_q:
                cur["split_after"] = Path(pg["path"]).name   # reported on the finished question as a check
            if cur is None or cur["phase"] == "expl" or new_q:
                cur = {"qpages": [], "qland": [], "epages": [], "phase": "q"}; groups.append(cur)
            cur["qpages"].append(pg)
        elif k == "land" and cur:
            if answered:            # zoomed screen after answering = the explanation, opened in landscape
                cur["phase"] = "expl"
            (cur["epages"] if cur["phase"] == "expl" else cur["qland"]).append(pg)
        elif k == "expl" and cur:
            cur["phase"] = "expl"; cur["epages"].append(pg)
        elif k == "exptail" and cur and (cur["phase"] == "expl" or answered):
            cur["phase"] = "expl"; cur["epages"].append(pg)
    return groups


def _amx_status(name, **kw):
    """Feeds the live dashboard (/root/live/project.json points at this file)."""
    p = os.environ.get("M2_AMX_STATUS")
    if not p:
        return
    try:
        st = json.load(open(p)) if os.path.exists(p) else {"files": []}
        files = st.setdefault("files", [])
        d = next((f for f in files if f.get("name") == name), None)
        if d is None:
            d = {"name": name}; files.append(d)
        d.update(kw)
        tmp = p + ".tmp"; json.dump(st, open(tmp, "w"), ensure_ascii=False); os.replace(tmp, p)
    except Exception:
        pass


# ---- Amedex zoomed (landscape) screens: tables as rows/cells + the text around them --------------

def _amx_np(im):
    import numpy as np
    return np.asarray(im.convert("RGB"), dtype=np.int16)


def _amx_find_strip(a, b, y_top):
    """Row in `a` where the strip of `b` starting at y_top appears (compares only columns that the
    home bar / chat bubble / up-button never cover). Returns (row, score)."""
    import numpy as np
    ga = a.mean(axis=2)[::2, ::2]; gb = b.mean(axis=2)[::2, ::2]
    W = ga.shape[1]
    cols = np.r_[int(W * .10):int(W * .28), int(W * .52):int(W * .85)]
    h = 30; t = y_top // 2
    strip = gb[t:t + h, cols]
    if strip.min() > 150:
        return None, 1e9
    best = (None, 1e9)
    for y in range(0, ga.shape[0] - h):
        d = np.abs(ga[y:y + h, cols] - strip).mean()
        if d < best[1]:
            best = (y * 2, d)
    return best


def _amx_land_stack(pages):
    """Zoomed screens of one picture/table scrolled vertically -> one tall image (RGB array, page coords lost)."""
    import numpy as np
    global _AMX_STACK_FAILED
    _AMX_STACK_FAILED = False
    out = None
    for pg in pages:
        full = _amx_np(pg["img"])
        top = AMX_HEADER - 5 if _amx_bar_kind(pg["img"].load(), pg["W"]) == "app" else 0   # image zooms have no app bar
        arr = full[top:pg["H"] - 50]
        if out is None:
            out = arr; continue
        y, sc = _amx_find_strip(out, arr, 10)
        if y is not None and sc < 14:
            out = np.concatenate([out[:y], arr[10:]], axis=0)
        else:
            out = np.concatenate([out, arr], axis=0); _AMX_STACK_FAILED = True
    return out


def _amx_bubble_left(arr):
    """x where the blue chat bubble starts (it floats over the right edge), else None."""
    import numpy as np
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    m = (b > 170) & (r < 70) & (g > 90) & (g < 150)
    cols = np.where(m.sum(axis=0) > 40)[0]
    return int(cols.min()) if len(cols) else None


def _amx_land_picture(pages, out_path):
    """HD picture from zoomed screens: stack, keep the white card, crop to the picture, cut the bubble."""
    import numpy as np
    arr = _amx_land_stack(pages)
    if arr is None:
        return False
    lum = arr.mean(axis=2)
    card = np.where(lum[:, :].mean(axis=0) > 170)[0]
    if not len(card):
        return False
    x0, x1 = int(card.min()) + 8, int(card.max()) - 8
    ink = (np.abs(arr[..., 0] - arr[..., 2]) > 25) | (lum < 185)
    ink[:, :x0] = False; ink[:, x1:] = False
    ink[lum[:, x0:x1].mean(axis=1) < 150, :] = False          # dark page behind the zoom card
    rows = np.where(ink.sum(axis=1) > 40)[0]; cols = np.where(ink.sum(axis=0) > 20)[0]
    if not len(rows) or not len(cols):
        return False
    y0, y1, xa, xb = int(rows.min()), int(rows.max()) + 1, int(cols.min()), int(cols.max()) + 1
    bl = _amx_bubble_left(arr[y0:y1, xa:xb])
    if bl is not None and bl > (xb - xa) * 0.8:
        xb = xa + bl - 4
    Image.fromarray(arr[y0:y1, xa:xb].astype("uint8")).save(out_path)
    return True


def _amx_tess(img, psm=6):
    """OCR one crop -> word dicts (crop coords)."""
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".png"); os.close(fd)
    img.save(p)
    try:
        tsv = subprocess.run(["tesseract", p, "stdout", "-l", "eng", "--psm", str(psm), "tsv"],
                             capture_output=True, text=True, errors="replace",
                             env=dict(os.environ, OMP_THREAD_LIMIT="1")).stdout
    finally:
        os.remove(p)
    ws = []
    for r in tsv.splitlines()[1:]:
        c = r.split("\t")
        # tesseract sometimes gives the first words of a line confidence 0 although they are read right
        if len(c) >= 12 and c[0] == "5" and c[11].strip() and (float(c[10]) > 10 or
                                                               (float(c[10]) == 0 and any(ch.isalnum() for ch in c[11]))):
            ws.append({"t": c[11].strip(), "l": int(c[6]), "y": int(c[7]), "w": int(c[8]), "h": int(c[9]),
                       "c": float(c[10]) if float(c[10]) > 0 else 60.0,
                       "k": (int(c[2]), int(c[3]), int(c[4]))})
    return ws


def _amx_words_text(ws, lum=None):
    """Words of one table cell -> text; bullet lines become '• ' items joined by <br>."""
    ws = [w for w in ws if (any(ch.isalnum() for ch in w["t"]) and (w["c"] >= 50 or len(w["t"]) > 3))
          or w["c"] >= 60 or w["t"] in ("•", "*", "**", "***")]       # short low-confidence bits are icon/bubble noise
    if not ws:
        return ""
    ws = sorted(ws, key=lambda w: (w["y"] + w["h"] / 2, w["l"]))
    lines = []
    for w in ws:
        yc = w["y"] + w["h"] / 2
        same = lines and (("k" in w and w["k"] == lines[-1].get("k")) or
                          ("k" not in w and abs(yc - lines[-1]["yc"]) < max(12, w["h"] * 0.6)))
        if same:
            lines[-1]["ws"].append(w)
        else:
            lines.append({"yc": yc, "ws": [w], "k": w.get("k")})
    lines.sort(key=lambda L: L["yc"])
    base = min(min(x["l"] for x in L["ws"]) for L in lines)
    items = []; cur = None; cur_left = -999; prev_bot = None
    for L in lines:
        L["ws"].sort(key=lambda w: w["l"])
        lh = max(w["h"] for w in L["ws"]); ltop = min(w["y"] for w in L["ws"])
        para = prev_bot is not None and ltop - prev_bot > 1.2 * lh
        prev_bot = max(w["y"] + w["h"] for w in L["ws"])
        if para and cur:
            items.append(cur); cur = None
        f = L["ws"][0]
        glyph = len(L["ws"]) > 1 and ((len(f["t"]) <= 2 and not f["t"].isalnum() and "*" not in f["t"]) or f["t"] in ("e", "o")) \
            and L["ws"][1]["l"] - (f["l"] + f["w"]) > 12
        dot = False
        if not glyph and lum is not None:
            yc = int(L["yc"]); xa, xb = max(0, f["l"] - 60), max(0, f["l"] - 10)
            reg = lum[max(0, yc - 9):yc + 9, xa:xb] < 100
            n = int(reg.sum())
            full_cols = int((reg.mean(axis=0) > 0.9).sum()) if reg.size else 0   # a grid line, not a dot
            dot = 25 <= n <= 400 and full_cols == 0 and int(reg.any(axis=1).sum()) < 17
        bullet = glyph or dot
        words = [w["t"] for w in (L["ws"][1:] if glyph else L["ws"])]
        first_l = (L["ws"][1] if glyph else f)["l"]
        if bullet:
            if cur: items.append(cur)
            cur = "• " + " ".join(words); cur_left = first_l
        elif words and words[0].startswith("*"):
            if cur: items.append(cur)
            cur = " ".join(words)                      # footnote line (*, **, ***)
        elif cur is not None and cur.startswith("• ") and (f["l"] > base + 15 or abs(f["l"] - cur_left) < 20):
            cur += " " + " ".join(words)
        elif cur is not None and cur.startswith("• "):
            items.append(cur); cur = " ".join(words)
        else:
            cur = (cur + " " if cur else "") + " ".join(words)
    if cur:
        items.append(cur)
    if len(items) > 1:                                  # a bullet line with no real text is OCR noise
        items = [x for x in items if sum(ch.isalnum() for ch in x) >= 2]
    t = re.sub(r"\s+", " ", "<br>".join(items)).replace("|", "/").strip()
    if "• " in t:
        t = re.sub(r"(^|<br>)\* (?=[A-Za-z])", r"\1• ", t)      # bullet read as '*'
    return _amx_fix_ocr(t)


def _amx_band_words(img, box):
    """OCR one dark band (white text) once: invert, then words in page coords."""
    from PIL import ImageOps
    x0, y0, x1, y1 = box
    crop = _amx_clean_cell(img.crop((x0, y0, x1, y1)), invert=True)
    return [dict(w, l=w["l"] - 16 + x0, y=w["y"] - 16 + y0) for w in _amx_tess(crop, 6)]


def _amx_clean_cell(im, invert):
    """Cell/band crop -> black text on white: grey or coloured fills and grid lines confuse tesseract."""
    from PIL import ImageOps, ImageDraw
    import numpy as np
    a = np.asarray(im.convert("L")).astype(int)
    if invert:                          # white text on a coloured fill: text is brighter than the fill
        bg = float(np.median(a))
        b = np.where(a > (bg + 255) / 2, 0, 255).astype("uint8")
    else:
        b = np.where(a > 140, 255, 0).astype("uint8")
    H, W = b.shape
    b[:5, :] = 255; b[-6:, :] = 255; b[:, :5] = 255; b[:, -6:] = 255
    for x in np.where((b == 0).mean(axis=0) > 0.8)[0]:
        b[:, max(0, x - 3):x + 4] = 255
    for y in np.where((b == 0).mean(axis=1) > 0.8)[0]:
        b[max(0, y - 3):y + 4, :] = 255
    return ImageOps.expand(Image.fromarray(b), border=16, fill=255)


def _amx_fix_short_cells(img, rows):
    """Cells of 1-3 characters (letters, scores) OCR badly inside a big screen: stack them and OCR once."""
    todo = [(r, i) for r in rows if not r["filled"] for i, c in enumerate(r["cells"]) if len(c) <= 3 and len(r["cells"]) > 1]
    if not todo:
        return
    crops = []
    for r, i in todo:
        x0, y0, x1, y1 = r["boxes"][i]
        c = _amx_clean_cell(img.crop((x0, y0, x1, y1)), invert=False)
        crops.append(c.resize((c.width * 2, c.height * 2)))
    W = max(c.width for c in crops) + 40; H = sum(c.height + 40 for c in crops) + 40
    canvas = Image.new("L", (W, H), 255); ys = []; y = 20
    for c in crops:
        canvas.paste(c, (20, y)); ys.append((y, y + c.height)); y += c.height + 40
    ws = _amx_tess(canvas, 6)
    for (r, i), (a, b) in zip(todo, ys):
        t = " ".join(w["t"] for w in sorted([w for w in ws if a <= w["y"] + w["h"] / 2 <= b], key=lambda w: w["l"]))
        t = t.replace("|", "/").strip()
        if t and any(ch.isalnum() for ch in t):
            if re.match(r"^>\s*(\d)$", t):            # tesseract reads ≥ as >
                _amx_repair(f"table cell '{t}' ko '≥' maana")
                t = re.sub(r"^>\s*(\d)$", r"≥\1", t)
            if t != r["cells"][i]:
                _amx_repair(f"table ka chhota cell dobara OCR: '{r['cells'][i]}' -> '{t}'")
            r["cells"][i] = _amx_fix_ocr(t)


def _amx_land_items(pg, ocr_tsv, last_screen=False):
    """One zoomed screen -> items top to bottom: complete table rows and text lines outside tables."""
    return _amx_table_items(pg["img"], _amx_words(ocr_tsv), AMX_HEADER, pg["H"] - 50, last_screen)


def _amx_run_items(run, last_screen=True):
    """Zoomed screens of one table scrolled vertically: stack them into one image (rows split between
    two screens become whole), OCR it once, then read rows. None when the screens cannot be joined."""
    import numpy as np
    frames = [_amx_np(p["img"])[AMX_HEADER - 5:p["H"] - 50] for p in run]
    out = frames[0]
    for fr in frames[1:]:
        y, sc = _amx_find_strip_right(out, fr, 10)
        if y is None or sc >= 14:
            return None
        out = np.concatenate([out[:y], fr[10:]], axis=0)
    img = Image.fromarray(out.astype("uint8"))
    words = _amx_tess(img, 4)
    return _amx_table_items(img, words, 0, img.size[1], last_screen)


def _amx_pictures(img, words, x0, x1, y0, y1, skip=(), min_h=110):
    """Pictures (ECG strips, diagrams, photos) between text: blocks of rows with ink or colour that no
    confident OCR line covers. Returns [(top, bottom, left, right)] in image coordinates."""
    import numpy as np
    arr = _amx_np(img)[y0:y1, x0:x1]
    if arr.size == 0:
        return []
    lum = arr.mean(axis=2); sat = arr.max(axis=2) - arr.min(axis=2)
    ink = (lum < 200) | (sat > 40)
    H = arr.shape[0]
    covered = np.zeros(H, bool)
    for w in words:                                   # rows of real text
        if w["c"] >= 75 and re.search(r"[A-Za-z]{2,}", w["t"]):
            a, b = w["y"] - y0 - 4, w["y"] + w["h"] - y0 + 4
            covered[max(0, a):max(0, b)] = True
    for a, b in skip:                                  # tables, boxes handled elsewhere
        covered[max(0, a - y0):max(0, b - y0)] = True
    rows = (ink.sum(axis=1) > 25) & ~covered
    pics = []; s = None; gap = 0
    for y in range(H + 1):
        on = y < H and rows[y]
        if on:
            s = y if s is None else s; gap = 0
        elif s is not None:
            gap += 1
            if gap > 14 or y == H:
                e = y - gap + 1
                if e - s >= min_h:
                    cols = np.where(ink[s:e].sum(axis=0) > 3)[0]
                    if len(cols) > 60:
                        pics.append((s + y0, e + y0, int(cols.min()) + x0, int(cols.max()) + 1 + x0))
                s = None; gap = 0
    return pics


def _amx_pic_item(img, box, H, top_limit, bot_limit):
    """A picture as an explanation item; `edge` when the screen cuts it."""
    t0, t1, l0, l1 = box
    crop = img.crop((max(0, l0 - 6), max(0, t0 - 6), l1 + 6, t1 + 6))
    small = crop.convert("L").resize((16, 16))
    px = list(small.getdata()); avg = sum(px) / len(px)
    h = "".join("1" if v > avg else "0" for v in px)
    return {"pic": crop, "hash": h, "text": "__PIC__" + h[:64], "words": [], "left": l0, "top": t0, "bottom": t1,
            "conf": 100, "edge": t0 <= top_limit + 4 or t1 >= bot_limit - 4}


def _amx_find_strip_right(a, b, y_top):
    """Like _amx_find_strip but on the right-middle columns only (a table's left label column can
    stay pinned while the rest scrolls)."""
    import numpy as np
    ga = a.mean(axis=2)[::2, ::2]; gb = b.mean(axis=2)[::2, ::2]
    W = ga.shape[1]
    cols = np.r_[int(W * .45):int(W * .85)]
    h = 30; t = y_top // 2
    strip = gb[t:t + h, cols]
    if strip.min() > 150:
        return None, 1e9
    best = (None, 1e9)
    for y in range(0, ga.shape[0] - h):
        d = np.abs(ga[y:y + h, cols] - strip).mean()
        if d < best[1]:
            best = (y * 2, d)
    return best


def _amx_table_items(img, page_words, top, bot, last_screen=False):
    import numpy as np
    pg = {"img": img}
    arr = _amx_np(img); H, W = arr.shape[:2]
    lum = arr.mean(axis=2)
    dark = lum < 125
    # card (white app page) columns
    card = np.where(lum[top:bot].mean(axis=0) > 170)[0]
    cx0, cx1 = (int(card.min()), int(card.max())) if len(card) else (0, W)
    bl = _amx_bubble_left(arr[top:bot])
    if bl is not None and bl > cx0 + (cx1 - cx0) * 0.8:
        cx1 = min(cx1, bl - 6)
    span = dark[:, cx0:cx1]
    # boundary rows: a dark run over >= 45% of the card width (thin lines or filled bands)
    frac = span.mean(axis=1)
    med = np.median(lum[:, cx0:cx1], axis=1)          # a band row stays dark at the median even under dense white text
    cand = [y for y in range(top, bot) if frac[y] > 0.45 or med[y] < 140]
    groups = []
    for y in cand:
        if groups and y - groups[-1][1] <= 2:
            groups[-1][1] = y
        else:
            groups.append([y, y])
    # rows of dense white text split a dark band into pieces: join pieces of the same background colour
    merged = []
    for g in groups:
        if merged and g[0] - merged[-1][1] <= 30:
            ins = np.where((lum[merged[-1][0]:merged[-1][1] + 1, cx0:cx1] < 125).mean(axis=0) > 0.5)[0]
            xa_, xb_ = (cx0 + int(ins.min()) + 4, cx0 + int(ins.max()) - 4) if len(ins) > 20 else (cx0 + 4, cx1 - 4)
            c1 = np.median(arr[merged[-1][0]:merged[-1][1] + 1, xa_:xb_].reshape(-1, 3), axis=0)
            cg = np.median(arr[merged[-1][1] + 1:g[0], xa_:xb_].reshape(-1, 3), axis=0) if g[0] > merged[-1][1] + 1 else c1
            if np.abs(c1 - cg).sum() < 40:
                merged[-1][1] = g[1]; continue
        merged.append(list(g))
    # grow each filled band over its own rows of dense white text (they have too little dark to count)
    for g in merged:
        if g[1] - g[0] <= 25:
            continue
        ins = np.where((lum[g[0]:g[1] + 1, cx0:cx1] < 125).mean(axis=0) > 0.5)[0]
        if len(ins) <= 20:
            continue
        xa_, xb_ = cx0 + int(ins.min()) + 4, cx0 + int(ins.max()) - 4
        c = np.median(arr[g[0]:g[1] + 1, xa_:xb_].reshape(-1, 3), axis=0)
        same = lambda y: np.abs(np.median(arr[y, xa_:xb_], axis=0) - c).sum() < 40
        while g[0] - 1 > top and same(g[0] - 1):
            g[0] -= 1
        while g[1] + 1 < bot and same(g[1] + 1):
            g[1] += 1
    groups = merged
    items = []
    rows = []
    bands = []
    for i, (g0, g1) in enumerate(groups):
        if g1 - g0 > 25:
            # one filled band per colour (a green title row sitting on a blue header row)
            inside = np.where((lum[g0:g1 + 1, cx0:cx1] < 125).mean(axis=0) > 0.5)[0]   # the band's own width
            bx0_, bx1_ = (cx0 + int(inside.min()) + 4, cx0 + int(inside.max()) - 4) if len(inside) > 20 else (cx0 + 4, cx1 - 4)
            cols = np.median(arr[g0:g1 + 1, bx0_:bx1_], axis=1)              # background colour of each row
            # a colour change that holds for 10+ rows (a new row), not the strokes of a text line
            cuts = [g0] + [g0 + j for j in range(1, len(cols) - 10)
                           if np.abs(cols[j] - cols[j - 1]).sum() > 60
                           and all(np.abs(cols[j + k] - cols[j - 1]).sum() > 60 for k in range(0, 10, 3))] + [g1 + 1]
            cuts = [c for k, c in enumerate(cuts) if k == 0 or c - cuts[k - 1] > 20 or c == g1 + 1]
            # a real row of its own colour is tall; a sliver (border line, text strokes) joins its neighbour
            cuts = [c for k, c in enumerate(cuts) if k in (0, len(cuts) - 1)
                    or (c - cuts[k - 1] >= 40 and cuts[k + 1] - c >= 40)]
            for a_, b_ in zip(cuts, cuts[1:]):
                if b_ - a_ > 25:
                    bands.append((a_, b_, (a_ + b_) // 2))
        if i + 1 < len(groups):
            bands.append((g1 + 1, groups[i + 1][0], g1))           # space between two boundaries
    if groups:
        bands.append((groups[-1][1] + 1, bot, groups[-1][1]))      # a row the screen ends inside
        if groups[0][0] - top > 40:
            bands.insert(0, (top, groups[0][0], groups[0][0]))      # a row the screen starts inside
    for y0, y1, yl in bands:
        if y1 - y0 < 25:
            continue
        seg = dark[y0:y1, cx0:cx1]
        filled = seg.mean() > 0.5
        if filled and y1 >= bot - 3:
            continue                      # dark band cut by the screen bottom: the next screen shows it whole
        if filled and y0 <= top + 3 and y1 - y0 < 70:
            bw = [w for w in page_words if y0 <= w["y"] + w["h"] / 2 <= y1]
            if not bw or min(w["y"] for w in bw) < y0 + 4:
                continue                  # its text is cut by the app bar
        border = np.where(dark[yl, cx0:cx1])[0]
        tx0 = int(border.min()) + cx0 if len(border) else cx0
        tx1 = int(border.max()) + cx0 if len(border) else cx1
        if filled:
            xs = np.where((lum[y0:y1, cx0:cx1] > 170).mean(axis=0) > 0.85)[0]
        else:
            xs = np.where(seg.mean(axis=0) > 0.85)[0]
        seps = []
        for x in xs:
            x = int(x) + cx0
            if seps and x - seps[-1][1] <= 3:
                seps[-1][1] = x
            else:
                seps.append([x, x])
        seps = [(s0 + s1) // 2 for s0, s1 in seps]
        if filled:
            seps = sorted(set([tx0] + [x for x in seps if tx0 + 10 < x < tx1 - 10] + [tx1]))
        elif not (len(seps) >= 2 and abs(seps[0] - tx0) < 12 and abs(seps[-1] - tx1) < 12):
            continue                       # no side borders: text between tables, not a row
        ws = _amx_band_words(pg["img"], (tx0 + 2, y0, tx1 - 2, y1)) if filled else page_words
        cells = []
        for i in range(len(seps) - 1):
            cw = [w for w in ws if seps[i] + 2 <= w["l"] + w["w"] / 2 < seps[i + 1] - 2
                  and y0 <= w["y"] + w["h"] / 2 < y1 and (filled or w["c"] > 20)]
            ct = _amx_words_text(cw, None if filled else lum)
            cells.append(re.sub(r"^(.+?)(?: \1)+$", r"\1", ct))     # a pinned label seen twice after stacking
        part = "bottom" if y1 == bot else ("top" if y0 == top else None)
        if not any(cells):
            continue
        if filled and len(cells) > 1 and cells[0] and all(c[:1].islower() for c in cells[1:] if c) \
                and not cells[0].rstrip().endswith((".", ":")):
            cells = [" ".join(c for c in cells if c)]          # one sentence across a faint grid line (a title)
            seps = [seps[0], seps[-1]]; is_title = True
        else:
            is_title = False
        bg = arr[y0:y1, tx0 + 3:tx1 - 3].reshape(-1, 3).mean(axis=0)
        kind = "dark" if filled else ("head" if bg[2] > 200 and bg[0] < 170 else "body")
        rows.append({"y0": y0, "y1": y1, "cells": cells, "kind": kind, "x0": tx0, "x1": tx1, "part": part,
                     "boxes": [(seps[i], y0, seps[i + 1], y1) for i in range(len(seps) - 1)], "filled": filled,
                     "ws": ws if filled else None, "title": is_title})
    for k, r in enumerate(rows):
        if r["filled"] and len(r["cells"]) == 1 and not r.get("title"):
            nxt = next((q for q in rows[k + 1:k + 3] if len(q["cells"]) >= 2 and not q["filled"] and abs(q["x0"] - r["x0"]) < 12
                        and abs(q["x1"] - r["x1"]) < 12 and q["y0"] - r["y1"] < 40), None)
            if nxt:
                seps = [b[0] for b in nxt["boxes"]] + [nxt["boxes"][-1][2]]
                ws = r.get("ws") or []
                if ws and not any(w["l"] < s < w["l"] + w["w"] for w in ws for s in seps[1:-1]):
                    r["cells"] = [_amx_words_text([w for w in ws if seps[i] <= w["l"] + w["w"] / 2 < seps[i + 1]])
                                  for i in range(len(seps) - 1)]
                    r["boxes"] = nxt["boxes"]
    _amx_fix_short_cells(pg["img"], rows)
    # text lines of the screen that are not inside a table (a row cut by the screen edge still has its
    # left border at that height, so its text is dropped here and taken from the screen that shows it whole)
    xs_border = sorted(set(r["x0"] for r in rows))
    def in_table(L):
        yc = int((L["top"] + L["bottom"]) / 2)
        return any(dark[max(0, yc - 20):yc + 20, max(0, x - 2):x + 3].any(axis=1).mean() > 0.9 for x in xs_border) \
            and any(r["x0"] - 5 <= L["left"] <= r["x1"] for r in rows)
    page_lines = _amx_lines([w for w in page_words if w["l"] + w["w"] < cx1 - 4])
    lines = [L for L in page_lines if L["top"] >= top and L["bottom"] <= bot
             and cx0 <= L["left"] <= cx1 and _amx_is_text(L) and not in_table(L)
             and not any(r["y0"] - 8 <= (L["top"] + L["bottom"]) / 2 <= r["y1"] + 8 for r in rows)
             # white text inside a dark band (footnote/title) is read with the band, never as page text
             and not any(g1 - g0 > 25 and g0 - 6 <= (L["top"] + L["bottom"]) / 2 <= g1 + 6 for g0, g1 in groups)]
    # pictures on the zoomed screen (diagrams, annotated ECGs) outside the table rows
    pics = [_amx_pic_item(img, b, H, top, bot) for b in
            _amx_pictures(img, page_words, cx0 + 5, cx1 - 5, top, bot,
                          skip=[(r["y0"], r["y1"]) for r in rows] + [(g0, g1) for g0, g1 in groups if g1 - g0 > 25])]
    lines = [L for L in lines if not any(p["top"] - 4 <= (L["top"] + L["bottom"]) / 2 <= p["bottom"] + 4 for p in pics)]
    items.extend(pics)
    for r in rows:
        items.append({"row": r["cells"], "rkind": r["kind"], "part": r.get("part"), "text": " | ".join(r["cells"]), "words": [],
                      "left": r["x0"], "top": r["y0"], "bottom": r["y1"], "conf": 100})
    prev = None
    for L in lines:
        items.append(dict(L))
    items.sort(key=lambda L: L["top"])
    prev = None
    for it in items:
        if "row" not in it:
            it["gap"] = (it["top"] - prev["bottom"]) if prev else None
        prev = it
    return items


def _amx_cell_union(a, b):
    """Two readings of one cell (row split between screens): keep every item once, the fuller reading of each."""
    A = [x for x in a.split("<br>") if x.strip()]; B = [x for x in b.split("<br>") if x.strip()]
    out = list(A)
    for it in B:
        n = _amx_norm(it)
        hit = next((i for i, x in enumerate(out) if _amx_norm(x) == n or _amx_sim(x, it) >= 0.93 or
                    (len(_amx_norm(x)) >= 4 and len(n) >= 4 and (n.startswith(_amx_norm(x)) or _amx_norm(x).startswith(n)))), None)
        if hit is None:
            out.append(it)
        elif len(it) > len(out[hit]):
            out[hit] = it
    return "<br>".join(out)


def _amx_merge_screens(acc, new):
    """Joins the items of the next zoomed screen: a row cut at the bottom of one screen and continued at the
    top of the next becomes one row; rows both screens show completely are kept once."""
    acc = list(acc); new = list(new)
    while acc and new and "row" in acc[-1] and acc[-1].get("part") == "bottom" and "row" in new[0] \
            and len(new[0]["row"]) == len(acc[-1]["row"]) and \
            (new[0].get("part") == "top" or _amx_sim(acc[-1]["row"][0], new[0]["row"][0]) >= 0.8
             or _amx_norm(new[0]["row"][0]).startswith(_amx_norm(acc[-1]["row"][0])[:25])):
        a, b = acc[-1], new.pop(0)
        cells = [_amx_cell_union(x, y) for x, y in zip(a["row"], b["row"])]
        acc[-1] = dict(a, row=cells, text=" | ".join(cells), part=b.get("part") if b.get("part") != "top" else None)
    acc, _ = _amx_merge(acc, new)
    # a complete row seen again partially at the next screen's top: drop the partial copy
    out = []
    for it in acc:
        if "row" in it and it.get("part") == "top" and out and "row" in out[-1] and \
                _amx_sim(out[-1]["row"][0], it["row"][0]) >= 0.8:
            cells = [_amx_cell_union(x, y) for x, y in zip(out[-1]["row"], it["row"])] if len(it["row"]) == len(out[-1]["row"]) else out[-1]["row"]
            out[-1] = dict(out[-1], row=cells, text=" | ".join(cells)); continue
        out.append(it)
    return out


def _amx_rows_to_md(rows):
    """Consecutive table rows -> markdown. Single-cell rows become a bold caption or a note line."""
    # footnote markers (*, **, ***) must not turn into bold/italic in the viewer: use the look-alike ∗
    rows = [dict(r, row=[c.replace("*", "∗") for c in r["row"]]) for r in rows]
    out = []; table = []

    def flush():
        if not table:
            return
        n = max(len(r["row"]) for r in table)
        body = [r for r in table]
        hdr = body[0]["row"] if body[0]["rkind"] in ("head", "dark") else [""] * n
        if body[0]["rkind"] in ("head", "dark"):
            body = body[1:]
        pad = lambda r: [c.replace("|", "/") for c in r] + [""] * (n - len(r))
        out.append("| " + " | ".join(pad(hdr)) + " |")
        out.append("|" + "---|" * n)
        for r in body:
            out.append("| " + " | ".join(pad(r["row"])) + " |")
        table.clear()

    for r in rows:
        if len(r["row"]) == 1:
            txt = r["row"][0]
            if not table and r["rkind"] == "dark":
                out.append(f"**{txt}**"); out.append("")
            else:
                flush(); out.append(""); out.extend(t for t in txt.split("<br>")); out.append("")
        else:
            table.append(r)
    flush()
    return "\n".join(out).strip()


_AMX_JOB_START = {}


def _amx_prog(job, label, done, total, unit, detail="", state="run"):
    """Progress bar on the live dashboard (M2_AMX_JOBS = its jobs folder)."""
    d = os.environ.get("M2_AMX_JOBS")
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        now = int(time.time() * 1000)
        start = _AMX_JOB_START.setdefault(job, now)
        tmp = os.path.join(d, job + ".json.tmp")
        json.dump({"label": label, "done": done, "total": total, "unit": unit, "detail": detail,
                   "state": state, "ts": now, "start": start}, open(tmp, "w"))
        os.replace(tmp, os.path.join(d, job + ".json"))
    except Exception:
        pass


def _amx_apply_manual(qs, out_dir, stem_name):
    """Hand-checked corrections (<stem>_manual_fixes.json), re-applied on every run:
    {"12": [{"field": "explanation" | "text" | "option:b" | "link:0", "old": "...", "new": "...",
             "note": "...", "clears": ["part of a warning this settles", ...]}]}
    A fix whose `old` text is no longer found flags the question instead of being skipped silently."""
    p = os.path.join(out_dir, f"{stem_name}_manual_fixes.json")
    try:
        fixes = json.load(open(p, encoding="utf-8"))
    except Exception:
        return
    byn = {q["number"]: q for q in qs}
    for n, lst in fixes.items():
        q = byn.get(int(n))
        if not q:
            continue
        for fx in lst:
            if "field" not in fx:                     # checked, nothing to change: only settle the warnings
                for c in fx.get("clears", []):
                    for w in [w for w in q["warnings"] if c in w]:
                        q["warnings"].remove(w); q.setdefault("resolved_warnings", []).append(w)
                q["verified"] = fx.get("note", "hand-checked against the PDF")
                continue
            f = fx["field"]; old, new = fx.get("old", ""), fx.get("new", "")
            if f.startswith("option:"):
                tgt = next((o for o in q["options"] if o["id"] == f.split(":")[1]), None); key = "text"
            elif f.startswith("link:"):
                i = int(f.split(":")[1]); tgt = q["links"][i] if i < len(q["links"]) else None; key = "title"
            else:
                tgt, key = q, f
            if tgt is None or (old and old not in tgt.get(key, "")):
                q["warnings"].append(f"manual fix lag nahi saka ({f}): '{old[:40]}'")
                continue
            if f.startswith("link:") and new == "__DELETE__":
                q["links"].remove(tgt)                 # a reference split in two by the OCR: drop the tail
                q.setdefault("manual_fixes", []).append({"field": f, "old": old, "new": "(removed)", "note": fx.get("note", "")})
                continue
            tgt[key] = tgt[key].replace(old, new, 1) if old else new
            if key == "title" and tgt.get("url"):
                tgt["url"] = tgt["title"] if tgt["title"].startswith("http") else tgt["url"]
            q.setdefault("manual_fixes", []).append({"field": f, "old": old, "new": new, "note": fx.get("note", "")})
            for c in fx.get("clears", []):
                hit = [w for w in q["warnings"] if c in w]
                for w in hit:
                    q["warnings"].remove(w)
                    q.setdefault("resolved_warnings", []).append(w)
                if hit:
                    q["verified"] = fx.get("note", "hand-checked against the PDF")


def _amx_apply_resolved(qs, out_dir, stem_name):
    """Questions checked by hand and found right (<stem>_qa_resolved.json) stop being flagged — as long as
    the parser still reports the same warnings for them; new warnings flag the question again."""
    p = os.path.join(out_dir, f"{stem_name}_qa_resolved.json")
    try:
        res = json.load(open(p, encoding="utf-8"))
    except Exception:
        return
    for q in qs:
        r = res.get(str(q["number"]))
        if r and q["warnings"] and sorted(r.get("warnings", [])) == sorted(q["warnings"]):
            q["resolved_warnings"] = q["warnings"]; q["warnings"] = []
            q["verified"] = r.get("note", "checked against the PDF")


def run_amedex_mode(pdf_path, out_dir, limit=None):
    # a screenshot damaged in the PDF ("image file is truncated") must not stop the whole run:
    # PIL then shows what it could decode and the question is flagged by the usual checks
    from PIL import ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    stem_name = Path(pdf_path).stem
    work = _amx_work_dir(pdf_path)
    print(f"{C.CYAN}[amedex] {pdf_path} -> {out_dir}  (work: {work}){C.RESET}")
    pages = _amx_extract_pages(pdf_path, work)
    print(f"[amedex] {len(pages)} screenshots")
    _amx_status(stem_name, status="ocr", pages=len(pages), parsed=0, qaLeft=0, flagged="-", qmap="")
    _amx_ocr_pages(pages, work)
    _amx_prog("amx-ocr", "Amedex OCR", len(pages), len(pages), "pages", f"{stem_name}: cache se", "ok")
    # plain OCR text of every screenshot, in page order
    with open(os.path.join(out_dir, f"{stem_name}_ocr.txt"), "w", encoding="utf-8") as f:
        for p in pages:
            t = os.path.join(work, "ocr", Path(p).stem + ".txt")
            f.write(f"===== Page {int(Path(p).stem.split('-')[-1]) + 1} =====\n")
            f.write((open(t, encoding="utf-8", errors="replace").read() if os.path.exists(t) else "") .rstrip() + "\n\n")
    analysed = []
    for i, p in enumerate(pages):
        analysed.append(_amx_analyse(p, os.path.join(work, "ocr", Path(p).stem + ".tsv")))
        print(f"page {i + 1}/{len(pages)}: {analysed[-1]['kind']}", flush=True)
        _amx_prog("amx-pages", "Amedex screens samajh raha", i + 1, len(pages), "screens", f"{Path(p).name}: {analysed[-1]['kind']}")
        if limit and i % 4 == 3 and len(_amx_group(analysed)) > limit + 1:
            break
    _amx_prog("amx-pages", "Amedex screens samajh raha", len(analysed), len(analysed), "screens", stem_name, "ok")
    groups = _amx_group(analysed)
    if limit:
        groups = groups[:limit]
    img_dir = os.path.join(out_dir, "images"); os.makedirs(img_dir, exist_ok=True)
    qs = []
    for n, g in enumerate(groups, 1):
        q = _amx_build_question(n, g, img_dir, stem_name)
        q["warnings"] += _amx_selfcheck(q)
        if g.get("split_after"):
            q["warnings"].append(f"check: is sawal ki explanation screen 'expl' nahi pehchani gayi thi — agla sawal "
                                 f"{g['split_after']} se alag kiya; explanation aur jawab check karo")
        qs.append(q)
        flag = f"  {C.YELLOW}! {'; '.join(q['warnings'])}{C.RESET}" if q["warnings"] else ""
        print(f"questions {n}/{len(groups)}  Q{n}: answer {str(q['correct_answer_id']).upper()} "
              f"pages {q['source_pages'][0]}-{q['source_pages'][-1]}{flag}", flush=True)
        _amx_prog("amx-q", "Amedex questions bana raha", n, len(groups), "Qs",
                  f"Q{n}: jawab {str(q['correct_answer_id']).upper()}" + (" ⚠ " + "; ".join(q["warnings"]) if q["warnings"] else ""))
        nf = sum(1 for x in qs if x["warnings"])
        _amx_status(stem_name, status="parsing", parsed=n, total=len(groups), qaTotal=len(groups), qaLeft=nf,
                    flagged=", ".join(f"Q{x['number']}" for x in qs if x["warnings"]) or "-",
                    qmap="".join("f" if x["warnings"] else "g" for x in qs))
    _amx_apply_manual(qs, out_dir, stem_name)
    _amx_apply_resolved(qs, out_dir, stem_name)
    ak = {"source_pdf": stem_name, "parser_version": "amedex-1", "total_questions": len(qs), "questions": qs}
    sv = {"source_pdf": stem_name, "parser_version": "amedex-1", "total_questions": len(qs), "questions": [
        {**{k: v for k, v in q.items() if k not in ("correct_answer_id", "correct_source", "explanation", "links")},
         "options": [{"id": o["id"], "text": o["text"]} for o in q["options"]],
         "attempted": False, "student_answer": None, "correct_answer_id": None, "explanation": None}
        for q in qs]}
    ak_path = os.path.join(out_dir, f"{stem_name}_answer_key.json")
    _atomic_write_json_global(ak_path, ak)
    _atomic_write_json_global(os.path.join(out_dir, f"{stem_name}_student_view.json"), sv)
    write_bundled_viewer(out_dir, ak_path)
    nflag = sum(1 for q in qs if q["warnings"])
    nqa = sum(1 for q in qs if q["warnings"] or q.get("resolved_warnings"))
    _amx_prog("amx-q", "Amedex questions bana raha", len(qs), len(qs), "Qs", f"{len(qs)} likhe, {nflag} flagged", "ok" if not nflag else "fail")
    _amx_status(stem_name, status="qa" if nflag else "clean", parsed=len(qs), total=len(qs), qaTotal=nqa, qaLeft=nflag,
                flagged=", ".join(f"Q{x['number']}" for x in qs if x["warnings"]) or "-",
                qmap="".join("f" if x["warnings"] else "g" for x in qs))
    print(f"{C.GREEN}[amedex] {len(qs)} questions likhe, {nflag} flagged -> {ak_path}{C.RESET}")
    return ak_path


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  Question PDF:    python3 pdf_qbank_parser.py <input.pdf> [output_dir] [--limit=N]")
        print("  Notes image(s):  python3 pdf_qbank_parser.py <notes1.jpg> [notes2.jpg ...] [output_dir]")
        print("  --limit=N: sirf pehle N questions process karega (testing ke liye fast)")
        print("  --no-diagrams: koi image/diagram download ya extract nahi karega -- sirf fast JSON banega")
        print("  --fill-images: pehle se bani JSON mein baad mein diagrams/images fill karega")
        print("  --amedex: Amedex app ke screenshots wali PDF (OCR + ✓/✗ marks se jawab)")
        sys.exit(1)

    args = sys.argv[1:]

    limit = None
    limit_args = [a for a in args if a.startswith("--limit=")]
    if limit_args:
        limit = int(limit_args[0].split("=", 1)[1])
        args = [a for a in args if not a.startswith("--limit=")]

    for _a in list(args):
        if _a.startswith("--pages="):
            os.environ["M2_MAX_PAGES"] = _a.split("=", 1)[1]
            args.remove(_a)
            print(f"[test] sirf pehle {os.environ['M2_MAX_PAGES']} pages process honge")
    global ASSUME_IMAGES, IMAGES_ONLY, FRESH
    FRESH = "--fresh" in args
    args = [a for a in args if a != "--fresh"]
    ASSUME_IMAGES = "--assume-images" in args
    args = [a for a in args if a != "--assume-images"]
    IMAGES_ONLY = "--images-only" in args
    args = [a for a in args if a != "--images-only"]
    no_diagrams = "--no-diagrams" in args
    args = [a for a in args if a != "--no-diagrams"]
    fill_images = "--fill-images" in args
    args = [a for a in args if a != "--fill-images"]
    offline_images = "--offline-images" in args
    args = [a for a in args if a != "--offline-images"]
    amedex = "--amedex" in args
    args = [a for a in args if a != "--amedex"]

    # last arg is output_dir only if it's not an existing pdf/image file
    if not (args[-1].lower().endswith(".pdf") or Path(args[-1]).suffix.lower() in IMAGE_EXTS):
        out_dir = args[-1]
        args = args[:-1]
    else:
        out_dir = "parsed_output"

    image_inputs = [a for a in args if Path(a).suffix.lower() in IMAGE_EXTS]
    pdf_inputs = [a for a in args if a.lower().endswith(".pdf")]

    os.makedirs(out_dir, exist_ok=True)

    if image_inputs:
        source_name = Path(image_inputs[0]).stem
        run_notes_mode(image_inputs, out_dir, source_name)
        return

    if pdf_inputs and amedex:
        run_amedex_mode(pdf_inputs[0], out_dir, limit=limit)
        return

    if pdf_inputs:
        run_pdf_mode(pdf_inputs[0], out_dir, limit=limit, no_diagrams=no_diagrams, fill_images=fill_images, offline_images=offline_images)
        return

    print("Koi valid .pdf ya image file nahi mili input mein.")
    sys.exit(1)


def _clean_md_flow(md):
    """Token hatao + beech se toote hue paragraph jodo (prev bina . ? ! : ke khatam, next lowercase se shuru)."""
    if not md:
        return md
    md = md.replace(NOTES_START_TOKEN, "")
    md = re.sub(r"(?m)^\s*[-*]\s*$\n?", "", md)
    parts = re.split(r"\n\s*\n", md)
    out = []
    for p in parts:
        p = p.strip("\n")
        if not p.strip():
            continue
        plain = lambda t: not re.match(r"^\s*([-*#|>!]|\d+\.|\*\*)", t)
        if out and plain(out[-1]) and plain(p) and re.search(r"[A-Za-z,;\-]\s*$", out[-1]) and re.match(r"^\s*[a-z]", p) \
                and "\n" not in out[-1].strip() and "![" not in p:
            out[-1] = out[-1].rstrip() + " " + p.lstrip()
        else:
            out.append(p)
    return "\n\n".join(out)


def run_pdf_mode(pdf_path, out_dir, limit=None, no_diagrams=False, fill_images=False, offline_images=False):
    work_dir = os.path.join(out_dir, "_work")
    img_dir = os.path.join(out_dir, "images")
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(img_dir, exist_ok=True)

    source_name = Path(pdf_path).stem

    acquire_wake_lock()
    print_banner()
    if HAVE_GEO:
        print(f"{C.GREEN}[geo] PDF geometry backend: {GEO_BACKEND}  |  m2 version: {PARSER_VERSION}{C.RESET}")
    else:
        print(f"\033[1;31m[!] Geometry backend NAHI mila: {GEO_BACKEND}. Tables/green-bar/headings/links DISABLED. "
              f"Termux: pkg install poppler && pip install pypdf\033[0m")

    # --offline-images fast-path: PDF ke andar se 2500+ embedded images ka
    # mutool-extraction step ([1/5], jo bhaari CPU/IO kaam hai aur kam-RAM
    # phone pe Termux crash/OOM-kill tak kar sakta hai) skip karne ke liye,
    # img_dir mein pehle se maujood downloaded diagram files ko seedha
    # unke FILENAME PATTERN se scan karke diagram-mapping bana lete hain
    # (source_name_q<N>_diagram_<idx>.ext). Ye purani JSON pe depend nahi
    # karta -- agar kisi wajah se purani JSON mein diagram-mapping
    # save hi nahi hui thi (jaise koi interrupted run), tab bhi ye
    # reliably kaam karta hai kyunki files khud hi disk pe mojood ground-
    # truth hain.
    prebuilt_diagram_map = None
    image_paths = []
    if offline_images and os.path.isdir(img_dir):
        diagram_pattern = re.compile(re.escape(source_name) + r"_q(\d+)_diagram_(\d+)\.")
        found = {}  # qnum -> {idx: (size, filename)}  -- ek idx ke liye ek hi file rakhte hain
        dup_count = 0
        for fname in os.listdir(img_dir):
            m = diagram_pattern.match(fname)
            if not m:
                continue
            fpath = os.path.join(img_dir, fname)
            fsize = os.path.getsize(fpath)
            if fsize <= 200:
                continue
            qnum = int(m.group(1))
            idx = int(m.group(2))
            q_entries = found.setdefault(qnum, {})
            if idx in q_entries:
                # Do alag files isi (qnum, idx) ke liye mil gayi (jaise
                # purane runs mein alag extension se dobara try hui thi,
                # ya ek fallback ".png" bhi ban gayi thi asal file ke
                # sath) -- dono ko separate images dikhana galat/duplicate
                # hoga, isliye BADI file (zyada mumkin hai ke wo sharp/
                # complete original ho, na ke koi chhota partial attempt)
                # ko rakhte hain aur dusri ko discard.
                dup_count += 1
                prev_size, prev_fname = q_entries[idx]
                if fsize <= prev_size:
                    continue  # purani wali hi behtar/barabar hai, is naye duplicate ko ignore karo
            q_entries[idx] = (fsize, fname)
        if found:
            prebuilt_diagram_map = {}
            for qnum, q_entries in found.items():
                items = sorted(q_entries.items())  # idx ke hisaab se, diagram_0, diagram_1...
                prebuilt_diagram_map[qnum] = [
                    {
                        "file": os.path.relpath(os.path.join(img_dir, fname), out_dir),
                        "caption": "Diagram extracted from source PDF (verify/edit caption manually).",
                        "page": None,
                    }
                    for idx, (fsize, fname) in items
                ]
            total_imgs = sum(len(v) for v in prebuilt_diagram_map.values())
            dup_note = f" ({dup_count} duplicate file(s) discard kiye){C.RESET}" if dup_count else f"{C.RESET}"
            print(f"{C.GREEN}[1/5] --offline-images: disk pe pehle se maujood {total_imgs} "
                  f"image file(s) se {len(prebuilt_diagram_map)} questions ka diagram-mapping bana raha hoon -- "
                  f"PDF se dobara image-extraction skip{dup_note}")



    if prebuilt_diagram_map is None:
        print(f"{C.CYAN}[1/5] Extracting embedded images from {pdf_path} ...{C.RESET}")
        image_paths = extract_images(pdf_path, work_dir)
        print(f"{C.CYAN}      -> {len(image_paths)} images found{C.RESET}")

    real_text = get_pdf_text_if_real(pdf_path)

    if real_text is not None and split_into_questions(real_text) is not None:
        run_multi_question_mode(pdf_path, real_text, out_dir, source_name, image_paths, limit=limit, no_diagrams=no_diagrams, fill_images=fill_images, offline_images=offline_images, prebuilt_diagram_map=prebuilt_diagram_map)
        return

    if real_text is not None:
        print("[2/5] Real text-layer PDF detect hua -- OCR skip, direct text use kar rahe hain ...")
        lines = clean_lines(real_text)
        total_pages = len(compute_page_bounds(real_text))
        rendered_pages = render_page_range(pdf_path, 1, total_pages, work_dir, tag="all")
        diagram_files = []
        for path in rendered_pages:
            cropped_path = autocrop_diagram(str(path))
            if cropped_path == str(path):
                continue
            idx = len(diagram_files)
            final_name = f"{source_name}_diagram_{idx}.png"
            final_path = os.path.join(img_dir, final_name)
            os.replace(cropped_path, final_path)
            diagram_files.append({
                "file": os.path.relpath(final_path, out_dir),
                "caption": "Diagram extracted from source PDF (verify/edit caption manually)."
            })
        print(f"      -> {len(diagram_files)} diagram image(s) saved to {img_dir}/ (high-DPI page render, not blurry)")
    else:
        print("[2/5] Real text nahi mila -- OCR + classifying text vs diagram images ...")
        full_text, diagram_images = classify_and_ocr(image_paths, psm="3", enhance=True)
        lines = clean_lines(full_text)

        diagram_files = []
        for idx, path in enumerate(diagram_images):
            cropped_path = autocrop_diagram(path)
            ext = ".png" if cropped_path != path else Path(path).suffix
            final_name = f"{source_name}_diagram_{idx}{ext}"
            final_path = os.path.join(img_dir, final_name)
            os.replace(cropped_path, final_path)
            diagram_files.append({
                "file": os.path.relpath(final_path, out_dir),
                "caption": "Diagram extracted from source PDF (verify/edit caption manually)."
            })
        print(f"      -> {len(diagram_files)} diagram image(s) saved to {img_dir}/")

    print("[3/5] Cleaning noise lines ...")
    # (lines already cleaned above in both branches)

    print("[4/5] Structuring question + notes ...")
    if real_text is not None:
        # text-mode: paragraph-aware split (proper paragraph breaks +
        # generic notes-heading detection, not hardcoded to "Murmurs")
        paragraphs = paragraphs_from_text(real_text)
        question = parse_question_paragraphs(paragraphs)
        notes = parse_notes_by_indent(question["notes_lines"])
    else:
        # OCR-mode: keep the original flat-line + "Murmurs"-keyword split
        split_idx = next((i for i, l in enumerate(lines) if l.rstrip(" *®★") in HEADING_KEYWORDS[:1]), len(lines))
        question_lines = lines[:split_idx]
        notes_lines = lines[split_idx:]
        question = parse_question_block(question_lines)
        notes = parse_notes_block(notes_lines)

    print("[5/5] Writing JSON output ...")
    answer_key, student_view = build_json(question, notes, diagram_files, source_name)

    ak_path = os.path.join(out_dir, f"{source_name}_answer_key.json")
    sv_path = os.path.join(out_dir, f"{source_name}_student_view.json")
    _atomic_write_json_global(ak_path, answer_key)
    _atomic_write_json_global(sv_path, student_view)

    print(f"\n{C.GREEN}{C.BOLD}Done.{C.RESET}\n{C.GREEN}  Answer key   -> {ak_path}\n  Student view -> {sv_path}\n  Images       -> {img_dir}/{C.RESET}")
    write_bundled_viewer(out_dir, ak_path)
    print("\n  Tip: agar notes section theek se structure nahi hua, notes ke")
    print("  screenshots alag se is tarah bhejo:")
    print(f"    python3 pdf_qbank_parser.py notes1.jpg notes2.jpg ... {out_dir}")


if __name__ == "__main__":
    main()
