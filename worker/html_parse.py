#!/usr/bin/env python3
"""Generic HTML / MHTML -> structured JSON (lab feature). Usage: html_parse.py <in.html|.mhtml> <out_dir>
Output: <name>.json = {source,title,lang,blocks:[heading|paragraph|list|table|image|code|quote], links:[...]}, images/ (embedded images from MHTML).
Generic: no site-specific selectors."""
import sys, os, re, json, email, base64, mimetypes
from html.parser import HTMLParser
from email import policy

SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "ins"}
BLOCK = {"p", "div", "section", "article", "li", "td", "th", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "figure", "figcaption", "dd", "dt"}


class P(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""; self.lang = ""; self.blocks = []; self.links = []
        self.skip = 0; self.buf = []; self.in_title = False; self.href = None
        self.list_stack = []; self.table = None; self.row = None; self.cell = None
        self.head = None; self.pre = False; self.quote = 0

    def flush(self, kind="paragraph", **kw):
        t = re.sub(r"\s+", " ", "".join(self.buf)).strip(); self.buf = []
        if t:
            self.blocks.append(dict(type=kind, text=t, **kw))

    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag in SKIP: self.skip += 1; return
        if self.skip: return
        if tag == "html": self.lang = a.get("lang", "")
        if tag == "title": self.in_title = True
        if tag in ("ul", "ol"): self.flush(); self.list_stack.append([])
        if tag == "li" and self.list_stack: self.flush()
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"): self.flush(); self.head = int(tag[1])
        if tag == "table": self.flush(); self.table = []
        if tag == "tr" and self.table is not None: self.row = []
        if tag in ("td", "th") and self.row is not None: self.cell = []
        if tag == "pre": self.flush(); self.pre = True
        if tag == "blockquote": self.flush(); self.quote += 1
        if tag in BLOCK and tag not in ("li", "td", "th", "tr", "table", "ul", "ol") and not self.head and not self.pre and self.cell is None: self.flush()
        if tag == "br": self.buf.append("\n" if self.pre else " ")
        if tag == "a" and a.get("href"): self.href = a["href"]
        if tag == "img":
            src = a.get("src") or a.get("data-src") or a.get("data-lazy-src") or ""
            if src and not src.startswith("data:image/gif"):
                self.flush()
                self.blocks.append(dict(type="image", src=src, alt=a.get("alt", ""), width=a.get("width"), height=a.get("height")))

    def handle_endtag(self, tag):
        if tag in SKIP: self.skip = max(0, self.skip - 1); return
        if self.skip: return
        if tag == "title": self.in_title = False
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self.head:
            self.flush("heading", level=self.head); self.head = None
        elif tag == "li" and self.list_stack:
            t = re.sub(r"\s+", " ", "".join(self.buf)).strip(); self.buf = []
            if t: self.list_stack[-1].append(t)
        elif tag in ("ul", "ol") and self.list_stack:
            items = self.list_stack.pop()
            if items: self.blocks.append(dict(type="list", ordered=tag == "ol", items=items))
        elif tag in ("td", "th") and self.cell is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip()); self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row): self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            if self.table: self.blocks.append(dict(type="table", rows=self.table))
            self.table = None
        elif tag == "pre":
            t = "".join(self.buf).strip("\n"); self.buf = []; self.pre = False
            if t.strip(): self.blocks.append(dict(type="code", text=t))
        elif tag == "blockquote":
            self.flush("quote"); self.quote = max(0, self.quote - 1)
        elif tag == "a":
            self.href = None
        elif tag in BLOCK and self.cell is None and not self.list_stack:
            self.flush()

    def handle_data(self, d):
        if self.skip: return
        if self.in_title: self.title += d; return
        if self.cell is not None: self.cell.append(d); return
        self.buf.append(d)
        if self.href:
            t = d.strip()
            if t: self.links.append({"text": t, "href": self.href})


def load(path):
    raw = open(path, "rb").read()
    imgs = {}
    if raw[:600].lower().find(b"mime-version") >= 0 and b"multipart" in raw[:2000].lower():
        msg = email.message_from_bytes(raw, policy=policy.default)
        html = None
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html" and html is None:
                html = part.get_content()
            elif ct.startswith("image/"):
                loc = part.get("Content-Location") or ""
                cid = (part.get("Content-ID") or "").strip("<>")
                imgs[loc or "cid:" + cid] = (ct, part.get_payload(decode=True))
        return html or "", imgs
    for enc in ("utf-8", "cp1256", "latin-1"):
        try: return raw.decode(enc), imgs
        except UnicodeDecodeError: pass
    return raw.decode("utf-8", "replace"), imgs


def main():
    if len(sys.argv) < 3: sys.exit(__doc__)
    src, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    html, imgs = load(src)
    p = P(); p.feed(html); p.flush()
    base = os.path.splitext(os.path.basename(src))[0]
    saved = 0
    if imgs:
        d = os.path.join(out, "images"); os.makedirs(d, exist_ok=True)
        name_of = {}
        for k, (ct, data) in imgs.items():
            if not data: continue
            ext = mimetypes.guess_extension(ct) or ".bin"
            fn = f"img_{saved+1:04d}{ext}"; open(os.path.join(d, fn), "wb").write(data)
            name_of[k] = "images/" + fn; saved += 1
        for b in p.blocks:
            if b["type"] == "image":
                b["file"] = name_of.get(b["src"])
    for b in p.blocks:  # inline data: images
        if b["type"] == "image" and b["src"].startswith("data:image/"):
            m = re.match(r"data:image/(\w+);base64,(.*)", b["src"], re.S)
            if m:
                d = os.path.join(out, "images"); os.makedirs(d, exist_ok=True)
                saved += 1; fn = f"img_{saved:04d}.{m.group(1)}"
                open(os.path.join(d, fn), "wb").write(base64.b64decode(m.group(2)))
                b["file"] = "images/" + fn; b["src"] = "(embedded)"
    res = {"source": os.path.basename(src), "title": re.sub(r"\s+", " ", p.title).strip(), "lang": p.lang,
           "counts": {"blocks": len(p.blocks), "images": sum(b["type"] == "image" for b in p.blocks), "saved_images": saved,
                      "tables": sum(b["type"] == "table" for b in p.blocks), "links": len(p.links)},
           "blocks": p.blocks, "links": p.links}
    json.dump(res, open(os.path.join(out, base + ".json"), "w", encoding="utf8"), ensure_ascii=False, indent=1)
    print("ok", res["counts"])

main()
