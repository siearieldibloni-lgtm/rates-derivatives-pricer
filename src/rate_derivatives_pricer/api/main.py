"""FastAPI application factory.

Run locally with ``uv run uvicorn rate_derivatives_pricer.api.main:app --reload``.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

import rate_derivatives_pricer
from rate_derivatives_pricer.api.errors import register_error_handlers
from rate_derivatives_pricer.api.logging_config import configure_logging
from rate_derivatives_pricer.api.middleware import RequestContextMiddleware
from rate_derivatives_pricer.api.routes import router
from rate_derivatives_pricer.api.settings import Settings
from rate_derivatives_pricer.core.market.providers import FileMarketDataProvider
from rate_derivatives_pricer.services.curve_cache import LruCurveCache
from rate_derivatives_pricer.services.pricer_service import PricerService

logger = logging.getLogger(__name__)

API_TITLE = "Rate Derivatives Pricer"


def create_app(
    settings: Settings | None = None,
    service: PricerService | None = None,
    configure_logs: bool = True,
) -> FastAPI:
    """Build the application.

    Args:
        settings: Configuration; read from the environment when ``None``.
        service: Pricing service; built from ``settings`` when ``None`` (injectable in tests).
        configure_logs: Install the structured log handler (tests may keep their own).
    """
    settings = settings or Settings()
    if configure_logs:
        configure_logging(settings.log_level, settings.log_format)
    if service is None:
        service = PricerService(
            provider=FileMarketDataProvider(settings.market_data_dir),
            cache=LruCurveCache(settings.curve_cache_size, settings.curve_cache_ttl_seconds),
        )

    app = FastAPI(
        title=API_TITLE,
        version=rate_derivatives_pricer.__version__,
        description="Stateless pricing service for interest rate derivatives (EUR ESTR OIS).",
    )
    app.state.settings = settings
    app.state.service = service
    register_error_handlers(app)
    app.include_router(router)
    app.add_middleware(RequestContextMiddleware)
    logger.info(
        "api_started",
        extra={
            "version": rate_derivatives_pricer.__version__,
            "market_data_dir": str(settings.market_data_dir),
            "curve_cache_size": settings.curve_cache_size,
        },
    )
    return app


app = create_app()
