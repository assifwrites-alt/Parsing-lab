# Dr Harry Lab (Parsing lab)

PDFs and screenshot-PDFs go in, verified MCQ question banks come out. Accuracy against the SOURCE comes first: every question is
checked against the source page text and either marked **verified**, or **flagged** and fixed with page-image vision, or left as **needs_human**.

## Parts
- **Hub (PHP, Hostinger)**: `index.html` + `api.php` (+ `agent.php`). Uploads, job queue, live progress, downloads, flag viewer. No database; JSON stores in `data/`.
- **Worker (Python, Linux)**: `worker/` - polls the hub, runs `lab.pipeline` (m2.py parse -> verify -> image check -> vision fix -> reports). Runs on Colab or a VPS, not on Hostinger shared hosting.
- Pipeline output per file: `answer_key.json`, `student_view.json`, `viewer.html`, `_flags.json`, `_flags_report.md`, `run_summary.md`, `_image_decisions.json`.

## One-command setup (Debian/Ubuntu, VPS or Colab)
```bash
git clone https://github.com/assifwrites-alt/Parsing-lab && cd Parsing-lab && bash setup.sh
```
`setup.sh` is idempotent: apt packages (`packages.txt`), pip libs (`requirements.txt`), then `worker/doctor.py`. Check anytime: `python3 worker/doctor.py`.

## Environment variables
| var | needed for |
|---|---|
| `HUB_URL`, `LAB_TOKEN` | worker connects to the hub |
| `ANTHROPIC_API_KEY` | vision fix of flagged questions (without it they stay `needs_human`; nothing is invented) |
| `LAB_VISION_MODEL` (default `claude-sonnet-5-5`), `LAB_MAX_VISION_CALLS` (60), `LAB_MAX_VISION_USD` (3) | vision model and budget guard |
| `LAB_MODE` = auto / text / amedex | force the parser mode |
| `LAB_RENDER_ALL=1` | render every page even for PDFs over 150 pages (default: on demand for flagged questions) |

## Run locally on one file
```bash
cd worker && python3 -m lab.pipeline ../some.pdf out_dir --limit 10      # add --no-vision to skip API calls
cd .. && python3 -m pytest -q tests
```

## Run the worker
- **Colab**: open `colab_worker.ipynb`, add Secrets `HUB_TOKEN` (and `ANTHROPIC_API_KEY`), run Cell 1 then Cell 2.
- **Docker/VPS**: `docker build -t drharry-worker . && docker run -e HUB_URL=... -e LAB_TOKEN=... -e ANTHROPIC_API_KEY=... drharry-worker`
- Rebuild `worker.zip` after editing `worker/`: `bash build_zip.sh`.

## Hub config (`config.php`, never committed)
`token` (login), optional `anthropic_key` or a `key.php` returning the key string, to let the site's Claude assistant answer and edit worker code.
