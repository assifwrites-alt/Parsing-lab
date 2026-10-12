"""Builds a small synthetic text-layer MCQ PDF (PassMedicine-like 'Question N of M') for tests. No real exam content."""
import sys
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

QS = [
 ("A 45-year-old man has crushing chest pain for 30 minutes. ECG shows ST elevation in leads II, III, aVF. Which artery is most likely occluded?",
  ["Left anterior descending", "Right coronary artery", "Left circumflex", "Left main stem", "Posterior descending only"], 1, [8, 71, 12, 6, 3],
  "Inferior ST elevation (II, III, aVF) points to the right coronary artery.\nThe left anterior descending artery supplies the anterior wall."),
 ("Which electrolyte abnormality produces peaked T waves?",
  ["Hypokalaemia", "Hyperkalaemia", "Hypocalcaemia", "Hypernatraemia", "Hypomagnesaemia"], 1, [5, 88, 4, 2, 1],
  "Hyperkalaemia causes peaked T waves, then wide QRS and a sine-wave pattern.\nHypokalaemia gives U waves."),
 ("First-line drug for an acute asthma attack in a child with wheeze?",
  ["Oral prednisolone", "Inhaled salbutamol", "IV magnesium", "Nebulised ipratropium", "Oral montelukast"], 1, [20, 70, 3, 6, 1],
  "Inhaled salbutamol is first line. Steroids follow if the response is poor."),
 ("Which organism most commonly causes community-acquired pneumonia?",
  ["Staphylococcus aureus", "Streptococcus pneumoniae", "Klebsiella", "Legionella", "Mycoplasma"], 1, [4, 80, 3, 5, 8],
  "Streptococcus pneumoniae is the commonest cause of community-acquired pneumonia."),
 ("Best initial test for suspected hypothyroidism?",
  ["TSH", "Free T3", "Thyroid antibodies", "Ultrasound neck", "Radioiodine uptake"], 0, [90, 4, 3, 2, 1],
  "TSH is the best initial test. Free T4 is added when TSH is abnormal."),
 ("Which vitamin deficiency causes pellagra?",
  ["Thiamine", "Riboflavin", "Niacin", "Pyridoxine", "Cobalamin"], 2, [9, 6, 75, 6, 4],
  "Niacin deficiency causes dermatitis, diarrhoea and dementia."),
 ("Antidote for paracetamol overdose?",
  ["Naloxone", "Flumazenil", "N-acetylcysteine", "Atropine", "Pralidoxime"], 2, [1, 2, 95, 1, 1],
  "N-acetylcysteine replenishes glutathione and prevents hepatotoxicity."),
 ("Most common site of a carcinoid tumour in the gut?",
  ["Stomach", "Appendix", "Colon", "Oesophagus", "Duodenum"], 1, [10, 55, 20, 5, 10],
  "The appendix and small bowel are the commonest sites of carcinoid tumours."),
]
def build(path, variant=False):
    c = canvas.Canvas(path, pagesize=A4); W, H = A4; n = len(QS)
    for i, (stem, opts, ans, pct, expl) in enumerate(QS, 1):
        y = H - 60
        c.setFont("Helvetica-Bold", 11); c.drawString(50, y, f"Question {i} of {n}"); y -= 24
        c.setFont("Helvetica", 11)
        for ln in [stem[j:j+90] for j in range(0, len(stem), 90)]:
            c.drawString(50, y, ln); y -= 15
        y -= 8
        for k, (o, p) in enumerate(zip(opts, pct)):
            c.drawString(60, y, f"{'ABCDE'[k]}. {o}    {p}%"); y -= 16
        y -= 8
        c.setFont("Helvetica-Bold", 11); c.drawString(50, y, "Explanation"); y -= 16; c.setFont("Helvetica", 11)
        if not (variant and i == 3):   # variant: Q3 has no explanation signal for the answer (only peer percentages)
            c.drawString(50, y, f"The correct answer is {'ABCDE'[ans]}."); y -= 15
        for ln in expl.split("\n"):
            c.drawString(50, y, ln[:100]); y -= 15
        c.showPage()
    c.save()
def build_with_images(path, workdir):
    """Same questions, plus a header logo on every page and an ECG-like picture inside question 2 (tests image boundaries)."""
    import os
    import numpy as np
    from PIL import Image, ImageDraw
    os.makedirs(workdir, exist_ok=True)
    im = Image.new("RGB", (700, 260), "white"); dr = ImageDraw.Draw(im)
    for x in range(0, 700, 100): dr.line((x, 0, x, 260), fill=(255, 200, 200))
    dr.line([(i * 7, 130 + int(60 * np.sin(i / 3) * (1 if i % 14 < 3 else .1))) for i in range(100)], fill="black", width=3); im.save(workdir + "/ecg.png")
    lg = Image.new("RGB", (160, 40), (20, 90, 160)); ImageDraw.Draw(lg).text((10, 12), "PASSMED", fill="white"); lg.save(workdir + "/logo.png")
    c = canvas.Canvas(path, pagesize=A4); W, H = A4; n = len(QS)
    for i, (stem, opts, ans, pct, expl) in enumerate(QS, 1):
        c.drawImage(workdir + "/logo.png", W - 200, H - 50, 160, 40)
        y = H - 80; c.setFont("Helvetica-Bold", 11); c.drawString(50, y, f"Question {i} of {n}"); y -= 24; c.setFont("Helvetica", 11)
        for j in range(0, len(stem), 90): c.drawString(50, y, stem[j:j + 90]); y -= 15
        if i == 2:
            c.drawString(50, y, "The ECG is shown below."); y -= 15; c.drawImage(workdir + "/ecg.png", 50, y - 135, 350, 130); y -= 150
        y -= 8
        for k, (o, p) in enumerate(zip(opts, pct)): c.drawString(60, y, f"{'ABCDE'[k]}. {o}    {p}%"); y -= 16
        y -= 8; c.setFont("Helvetica-Bold", 11); c.drawString(50, y, "Explanation"); y -= 16; c.setFont("Helvetica", 11)
        c.drawString(50, y, f"The correct answer is {'ABCDE'[ans]}."); y -= 15
        for ln in expl.split("\n"): c.drawString(50, y, ln[:100]); y -= 15
        c.showPage()
    c.save()

if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "tests/sample_pm.pdf", variant=len(sys.argv) > 2 and sys.argv[2] == "flagged")
