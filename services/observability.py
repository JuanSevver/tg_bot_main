"""
Минимальный health-check + metrics endpoint.

Без этого Docker не понимает, жив ли бот «по-настоящему»: процесс может
крутиться в FloodWait-цикле или у Telethon отвалились все аккаунты, а
контейнер не рестартится. /healthz даёт сигнал для HEALTHCHECK, а
/metrics — простой plain-text дашборд для глаз (или Prometheus pull,
формат совместим).
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

# Счётчики метрик. Простые int-ы, без histogramm — для нашего объёма достаточно.
_counters: dict[str, int] = defaultdict(int)
_gauges: dict[str, float] = {}
_started_at = time.time()


def inc(name: str, value: int = 1) -> None:
    _counters[name] += value


def gauge(name: str, value: float) -> None:
    _gauges[name] = value


async def _healthz(_request: web.Request) -> web.Response:
    """Liveness probe. 200 = процесс отвечает, цикл работает."""
    return web.json_response({"status": "ok", "uptime_seconds": int(time.time() - _started_at)})


async def _metrics(_request: web.Request) -> web.Response:
    """Plain-text expose в формате Prometheus."""
    lines = [f"# HELP uptime Seconds since process start", "# TYPE uptime gauge",
             f"uptime {int(time.time() - _started_at)}"]
    for name, value in _counters.items():
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")
    for name, value in _gauges.items():
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


async def start_http_server(host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    """Поднимает aiohttp на отдельном порту. Возвращает runner для shutdown."""
    app = web.Application()
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/metrics", _metrics)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logger.info("Observability HTTP server listening on %s:%d", host, port)
    return runner
