"""ORM models. Import every model here so Alembic autogenerate sees them."""

from app.models.repository import ParseStatus, Repository

__all__ = ["ParseStatus", "Repository"]
