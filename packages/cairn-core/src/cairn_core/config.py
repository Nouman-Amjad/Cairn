"""Configuration. One settings class per concern, composed per service.

Everything is env-driven. Nothing reads a file at import time except the
routing policy and the prompts, which are watched ConfigMaps and handled in
their own modules.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _Base(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class DatabaseSettings(_Base):
    model_config = SettingsConfigDict(env_prefix="CAIRN_DB_", extra="ignore")

    dsn: SecretStr = SecretStr("postgresql+asyncpg://cairn:cairn@localhost:5432/cairn")
    pool_size: int = 10
    max_overflow: int = 5
    pool_timeout_s: int = 10
    statement_timeout_ms: int = 15_000
    echo: bool = False


class RedisSettings(_Base):
    model_config = SettingsConfigDict(env_prefix="CAIRN_REDIS_", extra="ignore")

    url: str = "redis://localhost:6379/0"
    socket_timeout_s: float = 2.0


class S3Settings(_Base):
    model_config = SettingsConfigDict(env_prefix="CAIRN_S3_", extra="ignore")

    bucket: str = "cairn-artifacts"
    region: str = "eu-west-1"
    endpoint_url: str | None = None  # set for MinIO in local dev
    kms_key_id: str | None = None


class AuthSettings(_Base):
    model_config = SettingsConfigDict(env_prefix="CAIRN_AUTH_", extra="ignore")

    #: Trusts an `x-cairn-dev-user` header instead of an IdP. For local dev
    #: and the eval harness only; the gateway refuses to start with this on
    #: when CAIRN_ENV=prod.
    dev_mode: bool = False

    oidc_issuer: str = "https://example.okta.com/oauth2/default"
    oidc_audience: str = "cairn"
    jwks_url: str | None = None
    jwks_cache_s: int = 300

    #: Symmetric key for the short-lived internal JWT minted by the gateway.
    #: Rotated by External Secrets; both current and previous are accepted.
    internal_jwt_key: SecretStr = SecretStr("dev-only-insecure-key-padded-to-32-bytes")
    internal_jwt_key_previous: SecretStr | None = None
    internal_jwt_ttl_s: int = 300
    internal_jwt_issuer: str = "cairn-gateway"

    @field_validator("internal_jwt_key", "internal_jwt_key_previous")
    @classmethod
    def _hmac_key_long_enough(cls, v: SecretStr | None) -> SecretStr | None:
        # RFC 7518 3.2: an HMAC-SHA256 key shorter than the digest is a
        # downgrade. Enforced here so a short value from Secrets Manager
        # fails at boot rather than warning on every request.
        if v is not None and len(v.get_secret_value().encode()) < 32:
            raise ValueError("internal JWT key must be at least 32 bytes")
        return v


class TelemetrySettings(_Base):
    model_config = SettingsConfigDict(env_prefix="CAIRN_OTEL_", extra="ignore")

    service_name: str = "cairn"
    #: Unset by default: a service with no collector configured should run
    #: silently, not spend every request retrying an exporter. Helm sets it.
    endpoint: str | None = None
    sample_ratio: float = 1.0
    langfuse_host: str | None = None
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    log_level: str = "INFO"
    json_logs: bool = True


class RouterSettings(_Base):
    model_config = SettingsConfigDict(env_prefix="CAIRN_ROUTER_", extra="ignore")

    url: str = "http://cairn-router:8000"
    policy_path: str = "/etc/cairn/routing/policy.yaml"
    prices_path: str = "/etc/cairn/routing/prices.yaml"

    vllm_url: str = "http://vllm-server:8000/v1"
    vllm_model: str = "cairn-local"
    vllm_timeout_s: float = 120.0
    local_queue_depth_limit: int = 40

    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    cloud_model_strong: str = "claude-sonnet-4-5"
    cloud_model_cheap: str = "claude-haiku-4-5-20251001"
    cloud_timeout_s: float = 120.0
    cloud_error_rate_window_s: int = 300
    cloud_error_rate_trip: float = 0.10


class MCPSettings(_Base):
    model_config = SettingsConfigDict(env_prefix="CAIRN_MCP_", extra="ignore")

    observability_url: str = "http://cairn-mcp-observability:8000/mcp"
    runbooks_url: str = "http://cairn-mcp-runbooks:8000/mcp"
    actions_url: str = "http://cairn-mcp-actions:8000/mcp"
    timeout_s: float = 30.0
    #: Server-side cap on a single tool response. The whole context strategy
    #: rests on this being enforced by the tool, not the caller.
    max_response_tokens: int = 4_000


class BackendSettings(_Base):
    """The observability systems Cairn reads. All optional: an unset URL
    makes the corresponding tool return a clear 'not configured' error rather
    than a connection stack trace."""

    model_config = SettingsConfigDict(env_prefix="CAIRN_BACKEND_", extra="ignore")

    prometheus_url: str | None = None
    loki_url: str | None = None
    tempo_url: str | None = None
    kube_api_in_cluster: bool = True
    argocd_url: str | None = None
    argocd_token: SecretStr | None = None
    github_api_url: str = "https://api.github.com"
    github_org: str = "Nouman-Amjad"
    github_token: SecretStr | None = None
    jira_url: str | None = None
    jira_token: SecretStr | None = None
    jira_project: str = "OPS"
    query_timeout_s: float = 20.0


class EmbeddingSettings(_Base):
    """bge-m3 and its reranker, served next to vLLM on the same GPU.

    Co-located on purpose: embeddings then cost no extra hardware and never
    leave the cluster, which matters because runbooks and past trajectories
    are internal by default.
    """

    model_config = SettingsConfigDict(env_prefix="CAIRN_EMBED_", extra="ignore")

    url: str = "http://cairn-embeddings:8080"
    model: str = "BAAI/bge-m3"
    dims: int = 1024
    batch_size: int = 32
    timeout_s: float = 30.0
    reranker_url: str | None = None
    reranker_model: str = "BAAI/bge-reranker-v2-m3"


class ApprovalSettings(_Base):
    model_config = SettingsConfigDict(env_prefix="CAIRN_APPROVAL_", extra="ignore")

    url: str = "http://cairn-approval:8000"
    slack_bot_token: SecretStr | None = None
    slack_signing_secret: SecretStr | None = None
    slack_channel: str = "#cairn-approvals"
    default_ttl_s: int = 900
    signature_max_age_s: int = 300


class PolicySettings(_Base):
    model_config = SettingsConfigDict(env_prefix="CAIRN_POLICY_", extra="ignore")

    opa_url: str = "http://localhost:8181"
    decision_path: str = "cairn/tools/allow"
    timeout_s: float = 2.0
    #: When OPA is unreachable the tool servers deny. There is no fail-open
    #: mode and there is no flag to add one.
    enabled: bool = True


class AppSettings(_Base):
    model_config = SettingsConfigDict(env_prefix="CAIRN_", extra="ignore")

    env: str = "dev"
    ui_base_url: str = "http://localhost:3000"
    prompt_dir: str = "/etc/cairn/prompts"
    #: Per the cost model (§13.6). At Tier B's $0.094/query this is ~53
    #: queries a day, comfortably above any real on-call session.
    max_daily_cost_per_user_usd: float = 5.0
    rate_limit_per_minute: int = 20
    #: Global circuit breaker. Cloud inference is disabled for everyone
    #: once the day's spend passes `trip_ratio` of the forecast; queries
    #: keep working on the local tier, where marginal cost is zero.
    daily_cost_forecast_usd: float = 40.0
    cost_breaker_trip_ratio: float = 1.5
    port: int = 8000

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    s3: S3Settings = Field(default_factory=S3Settings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    otel: TelemetrySettings = Field(default_factory=TelemetrySettings)
    router: RouterSettings = Field(default_factory=RouterSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    backends: BackendSettings = Field(default_factory=BackendSettings)
    embed: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    approval: ApprovalSettings = Field(default_factory=ApprovalSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache(maxsize=1)
def settings() -> AppSettings:
    return AppSettings()
