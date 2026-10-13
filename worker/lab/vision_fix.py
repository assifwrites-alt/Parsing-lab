"""Stage 4: fix flagged questions one by one using page images + Claude vision. Reads ONLY what is visible; re-verifies after."""
import copy, json, os
from . import vision
from .source import render_pages
from .verify import Segments, verify_question

SYSTEM = ("You are a careful transcription checker for medical exam questions. Read ONLY what is visible in the page image(s). "
          "Do not invent, complete or paraphrase text. If something is unreadable, say so in 'unreadable'.")
PROMPT = """Below is a parsed question (JSON) and the reasons it was flagged. Compare it with the page image(s) and return the CORRECTED question as JSON only.
Keep keys: text, options (list of {{id, text, percentage}}; ids a,b,c... in order, option text WITHOUT the letter prefix), correct_answer_id, explanation.
Rules: copy text exactly as shown (keep medical terms, numbers, units). correct_answer_id = the option marked correct in the image (green tick/highlight) or stated in the explanation; if the image gives no clear evidence put null and explain in 'unreadable'.
Also return "answer_evidence" (what you saw that decides the answer) and "unreadable" (list, empty if none).

Flags: {flags}

Parsed question:
{q}
"""

def _apply(q, fixed):
    n = copy.deepcopy(q)
    for k in ("text", "explanation"):
        if isinstance(fixed.get(k), str) and fixed[k].strip(): n[k] = fixed[k].strip()
    if isinstance(fixed.get("options"), list) and len(fixed["options"]) >= 2:
        opts = []
        for i, o in enumerate(fixed["options"]):
            t = str(o.get("text", "")).strip(); letter = chr(97 + i)
            opts.append({"id": letter, "text": f"{letter.upper()}. {t}", "percentage": o.get("percentage") if isinstance(o.get("percentage"), (int, float)) else None, "correct": False})
        n["options"] = opts
    cid = fixed.get("correct_answer_id")
    if isinstance(cid, str) and cid.lower() in [o["id"] for o in n.get("options", [])]:
        n["correct_answer_id"] = cid.lower(); n["correct_source"] = "vision"
        for o in n["options"]: o["correct"] = o["id"] == cid.lower()
    return n

def fix_flagged(questions, results, src, budget, log, dpi_retry=300):
    """Mutates results (status/flags/fix log). Returns the possibly corrected questions list."""
    seg = Segments(src); qs = list(questions)
    for i, q in enumerate(qs):
        qid = q.get("id") or str(i); r = results[qid]
        if r["status"] != "flagged": continue
        flags = [f for f in r["flags"] if f["code"] not in ("skipped_numbers", "duplicate_number")]
        if not flags: continue
        pages = r["info"].get("pages") or []
        if not pages:
            r.update({"status": "needs_human", "note": "could not locate source pages for this question"}); continue
        attempt_ok = False; why = None
        for attempt, dpi in enumerate((200, dpi_retry)):
            if dpi == 200:
                imgs = render_pages(src.pdf, src.out, dpi=200, first=min(pages), last=max(pages))
            else:   # retry at higher DPI into a separate folder; the 200dpi ground-truth pages stay untouched
                import subprocess
                d = os.path.join(src.out, "_pages_hi"); os.makedirs(d, exist_ok=True)
                subprocess.run(["pdftoppm", "-r", str(dpi), "-f", str(min(pages)), "-l", str(max(pages)), "-png", src.pdf, os.path.join(d, f"q{qid}")], capture_output=True, timeout=600)
                imgs = sorted(os.path.join(d, f) for f in os.listdir(d) if f.startswith(f"q{qid}-"))
            if not imgs: why = "page images unavailable"; break
            text = vision.ask(budget, f"fix:{qid}:{dpi}", PROMPT.format(flags=json.dumps(flags, ensure_ascii=False), q=json.dumps({k: q.get(k) for k in ("text", "options", "correct_answer_id", "explanation")}, ensure_ascii=False)), [(p, None) for p in imgs[:3]], system=SYSTEM)
            if text is None: why = budget.error or "vision unavailable"; break
            fixed = vision.json_from(text)
            if not fixed: why = "vision returned no JSON"; continue
            if fixed.get("unreadable"): why = "unreadable per vision: " + "; ".join(map(str, fixed["unreadable"]))[:200]
            cand = _apply(q, fixed)
            seg_text, pg2, how2 = seg.get(q)   # compare against the same source segment
            nf, ni = verify_question(cand, seg_text, pages, how2)
            hard = [f for f in nf if f["code"] not in ("skipped_numbers", "duplicate_number")]
            log.append({"id": qid, "attempt": attempt + 1, "dpi": dpi, "before": {k: q.get(k) for k in ("text", "options", "correct_answer_id", "explanation")},
                        "after": {k: cand.get(k) for k in ("text", "options", "correct_answer_id", "explanation")}, "flags_before": flags, "flags_after": hard, "evidence": fixed.get("answer_evidence")})
            if not hard and not fixed.get("unreadable"):
                qs[i] = cand; r.update({"status": "verified (vision-fixed)", "flags": [], "info": ni, "evidence": fixed.get("answer_evidence")}); attempt_ok = True; break
            why = f"still flagged after vision: {[f['code'] for f in hard]}"
        if not attempt_ok:
            r.update({"status": "needs_human", "note": ("API vision band (key nahi): Termux Claude se auto-fix ya aap review karo." if "ANTHROPIC_API_KEY" in (why or "") else (why or "vision fix failed")), "page_images": [os.path.join("_pages", f"page_{p:03d}.png") for p in pages]})
    return qs
