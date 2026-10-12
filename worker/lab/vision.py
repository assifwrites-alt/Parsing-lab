"""Anthropic vision helper: key from env only, cached results, call/cost budget guard, usage log. Never prints the key."""
import base64, hashlib, io, json, os, re, time

MODEL = os.environ.get("LAB_VISION_MODEL", "claude-sonnet-5-5")
PRICE_IN, PRICE_OUT = float(os.environ.get("LAB_PRICE_IN", "3")), float(os.environ.get("LAB_PRICE_OUT", "15"))   # USD per 1M tokens (estimate)

class Budget:
    def __init__(self, out_dir):
        self.dir = os.path.join(out_dir, "_vision_cache"); os.makedirs(self.dir, exist_ok=True)
        self.max_calls = int(os.environ.get("LAB_MAX_VISION_CALLS", "60")); self.max_usd = float(os.environ.get("LAB_MAX_VISION_USD", "3"))
        self.calls = self.in_tok = self.out_tok = self.cache_hits = 0
        self.log = os.path.join(out_dir, "_vision_usage.jsonl"); self.error = None
    @property
    def usd(self): return (self.in_tok * PRICE_IN + self.out_tok * PRICE_OUT) / 1e6
    def allowed(self): return self.calls < self.max_calls and self.usd < self.max_usd
    def available(self):
        if not os.environ.get("ANTHROPIC_API_KEY"): self.error = "ANTHROPIC_API_KEY not set"; return False
        try: import anthropic  # noqa
        except Exception: self.error = "anthropic package missing"; return False
        return self.error is None

def _png_b64(path, max_side=1568, crop=None):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    if crop: im = im.crop(crop)
    if max(im.size) > max_side:
        r = max_side / max(im.size); im = im.resize((int(im.width * r), int(im.height * r)), Image.LANCZOS)
    b = io.BytesIO(); im.save(b, "PNG"); return base64.b64encode(b.getvalue()).decode()

def ask(budget, kind, prompt, images, system="", max_tokens=1800):
    """images: list of (path, crop_or_None). Returns text, or None if unavailable/over budget/failed (reason in budget.error)."""
    key = hashlib.sha256(json.dumps([kind, prompt, system, MODEL] + [[hashlib.sha256(open(p, "rb").read()).hexdigest(), c] for p, c in images], default=str).encode()).hexdigest()
    cf = os.path.join(budget.dir, key + ".json")
    if os.path.exists(cf):
        budget.cache_hits += 1; return json.load(open(cf))["text"]
    if not budget.available(): return None
    if not budget.allowed(): budget.error = "vision budget reached (LAB_MAX_VISION_CALLS / LAB_MAX_VISION_USD)"; return None
    import anthropic
    client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _png_b64(p, crop=c)}} for p, c in images]
    content.append({"type": "text", "text": prompt})
    try:
        r = client.messages.create(model=MODEL, max_tokens=max_tokens, system=system or "You read images exactly. Never invent text.", messages=[{"role": "user", "content": content}])
    except Exception as e:
        budget.error = f"vision API error: {type(e).__name__}: {str(e)[:200]}"; return None
    text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    budget.calls += 1; budget.in_tok += r.usage.input_tokens; budget.out_tok += r.usage.output_tokens
    json.dump({"text": text, "model": MODEL, "ts": time.time()}, open(cf, "w"))
    with open(budget.log, "a") as f: f.write(json.dumps({"kind": kind, "in": r.usage.input_tokens, "out": r.usage.output_tokens, "usd": round(budget.usd, 4), "ts": time.time()}) + "\n")
    return text

def json_from(text):
    if not text: return None
    m = re.search(r"\{.*\}", text, re.S)
    try: return json.loads(m.group(0)) if m else None
    except Exception: return None
