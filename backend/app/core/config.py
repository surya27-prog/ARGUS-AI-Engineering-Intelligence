from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from the repo-root .env file."""

    model_config = SettingsConfigDict(
        env_file="../.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:3000"

    # PostgreSQL
    database_url: str = "postgresql+psycopg://argus:argus_dev_password@localhost:5432/argus"
    # Seconds to wait for a Postgres connection before giving up.
    db_connect_timeout: int = 5

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "argus_dev_password"
    neo4j_database: str = "neo4j"
    # Seconds to wait for a Neo4j connection. The driver's default is 60s,
    # which would hang a request rather than report the graph as down.
    neo4j_timeout_seconds: int = 5

    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "argus_code_chunks"
    qdrant_timeout_seconds: int = 30

    # Parser
    workspace_dir: str = "./workspace"
    max_repo_size_mb: int = 500
    # `POST /repos` clones whatever URL it is given, so a private or
    # loopback address is refused by default — a server that fetches
    # arbitrary internal addresses on request can be used to read them.
    # Turn this on only when pointing ARGUS at a git server on your own
    # network.
    allow_private_git_hosts: bool = False
    parse_timeout_seconds: int = 900

    # Git history / co-change. `history_depth` is how many commits a clone
    # fetches and `history_max_commits` how many are read back; the clone is the
    # binding limit, so raising the second alone changes nothing.
    history_depth: int = 500
    history_max_commits: int = 500
    # A commit touching more parsed files than this is a refactor, not a
    # coupling signal — see parser.history.
    cochange_max_files_per_commit: int = 40
    cochange_min_commits: int = 2

    # LLM — chat and embeddings are selected independently. Anthropic has no
    # embeddings endpoint, so `llm_provider=anthropic` still needs one of the
    # embedding providers configured below.
    llm_provider: str = "anthropic"
    # Anthropic's cost/quality dial. It replaces `temperature`, which is not a
    # parameter on current models — sending it returns a 400.
    llm_effort: str = "high"
    llm_timeout_seconds: float = 120.0

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o"

    # Embeddings
    embedding_provider: str = "openai"
    openai_embedding_model: str = "text-embedding-3-small"
    # Must match the model above — Qdrant creates the collection with this
    # width, and a mismatch rejects every write rather than erroring at startup.
    embedding_dimensions: int = 1536

    @property
    def has_chat_credentials(self) -> bool:
        return self.llm_provider == "stub" or bool(self.anthropic_api_key)

    @property
    def has_embedding_credentials(self) -> bool:
        return self.embedding_provider == "hash" or bool(self.openai_api_key)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
