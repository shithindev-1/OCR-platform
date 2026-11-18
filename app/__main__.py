"""Production entry point: `python -m app [leasing|cheque ...]`.

This is what a Windows service (NSSM) runs. Calling Uvicorn from Python rather than through
`uvicorn.exe` also sidesteps Windows Application Control policies that block unsigned launcher
executables inside a virtual environment.

Services default to SERVICES from .env (both). Name one or both on the command line to override, and
use --port to run a second copy of the same code for the other service.

One worker only: the Leasing rate limiter and per-request usage tracking are held in process memory.
"""

import argparse
import os

import uvicorn

from app.common.config import SERVICE_NAMES, PlatformSettings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument("services", nargs="*", choices=SERVICE_NAMES, help="services to run")
    parser.add_argument("--host", help="overrides APP_HOST")
    parser.add_argument("--port", type=int, help="overrides APP_PORT")
    args = parser.parse_args()
    if args.services:
        # Read by PlatformSettings when app.main builds the app.
        os.environ["SERVICES"] = ",".join(args.services)
    settings = PlatformSettings()
    uvicorn.run(
        "app.main:app",
        host=args.host or settings.app_host,
        port=args.port or settings.app_port,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        # The services write their own rotating files under logs/<service>/; Uvicorn's access log only
        # goes to stdout, which NSSM captures in logs/service-stdout.log.
        access_log=True,
    )


if __name__ == "__main__":
    main()
