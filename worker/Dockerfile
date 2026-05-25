# Image for the regional check workers. Each VPS pulls this from GHCR
# and runs it via `docker compose up -d`; no Python install needed on
# the host beyond Docker itself.
#
# Build from repo root so the COPY paths resolve:
#   docker build -f worker/Dockerfile -t uptimebot-worker .
FROM python:3.12-slim

# Non-root user — the worker only makes outbound HTTPS calls, no need
# for any privileges.
RUN groupadd -r worker && useradd -r -g worker worker

WORKDIR /worker

# Install deps first so the image-cache layer survives source changes.
COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the worker subtree — keeps the image small and means the
# app code never lands in this image.
COPY worker/ .

USER worker

# Unbuffered stdout/stderr so `docker logs -f` is responsive.
ENV PYTHONUNBUFFERED=1

CMD ["python", "run.py"]
