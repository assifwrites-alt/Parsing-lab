import copy, json, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "worker"))
import pytest
from lab.source import Source
from lab.verify import verify_all
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "worker"))
import make_sample

@pytest.fixture(scope="module")
def env(tmp_path_factory):
    d = tmp_path_factory.mktemp("v"); pdf = str(d / "s.pdf"); make_sample.build(pdf)
    out = str(d / "out")
    subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(__file__), "..", "worker", "m2.py"), pdf, out], capture_output=True, timeout=300)
    key = [f for f in os.listdir(out) if f.endswith("_answer_key.json")][0]
    qs = json.load(open(os.path.join(out, key)))["questions"]
    return Source(pdf, out), qs

def run(env, mutate=None):
    src, qs = env; qs = copy.deepcopy(qs)
    if mutate: mutate(qs)
    return verify_all(qs, src)

def codes(r, qid): return [f["code"] for f in r[qid]["flags"]]

def test_clean_sample_all_verified(env):
    r = run(env); assert all(v["status"] == "verified" for v in r.values()), {k: codes(r, k) for k in r}

def test_wrong_answer_detected(env):
    def m(qs): qs[0]["correct_answer_id"] = "a"
    assert "answer_signals_disagree" in codes(run(env, m), "q1")

def test_truncated_explanation(env):
    def m(qs): qs[1]["explanation"] = "Hyperkalaemia causes peaked T waves, then wide"
    assert "explanation_truncated" in codes(run(env, m), "q2")

def test_noise_leak(env):
    def m(qs): qs[2]["text"] += " Chat with AI Tutor 10:42 AM"
    assert "noise_leak" in codes(run(env, m), "q3")

def test_missing_option_and_count(env):
    def m(qs): qs[3]["options"].pop()
    c = codes(run(env, m), "q4"); assert "option_count_mismatch" in c

def test_text_not_in_source(env):
    def m(qs): qs[4]["text"] = "Completely invented question about unrelated cardiac physiology and drugs."
    assert "low_stem_match" in codes(run(env, m), "q5")

def test_skipped_number(env):
    def m(qs): del qs[5]
    assert any("skipped_numbers" in codes(run(env, m), k) for k in ["q7"])

def test_only_percentage_support_is_flagged(env):
    def m(qs): qs[6]["explanation"] = "N-acetylcysteine replenishes glutathione and prevents hepatotoxicity."
    assert "answer_unconfirmed" in codes(run(env, m), "q7")


def _two_page_pdf(path):
    """PassMedicine style: each question has a question page AND an answer/explanation page that repeats the 'Question N of M' header."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path, pagesize=A4)
    Q = [("A 70-year-old man has progressive memory loss with visual hallucinations and parkinsonism. What is the most likely diagnosis?", ["Alzheimer disease", "Lewy body dementia", "Vascular dementia"], "The correct answer is Lewy body dementia. Fluctuating cognition with hallucinations and parkinsonism is characteristic of this condition."),
         ("A 80-year-old woman is investigated for sudden stepwise cognitive decline after a stroke. What is the most likely diagnosis?", ["Vascular dementia", "Pick disease", "Normal pressure hydrocephalus"], "The correct answer is Vascular dementia. A stepwise decline after cerebrovascular events points to a vascular cause rather than a degenerative one.")]
    for i, (stem, opts, ex) in enumerate(Q, 1):
        for page in (1, 2):
            y = 780; c.setFont("Helvetica", 10); c.drawString(60, y, f"Question {i} of 2"); y -= 30
            c.drawString(60, y, stem[:95]); y -= 14; c.drawString(60, y, stem[95:]); y -= 26
            for k, o in zip("ABC", opts): c.drawString(70, y, f"{k}. {o}"); y -= 16
            if page == 2:
                y -= 14; c.drawString(60, y, "Explanation"); y -= 16
                for j in range(0, len(ex), 90): c.drawString(60, y, ex[j:j + 90]); y -= 14
            c.showPage()
    c.save()


def test_repeated_header_does_not_cut_explanation(tmp_path):
    pdf = str(tmp_path / "two.pdf"); _two_page_pdf(pdf)
    src = Source(pdf, str(tmp_path / "o"))
    qs = [{"id": "q1", "number": 1, "set_total": 2, "text": "A 70-year-old man has progressive memory loss with visual hallucinations and parkinsonism. What is the most likely diagnosis?",
           "options": [{"id": "a", "text": "Alzheimer disease"}, {"id": "b", "text": "Lewy body dementia"}, {"id": "c", "text": "Vascular dementia"}], "correct_answer_id": "b",
           "explanation": "The correct answer is **Lewy body dementia**. Fluctuating cognition with hallucinations and parkinsonism is characteristic of this condition."}]
    r = verify_all(qs, src)
    assert "low_explanation_match" not in [f["code"] for f in r["q1"]["flags"]], r["q1"]
    assert r["q1"]["info"]["match"]["explanation"] >= 80
