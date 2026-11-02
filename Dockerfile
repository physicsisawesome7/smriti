#   docker build -t safearly/smriti .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SMRITI_DATA_DIR=/data \
    SMRITI_PACKS=/app/packs/tenants/railways-demo
WORKDIR /app

# git is needed to install Saaf from its repository
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY . /app
RUN pip install --no-cache-dir "/app[tables,qdrant,postgres,s3,mcp,mysql]" \
 && useradd --system --uid 10001 smriti && mkdir -p /data && chown smriti /data

USER smriti
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["smriti", "serve", "--host", "0.0.0.0", "--port", "8000", "--seed"]
