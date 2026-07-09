"""Entry point para arrancar la API en Windows con event loop compatible con psycopg async.

psycopg async no soporta ProactorEventLoop (default de Windows). Hay que setear el
policy ANTES de que uvicorn cree su loop — por eso esto es un script separado.
"""

import asyncio
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "umbra.api.app:app",
        host="127.0.0.1",
        port=int(os.environ.get("UMBRA_API_PORT", "8000")),
        reload=False,
        log_level="warning",
    )
