"""Pydantic Settings: runtime config from environment variables and ``.env``.

Defaults for the four URI namespaces match the committed identifiers listed
in ``CLAUDE.md``. Overriding them is meant for local development only and
must be coordinated with the National Library of Finland before any
production publish.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # URI namespaces (committed; do not change without surfacing the decision).
    work_namespace: str = Field(
        default="http://urn.fi/URN:NBN:fi:bib:work:",
        alias="BFFI_WORK_NAMESPACE",
    )
    expression_namespace: str = Field(
        default="http://urn.fi/URN:NBN:fi:bib:expression:",
        alias="BFFI_EXPRESSION_NAMESPACE",
    )
    helmet_source_uri: str = Field(
        default="http://urn.fi/URN:NBN:fi:bib:source:helmet",
        alias="BFFI_HELMET_SOURCE_URI",
    )
    graph_base: str = Field(
        default="http://urn.fi/URN:NBN:fi:bib:graph:",
        alias="BFFI_GRAPH_BASE",
    )

    # Local LLM endpoint.
    llm_base_url: str = Field(
        default="http://localhost:11434/v1",
        alias="LLM_BASE_URL",
    )
    llm_api_key: str = Field(default="ollama", alias="LLM_API_KEY")
    llm_model_primary: str = Field(
        default="qwen3:32b-q4_K_M",
        alias="LLM_MODEL_PRIMARY",
    )
    llm_model_fallback: str = Field(
        default="qwen2.5:72b-instruct-q4_K_M",
        alias="LLM_MODEL_FALLBACK",
    )

    # Triple store.
    fuseki_url: str = Field(
        default="http://localhost:3030/bffi",
        alias="FUSEKI_URL",
    )
    # Fuseki credentials are optional. The Apache Jena Fuseki 5 image
    # protects the Graph Store Protocol (`/<dataset>/data`) endpoints
    # behind admin auth by default; M10 phase 2 needs them set when
    # the dataset is locked down. Empty defaults preserve the existing
    # anonymous-access behavior for instances configured that way.
    fuseki_user: str = Field(default="", alias="FUSEKI_USER")
    fuseki_password: str = Field(default="", alias="FUSEKI_PASSWORD")

    # Filesystem layout.
    data_dir: Path = Field(default=Path("./data"), alias="BFFI_DATA_DIR")
    logs_dir: Path = Field(default=Path("./logs"), alias="BFFI_LOGS_DIR")
    eval_dir: Path = Field(default=Path("./eval-runs"), alias="BFFI_EVAL_DIR")
    config_dir: Path = Field(default=Path("./config"), alias="BFFI_CONFIG_DIR")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide :class:`Settings` singleton (lazy, cached)."""
    return Settings()
