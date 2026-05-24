# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Системные зависимости (нужны для python-levenshtein / cryptography)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Копируем только файлы зависимостей — слой кешируется пока они не меняются
COPY pyproject.toml uv.lock ./

# Устанавливаем зависимости без dev-пакетов
RUN uv sync --frozen --no-dev

# Копируем исходники
COPY . .

# Папки для персистентных данных (БД и Telethon-сессии)
RUN mkdir -p /data/sessions

CMD ["uv", "run", "python", "main.py"]
