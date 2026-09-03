"""Console entry point for NOVA.

``pyproject.toml`` declares ``nova = "nova.main:run"``, so this module is
what the ``nova`` command resolves to after an editable install.

Startup order matters: settings are validated first so a bad configuration
fails before anything is created, then directories, then logging, then the
server. Logging is configured before uvicorn starts so its output flows
through the same structured pipeline.
"""

from __future__ import annotations

import uvicorn

from nova import __version__
from nova.api import create_app
from nova.config import get_settings
from nova.utils import configure_logging, get_logger


def run() -> None:
    """Validate configuration and serve the NOVA API."""
    settings = get_settings()
    settings.ensure_directories()
    configure_logging(settings.logging)

    logger = get_logger("nova.main")
    logger.info(
        "nova_starting",
        version=__version__,
        environment=settings.environment,
        host=settings.server.host,
        port=settings.server.port,
    )

    uvicorn.run(
        create_app(settings),
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,
    )


if __name__ == "__main__":
    run()