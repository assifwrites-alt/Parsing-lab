"""End-to-end: parse a synthetic PDF, flag a weak question, fix it through a MOCK vision API, and check the no-key path."""
import http.server, json, os, subprocess, sys, threading
import pytest
HERE = os.path.dirname(__file__); sys.path.insert(0, os.path.join(HERE, "..", "worker"))
import make_sample

class Mock(http.server.BaseHTTPRequestHandler):
    calls = 0
    def do_POST(self):
        self.rfile.read(int(self.headers["content-length"])); Mock.calls += 1
        q = make_sample.QS[2]
        fixed = {"text": q[0], "options": [{"id": chr(97 + i), "text": o, "percentage": p} for i, (o, p) in enumerate(zip(q[1], q[3]))],
                 "correct_answer_id": "b", "explanation": q[4], "answer_evidence": "Option B is highlighted green in the image", "unreadable": []}
        body = json.dumps({"id": "m", "type": "message", "role": "assistant", "model": "mock", "content": [{"type": "text", "text": json.dumps(fixed)}],
                           "stop_reason": "end_turn", "usage": {"input_tokens": 1000, "output_tokens": 200}}).encode()
        self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass

def run(pdf, out, env_extra, *flags):
    env = dict(os.environ, **env_extra); env.pop("ANTHROPIC_API_KEY", None) if "ANTHROPIC_API_KEY" not in env_extra else None
    r = subprocess.run([sys.executable, "-m", "lab.pipeline", pdf, out, *flags], cwd=os.path.join(HERE, "..", "worker"), env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    return json.load(open(os.path.join(out, "student_view.json")))

@pytest.fixture(scope="module")
def pdf(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("e") / "flagged.pdf"); make_sample.build(p, variant=True); return p

def test_no_key_marks_needs_human_never_verified(pdf, tmp_path):
    v = run(pdf, str(tmp_path / "o"), {})
    q3 = [q for q in v["questions"] if q["number"] == 3][0]
    assert q3["status"] == "needs_human" and any(f["code"] == "answer_unconfirmed" for f in q3["flags"])
    assert all(q["status"] == "verified" for q in v["questions"] if q["number"] != 3)
    assert "ANTHROPIC_API_KEY" in open(str(tmp_path / "o" / "run_summary.md")).read()

def test_vision_fix_with_mock_api(pdf, tmp_path):
    srv = http.server.HTTPServer(("127.0.0.1", 0), Mock); threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{srv.server_port}"}
    out = str(tmp_path / "o"); v = run(pdf, out, env)
    q3 = [q for q in v["questions"] if q["number"] == 3][0]
    assert q3["status"] == "verified (vision-fixed)", q3
    log = json.load(open(os.path.join(out, "_flags.json")))["fix_log"]; assert log and log[0]["flags_after"] == []
    n = Mock.calls; run(pdf, out, env, "--skip-parse"); assert Mock.calls == n   # cache: re-run costs nothing
    srv.shutdown()
