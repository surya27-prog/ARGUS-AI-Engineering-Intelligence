from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.graph import check_connectivity
from app.core.vectors import check_connectivity as check_qdrant

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str
    postgres: str
    neo4j: str
    qdrant: str
    # Which providers are selected and whether they have credentials. Reported
    # from config rather than by calling them: a liveness probe should not make
    # a billable request every time it runs.
    llm_provider: str
    embedding_provider: str
    llm_configured: bool
    embedding_configured: bool


@router.get("/health", response_model=HealthResponse)
def health(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    """Liveness check. Reports store connectivity but stays 200 either way,
    so the endpoint is usable as a container liveness probe."""
    try:
        db.execute(text("SELECT 1"))
        postgres = "up"
    except Exception as exc:  # noqa: BLE001 - surfaced in the payload, not raised
        postgres = f"down: {type(exc).__name__}"

    return HealthResponse(
        status="ok",
        environment=settings.app_env,
        version="0.1.0",
        postgres=postgres,
        neo4j=check_connectivity(),
        qdrant=check_qdrant(),
        llm_provider=settings.llm_provider,
        embedding_provider=settings.embedding_provider,
        llm_configured=settings.has_chat_credentials,
        embedding_configured=settings.has_embedding_credentials,
    )
