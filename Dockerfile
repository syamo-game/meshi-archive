FROM python:3.11-slim AS app

WORKDIR /app

COPY requirements.txt constraints.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==83.0.0 \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY . .

FROM node:24-bookworm-slim AS node

FROM app AS test
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
RUN node -e "for (const name of ['File', 'FormData', 'Response', 'Headers']) { if (typeof globalThis[name] !== 'function') throw new Error(name + ' is required'); }" \
    && pip install --no-cache-dir -r requirements-dev.txt

FROM app AS runtime
ENV APP_ENV=production
