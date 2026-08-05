FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MOZ_HEADLESS=1 \
    MOZ_DISABLE_CONTENT_SANDBOX=1

ARG GECKODRIVER_VERSION=0.36.0

RUN apt-get update && apt-get install -y --no-install-recommends \
    firefox-esr \
    wget \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatspi2.0-0 \
    libdbus-glib-1-2 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libu2f-udev \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libxshmfence1 \
    libxcb1 \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) gecko_arch="linux64" ;; \
        arm64) gecko_arch="linux-aarch64" ;; \
        *) echo "Unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    wget -q -O /tmp/geckodriver.tar.gz "https://github.com/mozilla/geckodriver/releases/download/v${GECKODRIVER_VERSION}/geckodriver-v${GECKODRIVER_VERSION}-${gecko_arch}.tar.gz"; \
    tar -xzf /tmp/geckodriver.tar.gz -C /usr/local/bin; \
    rm /tmp/geckodriver.tar.gz; \
    chmod +x /usr/local/bin/geckodriver; \
    firefox --version; \
    geckodriver --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
