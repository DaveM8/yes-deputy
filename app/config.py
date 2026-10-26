import os
from pathlib import Path


def _load_env(path: str = ".env") -> None:
    """Minimal .env loader — no external dependency needed."""
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Only set if not already in environment (env vars take precedence)
        if key and key not in os.environ:
            os.environ[key] = value


_load_env()


class Settings:
    es_host: str
    es_user: str | None
    es_pass: str | None
    es_index: str
    openai_api_key: str
    anthropic_api_key: str | None
    default_model_key: str
    max_history_turns: int
    retrieval_k: int
    retrieval_num_candidates: int
    jwt_secret: str
    free_queries_per_day: int
    db_path: str
    # Email / admin portal
    smtp_host: str
    smtp_port: int
    smtp_user: str | None
    smtp_pass: str | None
    smtp_from: str
    admin_email: str | None
    site_url: str | None

    def __init__(self) -> None:
        self.es_host = os.environ.get("ES_HOST", "http://localhost:9200")
        self.es_user = os.environ.get("ES_USER")
        self.es_pass = os.environ.get("ES_PASS")
        self.es_index = os.environ.get("ES_INDEX", "oireachtas-chunks")
        self.openai_api_key = os.environ["OPENAI_API_KEY"]
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.default_model_key = os.environ.get("DEFAULT_MODEL_KEY", "claude-haiku")
        self.max_history_turns = int(os.environ.get("MAX_HISTORY_TURNS", "6"))
        self.retrieval_k = int(os.environ.get("RETRIEVAL_K", "8"))
        self.retrieval_num_candidates = int(os.environ.get("RETRIEVAL_NUM_CANDIDATES", "100"))
        self.jwt_secret = os.environ["JWT_SECRET"]
        self.free_queries_per_day = int(os.environ.get("FREE_QUERIES_PER_DAY", "10"))
        self.db_path = os.environ.get("DB_PATH", "data/users.db")
        # Email
        self.smtp_host = os.environ.get("SMTP_HOST", "")
        self.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        self.smtp_user = os.environ.get("SMTP_USER")
        self.smtp_pass = os.environ.get("SMTP_PASS")
        self.smtp_from = os.environ.get("SMTP_FROM", "noreply@yesdeputy.com")
        self.admin_email = os.environ.get("ADMIN_EMAIL")
        self.site_url = os.environ.get("SITE_URL")


settings = Settings()
