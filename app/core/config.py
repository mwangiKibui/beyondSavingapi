from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str
    redis_url: str
    # Any S3-compatible store: RustFS locally (docker-compose `storage`),
    # DigitalOcean Spaces in test/prod (e.g. https://fra1.digitaloceanspaces.com,
    # region fra1). Switching is env-only - see README "Object storage" (ab-263).
    storage_endpoint: str = "http://localhost:9000"
    storage_region: str = "us-east-1"
    storage_access_key: str
    storage_secret_key: str
    storage_bucket: str = "statements"
    # ab-313: load app/parsers/profiles/builtin into statement_profiles at
    # startup. Off in the test suite.
    seed_builtin_profiles: bool = True
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    # Keys the HMAC that hashes private category-suggestion keys (account
    # references, people on transfers) before they're stored (ab-253).
    # NEVER change it once set in an environment: every stored hash would
    # stop matching and users would lose their remembered categories.
    suggestion_key_secret: str
    jwt_expires_minutes: int = 60 * 24
    # Impersonation tokens are short-lived - bounds the blast radius of a
    # leaked/copy-pasted one; cheap to re-impersonate if it expires
    # mid-session since the admin's own session is untouched.
    jwt_impersonation_expires_minutes: int = 30
    # Public URL of beyondSavingUI, used to construct links (e.g. the
    # password reset link logged/emailed to a user).
    frontend_url: str = "http://localhost:3000"
    # Comma-separated list of origins allowed to call this API from a browser.
    cors_origins: str = "http://localhost:3000"
    # ab-314: DigitalOcean Serverless Inference for the AI layout proposer.
    # An empty key turns the AI step off (uploads go straight to Columns).
    # The key is made in the DigitalOcean control panel, limited to the model.
    do_inference_key: str = ""
    do_inference_url: str = "https://inference.do-ai.run/v1"
    do_inference_model: str = "openai-gpt-oss-120b"
    do_inference_timeout_seconds: float = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
