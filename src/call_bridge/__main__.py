from __future__ import annotations

import logging

import uvicorn

from call_bridge.app import create_app
from call_bridge.config import get_settings


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if settings.bridge_mode == "live":
        settings.require_live_sip()
        settings.require_live_voice()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
