FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/sync.py /app/sync.py

ENTRYPOINT ["python3", "/app/sync.py"]
