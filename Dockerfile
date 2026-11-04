#   docker build -t safearly/smriti .
# Base image pinned by digest; dependencies installed from requirements.lock with hashes.
# Refresh both together: `uv pip compile pyproject.toml --extra ... --generate-hashes -o requirements.lock`
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SMRITI_DATA_DIR=/data \
    SMRITI_PACKS=/app/packs/tenants/railways-demo
WORKDIR /app

# git is needed to install Saaf from its repository (pinned to a commit in pyproject.toml)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /app/requirements.lock
# every third-party wheel must match its recorded hash; Saaf (a git dependency) is pinned by commit
RUN grep -v "^saaf @ git+" /app/requirements.lock > /tmp/req.txt \
 && pip install --no-cache-dir --require-hashes -r /tmp/req.txt \
 && pip install --no-cache-dir --no-deps "$(grep '^saaf @ git+' /app/requirements.lock | cut -d' ' -f1-3)"
COPY . /app
RUN pip install --no-cache-dir --no-deps /app \
 && useradd --system --uid 10001 smriti && mkdir -p /data && chown smriti /data

USER smriti
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["smriti", "serve", "--host", "0.0.0.0", "--port", "8000", "--seed"]
