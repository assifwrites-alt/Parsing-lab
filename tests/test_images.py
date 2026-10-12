import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "worker"))
import numpy as np
from PIL import Image, ImageDraw
from lab import images

def mk(tmp, name, arr):
    d = tmp / "images"; d.mkdir(exist_ok=True); p = d / name; Image.fromarray(arr.astype("uint8")).save(p); return str(p)

def test_classification(tmp_path):
    rng = np.random.default_rng(1)
    icon = mk(tmp_path, "s_q1_diagram_0.png", rng.integers(0, 255, (30, 30, 3)))
    strip = mk(tmp_path, "s_q1_diagram_1.png", np.full((20, 400, 3), 200))
    blank = mk(tmp_path, "s_q1_diagram_2.png", np.full((300, 300, 3), 255))
    im = Image.new("RGB", (500, 400), "white"); dr = ImageDraw.Draw(im)
    for i in range(40): dr.line((rng.integers(0, 500), rng.integers(0, 400), rng.integers(0, 500), rng.integers(0, 400)), fill=(10, 10, 10), width=3)
    dr.ellipse((100, 80, 400, 330), outline="black", width=5)
    p = tmp_path / "images" / "s_q2_diagram_0.png"; im.save(p)
    r = images.classify_dir(str(tmp_path))
    dec = {os.path.basename(k): v["decision"] for k, v in r.items()}
    assert dec["s_q1_diagram_0.png"] == "noise" and dec["s_q1_diagram_1.png"] == "noise" and dec["s_q1_diagram_2.png"] == "noise"
    assert dec["s_q2_diagram_0.png"] == "useful"
    assert os.path.exists(tmp_path / "_image_decisions.json")

def test_repeated_logo_is_noise(tmp_path):
    rng = np.random.default_rng(2); logo = rng.integers(0, 255, (120, 400, 3))
    for q in (1, 2, 3, 4): mk(tmp_path, f"s_q{q}_diagram_0.png", logo)
    r = images.classify_dir(str(tmp_path))
    assert all(v["decision"] == "noise" for v in r.values())
