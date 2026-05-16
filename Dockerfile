FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MOZ_HEADLESS=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    firefox-esr \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
