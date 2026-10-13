"""Stage 3: verify every parsed question against the SOURCE. Never drops a question, never marks unverified as verified."""
import re
from rapidfuzz import fuzz
from .source import norm

NOISE = [
    (r"chat with (the )?ai tutor", "UI text 'Chat with AI Tutor'"), (r"\b\d{1,2}:\d{2}\s?(am|pm)\b", "timestamp"),
    (r"https?://\S+|www\.\S+", "URL in question text"), (r"[-]", "icon-font glyph"),
    (r"Ã.|â€|�", "mojibake"), (r"\b(share|report|bookmark|flag) (question|this)\b", "button text"),
]
IMG_REF = re.compile(r"\b(shown|image|photograph|picture|figure|below|ecg|x-?ray|ct scan|mri|scan|film|graph|chart|slide|biopsy)\b", re.I)
EXPL_ANS = re.compile(r"(?:correct\s+(?:answer|option)\s*(?:is|:)|answer\s+is|the\s+answer\s*:)\s*\(?([A-H])\b", re.I)

class Segments:
    """question number/set -> text between its 'Question N of M' marker and the next marker."""
    def __init__(self, src):
        self.src, self.pages = src, list(range(1, src.n + 1))
        texts = [src.page_text(p) for p in self.pages]
        self.full, self.starts = "", []
        for t in texts:
            self.starts.append(len(self.full)); self.full += t + "\f"
        self.marks = [(m.start(), int(m.group(1)), int(m.group(2))) for m in re.finditer(r"Question\s+(\d+)\s+of\s+(\d+)", self.full, re.I)]
    def page_of(self, off):
        pg = 1
        for i, s in enumerate(self.starts):
            if s <= off: pg = i + 1
        return pg
    def get(self, q):
        """returns (segment_text, [pages], how)"""
        sp = [int(x) for x in (q.get("source_pages") or []) if str(x).isdigit()]
        if sp:
            return "\n".join(self.src.page_text(p) for p in sp if 1 <= p <= self.src.n), sp, "source_pages"
        num, tot = q.get("set_number") or q.get("number"), q.get("set_total")
        cands = [i for i, (o, n, t) in enumerate(self.marks) if n == num and (not tot or t == tot)]
        if cands:
            # PassMedicine repeats the "Question N of M" header on the answer/explanation page(s) of the SAME question, so the segment must run
            # to the next marker of a DIFFERENT question (stopping at the repeat cut off every explanation: the 37 false low_explanation_match flags).
            i = cands[0]; a = self.marks[i][0]; n0, t0 = self.marks[i][1], self.marks[i][2]
            j = i + 1
            while j < len(self.marks) and (self.marks[j][1], self.marks[j][2]) == (n0, t0): j += 1
            b = self.marks[j][0] if j < len(self.marks) else len(self.full)
            return self.full[a:b], list(range(self.page_of(a), self.page_of(max(a, b - 1)) + 1)), "marker"
        # fuzzy: best page for the stem
        stem = norm(q.get("text"))[:200]; best, bp = 0, None
        for p in self.pages:
            s = fuzz.partial_ratio(stem, norm(self.src.page_text(p)))
            if s > best: best, bp = s, p
        if bp and best >= 80:
            return "\n".join(self.src.page_text(p) for p in (bp, bp + 1) if p <= self.src.n), [bp], "fuzzy"
        return "", [], "none"

def _strip_opt(t):
    return re.sub(r"^\s*\(?[A-Ha-h][\.\):]\s+", "", t or "")

def answer_signals(q):
    sig = {}
    opts = q.get("options") or []
    src = str(q.get("correct_source") or "")
    cid = q.get("correct_answer_id")
    if cid:
        sig["peer_percentage" if "percent" in src else "marker:" + (src or "parser")] = str(cid).lower()
    m = EXPL_ANS.search(q.get("explanation") or "")
    if m: sig["explanation_text"] = m.group(1).lower()
    pcts = [(o.get("percentage"), str(o.get("id")).lower()) for o in opts if isinstance(o.get("percentage"), (int, float))]
    if len(pcts) >= 2:
        top = sorted(pcts, reverse=True)
        if top[0][0] > top[1][0]: sig.setdefault("max_percentage", top[0][1])
    return sig

def verify_question(q, seg_text, pages, how, images_expected=None):
    flags, info = [], {"pages": pages, "locate": how}
    add = lambda code, detail="": flags.append({"code": code, "detail": detail})
    opts = q.get("options") or []; ids = [str(o.get("id", "")).lower() for o in opts]
    # (a) structure
    if not (q.get("text") or "").strip(): add("empty_stem")
    if len(opts) < 2: add("too_few_options", f"{len(opts)} options")
    if ids != [chr(97 + i) for i in range(len(ids))]: add("option_ids_not_sequential", ",".join(ids))
    cid = str(q.get("correct_answer_id") or "").lower()
    if not cid or cid not in ids: add("no_valid_correct_answer", f"id={cid!r}")
    if sum(1 for o in opts if o.get("correct")) > 1: add("multiple_correct_marked")
    if not (q.get("explanation") or "").strip(): add("empty_explanation")
    # (d) answer signals
    sig = answer_signals(q); info["answer_signals"] = sig
    vals = {x for k, x in sig.items() if k != "max_percentage"}   # the most-picked option by peers is often NOT the right answer, so it must not count as a disagreeing signal
    if len(vals) > 1: add("answer_signals_disagree", str(sig))
    elif {k.split(":")[0] for k in sig} <= {"peer_percentage", "max_percentage"}:
        add("answer_unconfirmed", "only the peer-response percentage supports this answer (no marker, no explanation text)")
    # (b) source match
    if seg_text:
        sn = norm(seg_text)
        stem = fuzz.partial_ratio(norm(q.get("text")), sn) if q.get("text") else 0
        om = [fuzz.partial_ratio(norm(_strip_opt(o.get("text"))), sn) for o in opts if (o.get("text") or "").strip()]
        ex = (q.get("explanation") or "")
        ex_plain = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", ex)                      # markdown images
        ex_plain = re.sub(r"^\s*>\s?", "", ex_plain, flags=re.M)                       # blockquote markers
        ex_plain = re.sub(r"^\s*[-*]\s+|^#+\s*", " ", ex_plain, flags=re.M)            # list / heading markers
        ex_plain = re.sub(r"\*\*|__|`", " ", ex_plain)                                 # bold / code markers
        sents = [s for s in re.split(r"(?<=[.!?])\s+|\n+", re.sub(r"^\W*explanation\W*", "", ex_plain, flags=re.I)) if len(s) >= 25]
        em = (100 * sum(1 for s in sents if fuzz.partial_ratio(norm(s), sn) >= 88) / len(sents)) if sents else 100.0
        info["match"] = {"stem": round(stem), "options_mean": round(sum(om) / len(om)) if om else 0, "options_min": round(min(om)) if om else 0, "explanation": round(em)}
        if stem < 90: add("low_stem_match", f"{stem:.0f}%")
        if om and min(om) < 85: add("low_option_match", f"min {min(om):.0f}%")
        if em < 80: add("low_explanation_match", f"{em:.0f}%")
        # (c) option count vs source
        letters = set(re.findall(r"(?m)^\s*\(?([A-H])[\.\)]\s+\S", seg_text))
        info["source_option_letters"] = len(letters)
        if len(letters) >= 2 and len(letters) != len(opts): add("option_count_mismatch", f"parsed {len(opts)}, source {len(letters)}")
    else:
        add("source_not_located", "could not find this question in the source text; nothing to compare against")
    pc = [o["percentage"] for o in opts if isinstance(o.get("percentage"), (int, float))]
    if pc and len(pc) == len(opts) and not (85 <= sum(pc) <= 120): add("percentages_sum", f"{sum(pc):.0f}%")
    # (e) completeness
    ex = (q.get("explanation") or "").strip()
    if ex and not re.search(r"[.!?)\]\"'`*]$|\|\s*$", ex): add("explanation_truncated", "…" + ex[-40:])
    imgs = q.get("images") or q.get("diagrams") or []
    if IMG_REF.search(q.get("text") or "") and not imgs and re.search(r"\b(shown|image|photograph|picture|figure|below)\b", q.get("text") or "", re.I):
        add("image_possibly_missing", "stem refers to an image but none is linked")
    # (f) noise
    blob = " ".join([q.get("text") or ""] + [o.get("text") or "" for o in opts] + [ex])
    for pat, name in NOISE:
        if re.search(pat, blob, re.I): add("noise_leak", name)
    for w in (q.get("warnings") or []):
        if "guessed_from_max_percentage" in w: continue  # handled by answer_unconfirmed with the explanation signal taken into account
        add("parser_warning", str(w)[:160])
    return flags, info

def verify_all(questions, src):
    seg = Segments(src); res = {}
    nums = [(q.get("set_number") or q.get("number"), q.get("set_total")) for q in questions]
    for i, q in enumerate(questions):
        text, pages, how = seg.get(q)
        flags, info = verify_question(q, text, pages, how)
        # (g) numbering within a set
        n, t = nums[i]
        if i > 0 and nums[i - 1][1] == t and isinstance(n, int) and isinstance(nums[i - 1][0], int):
            d = n - nums[i - 1][0]
            if d == 0: flags.append({"code": "duplicate_number", "detail": f"Q{n}"})
            elif d > 1: flags.append({"code": "skipped_numbers", "detail": f"after Q{nums[i-1][0]} comes Q{n}"})
        res[q.get("id") or str(i)] = {"status": "verified" if not flags else "flagged", "flags": flags, "info": info}
    return res
