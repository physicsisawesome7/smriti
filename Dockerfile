#   docker build -t safearly/smriti .
# Base image pinned by digest; dependencies installed from requirements.lock with hashes.
# Refresh both together: `uv pip compile pyproject.toml --extra ... --generate-hashes -o requirements.lock`
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SMRITI_DATA_DIR=/data \
    SMRITI_PACKS=/app/packs/tenants/railways-demo
WORKDIR /app

COPY requirements.lock /app/requirements.lock
COPY vendor/saaf-0.1.0.dev0-py3-none-any.whl /tmp/vendor/
# Every wheel must match its recorded hash. Saaf comes from the vendored wheel
# (built reproducibly from the commit pinned in pyproject.toml; see
# vendor/README.md), so the build needs neither git nor GitHub.
ARG SAAF_SHA256=440c0fedfd076bc55b02ffe02fd88d6d1b047a2b88b5f5d3d578bc9b42d04094
RUN grep -v "^saaf @ git+" /app/requirements.lock > /tmp/req.txt \
 && echo "saaf @ file:///tmp/vendor/saaf-0.1.0.dev0-py3-none-any.whl --hash=sha256:${SAAF_SHA256}" >> /tmp/req.txt \
 && pip install --no-cache-dir --require-hashes -r /tmp/req.txt \
 && rm -rf /tmp/vendor /tmp/req.txt
COPY . /app
RUN pip install --no-cache-dir --no-deps /app \
 && useradd --system --uid 10001 smriti && mkdir -p /data && chown smriti /data

USER smriti
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["smriti", "serve", "--host", "0.0.0.0", "--port", "8000", "--seed"]
