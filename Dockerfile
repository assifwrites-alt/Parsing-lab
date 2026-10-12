# Worker image: docker build -t drharry-worker . && docker run -e HUB_URL=... -e LAB_TOKEN=... -e ANTHROPIC_API_KEY=... drharry-worker
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
WORKDIR /app
COPY packages.txt requirements.txt ./
RUN apt-get update && xargs -a packages.txt apt-get install -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
 && (pip install --no-cache-dir -r requirements.txt || sed -E 's/==.*//; /^#/d; /^$/d' requirements.txt | xargs pip install --no-cache-dir)
COPY worker ./worker
RUN python3 worker/doctor.py || true
CMD ["sh", "-c", "python3 worker/doctor.py; exec python3 worker/worker.py"]
