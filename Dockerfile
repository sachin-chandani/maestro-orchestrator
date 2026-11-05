# Build the React app (ADR 0014). Node stays in this stage; the final image only gets the built files.
FROM node:22-slim AS web
WORKDIR /src/web-app
COPY web-app/package.json web-app/package-lock.json ./
RUN npm ci
COPY web-app ./
RUN npm run build  # writes to /src/maestro/web/app

FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY maestro ./maestro
COPY --from=web /src/maestro/web/app ./maestro/web/app
# [postgres] so MAESTRO_DB_URL=postgresql://... works out of the box; SQLite is still the default.
RUN pip install --no-cache-dir '.[server,postgres]' && useradd --create-home maestro

USER maestro
ENV MAESTRO_DB=/data/maestro.db
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["maestro", "serve", "--host", "0.0.0.0", "--port", "8000"]
