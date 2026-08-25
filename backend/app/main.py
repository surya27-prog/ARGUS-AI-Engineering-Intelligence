from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat, cochange, debt, graph, health, impact, repos, risk, search
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.graph import close_driver

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    yield
    # The Neo4j driver holds a connection pool; Postgres' is torn down by
    # SQLAlchemy's own atexit handling.
    close_driver()


app = FastAPI(
    title="ARGUS API",
    description="AI Engineering Intelligence Platform",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registered before the routers so a failure inside any of them is caught.
install_error_handlers(app)

app.include_router(health.router)
app.include_router(repos.router)
app.include_router(graph.router)
app.include_router(search.router)
app.include_router(chat.router)
app.include_router(impact.router)
app.include_router(cochange.router)
app.include_router(risk.router)
app.include_router(debt.router)


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "ARGUS API", "docs": "/docs"}
