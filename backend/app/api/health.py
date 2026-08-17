from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.graph import check_connectivity

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str
    postgres: str
    neo4j: str


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
    )
