"""FastAPI application initialization, middleware configuration, and lifespan setup."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.alerting import router as alerting_router
from src.api.analytics import router as analytics_router, support_router as support_analytics_router
from src.api.contracts import router as contracts_router
from src.api.dashboard import router as dashboard_router
from src.api.deals import router as deals_router
from src.api.financial import router as financial_router
from src.api.knowledge import router as knowledge_router
from src.api.marketing import router as marketing_router
from src.api.tickets import router as tickets_router
from src.api.v1.endpoints.auth import router as auth_router
from src.api.v1.endpoints.navigation import router as navigation_router
from src.lib.config import settings
from src.lib.logging import CorrelationIdMiddleware, setup_logging
import src.models.alerting  # noqa: F401
import src.models.analytics  # noqa: F401
import src.models.audit  # noqa: F401
import src.models.contract  # noqa: F401
import src.models.dashboard  # noqa: F401
from src.models.database import Base, engine
import src.models.deal  # noqa: F401
import src.models.financial  # noqa: F401
import src.models.forecast  # noqa: F401
import src.models.knowledge  # noqa: F401
import src.models.marketing  # noqa: F401
import src.models.role  # noqa: F401
import src.models.tenant  # noqa: F401
import src.models.ticket  # noqa: F401
import src.models.user  # noqa: F401


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Lifespan context manager for application startup and shutdown events.

    Initializes structured logging and creates all database tables if missing.
    """
    setup_logging(settings.LOG_LEVEL)
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title=settings.APP_NAME,
    debug=settings.DEBUG,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url=f"{settings.API_V1_STR}/docs",
    redoc_url=f"{settings.API_V1_STR}/redoc",
    lifespan=lifespan,
)

# Register request correlation ID middleware
app.add_middleware(CorrelationIdMiddleware)

# Register Cross-Origin Resource Sharing (CORS) middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API endpoints
app.include_router(auth_router, prefix=settings.API_V1_STR)
app.include_router(navigation_router, prefix=settings.API_V1_STR)
app.include_router(dashboard_router, prefix=settings.API_V1_STR)
app.include_router(contracts_router, prefix=settings.API_V1_STR)
app.include_router(deals_router, prefix=settings.API_V1_STR)
app.include_router(financial_router, prefix=settings.API_V1_STR)
app.include_router(alerting_router, prefix=settings.API_V1_STR)
app.include_router(analytics_router, prefix=settings.API_V1_STR)
app.include_router(support_analytics_router, prefix=settings.API_V1_STR)
app.include_router(marketing_router, prefix=settings.API_V1_STR)
app.include_router(tickets_router, prefix=settings.API_V1_STR)
app.include_router(knowledge_router, prefix=settings.API_V1_STR)


@app.get("/health", status_code=200, tags=["health"])
def health_check() -> dict[str, str]:
    """Basic health check endpoint returning application status."""
    return {"status": "ok", "environment": settings.ENVIRONMENT}


@app.get(f"{settings.API_V1_STR}/health", status_code=200, tags=["health"])
def api_v1_health_check() -> dict[str, str]:
    """API v1 health check endpoint."""
    return {"status": "ok", "environment": settings.ENVIRONMENT}
