"""Orquestador de tareas asyncio en background.

Loops activos:
- universe_scanner   — refresca tabla markets_active cada N min
- poller             — descarga snapshots cada 30s y dispara orchestrator
- exit_loop          — evalúa exits sobre posiciones abiertas cada 60s
- equity_loop        — persiste un punto de la curva de equity real cada 60s
- outcomes_loop      — resuelve mercados vencidos cada 1h (habilita PnL/Brier)
- supervisor         — auto halt + flatten cuando DD se rompe
- fills_consumer     — solo en `mode=shadow`: lee `nocti:fills` (Fase 3)
"""

from __future__ import annotations

import asyncio

from umbra.bus.fills import FillConsumer
from umbra.config import settings
from umbra.logging import get_logger
from umbra.scheduler.equity_loop import equity_loop
from umbra.scheduler.exit_loop import exit_loop
from umbra.scheduler.learning_loop import learning_loop
from umbra.scheduler.ohlc_loop import ohlc_loop
from umbra.scheduler.outcomes_loop import outcomes_loop
from umbra.scheduler.poller import poller_loop
from umbra.scheduler.supervisor import supervisor_loop
from umbra.universe.scanner import scanner_loop

log = get_logger("umbra.background")


class BackgroundTasks:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        log.info("background.starting")
        self._tasks = [
            asyncio.create_task(scanner_loop(self._stop), name="universe_scanner"),
            asyncio.create_task(poller_loop(self._stop), name="poller"),
            asyncio.create_task(exit_loop(self._stop), name="exit_loop"),
            asyncio.create_task(equity_loop(self._stop), name="equity_loop"),
            asyncio.create_task(outcomes_loop(self._stop), name="outcomes_loop"),
            asyncio.create_task(supervisor_loop(self._stop), name="supervisor"),
            asyncio.create_task(ohlc_loop(self._stop), name="ohlc_loop"),
            asyncio.create_task(learning_loop(self._stop), name="learning_loop"),
        ]

        # El consumidor de fills solo tiene sentido si alguien produce intents, y
        # eso solo pasa en `shadow`. Arrancarlo siempre crearía el grupo `brain`
        # sobre `nocti:fills` en instalaciones que no usan el bus, y dejaría un
        # consumidor leyendo un stream que nadie escribe.
        if settings.mode == "shadow":
            self._tasks.append(
                asyncio.create_task(
                    FillConsumer().run(self._stop), name="fills_consumer"
                )
            )

        log.info("background.started", n=len(self._tasks))

    async def stop(self) -> None:
        log.info("background.stopping")
        self._stop.set()
        if not self._tasks:
            return
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        log.info("background.stopped")
