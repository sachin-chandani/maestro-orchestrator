FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY maestro ./maestro
# [postgres] so MAESTRO_DB_URL=postgresql://... works out of the box; SQLite is still the default.
RUN pip install --no-cache-dir '.[server,postgres]' && useradd --create-home maestro

USER maestro
ENV MAESTRO_DB=/data/maestro.db
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["maestro", "serve", "--host", "0.0.0.0", "--port", "8000"]
