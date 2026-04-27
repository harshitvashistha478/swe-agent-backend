from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import List


class Settings(BaseSettings):
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str

    REPOS_BASE_PATH: str = "/var/lib/repomind/repos"
    ALLOWED_ORIGINS: List[str] = ["http://localhost:3000"]

    # Groq LLM
    GROQ_API_KEY: str = ""

    # Neo4j — Graph Knowledge Base
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "repomind_graph"

    @field_validator("SECRET_KEY")
    @classmethod
    def secret_key_must_be_strong(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError(
                "SECRET_KEY must be at least 32 characters. "
                "Generate one with: openssl rand -hex 64"
            )
        if v.lower() in ("supersecretkey", "secret", "changeme", "password"):
            raise ValueError("SECRET_KEY is set to an insecure default — change it.")
        return v

    @field_validator("ALLOWED_ORIGINS", mode="before")
    @classmethod
    def parse_origins(cls, v):
        if isinstance(v, str):
            # Handle both comma-separated strings and JSON arrays
            v = v.strip()
            if v.startswith("["):
                import json
                return json.loads(v)
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    model_config = {"env_file": ".env"}


settings = Settings()
