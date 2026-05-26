# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Системные зависимости (нужны для python-levenshtein / cryptography)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        curl \
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

# Telethon у нас использует StringSession в БД — отдельная папка sessions/ не нужна.
# bot.db приходит через bind-mount из docker-compose.yml.

# Liveness probe: бьём в /healthz, поднятый services/observability.py.
# Без него Docker не понимает, жив ли бот «по-настоящему».
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

# --no-sync: venv уже подготовлен на предыдущем шаге, нечего пересинхронизировать
# при каждом старте. Раньше «Installed 6 packages in 100ms» сыпалось на каждом
# рестарте контейнера, что и медленнее, и менее предсказуемо.
CMD ["uv", "run", "--no-sync", "python", "main.py"]
