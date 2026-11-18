"""Application entry point: one FastAPI app that hosts the Leasing and/or Cheque service.

    python -m app                      # production / Windows service (host, port, services from .env)
    python -m app leasing              # only the Leasing service
    python -m app cheque               # only the Cheque service
    uvicorn app.main:app --reload      # local development, whatever SERVICES says

Both services share one process when both are switched on (SERVICES=leasing,cheque, the default), and
every URL stays exactly what it was in the two separate projects. To isolate them, run the same code
twice with SERVICES=leasing and SERVICES=cheque on different ports.
"""

from app.factory import build_app

app = build_app()
