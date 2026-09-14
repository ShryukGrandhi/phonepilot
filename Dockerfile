FROM python:3.13-slim

# adb for --transport adb; ffmpeg only for `phonepilot video`
RUN apt-get update && apt-get install -y --no-install-recommends android-sdk-platform-tools-common adb ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd -m -u 10001 phonepilot && mkdir -p /data && chown phonepilot:phonepilot /data
USER phonepilot
VOLUME ["/data"]
EXPOSE 8080
ENV PHONEPILOT_DATA=/data
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["phonepilot", "serve", "--host", "0.0.0.0", "--port", "8080", "--data", "/data", "--secure-cookies", "--trust-proxy"]
