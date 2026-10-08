FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

# Wheels are mounted from BuildKit's cache instead of re-downloaded. `COPY app ./app` sits above
# this line because the package is installed from its own metadata, so every source edit rebuilds
# it - and a rebuild that fetched the whole dependency set again took minutes per iteration.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && pip install .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000"]
