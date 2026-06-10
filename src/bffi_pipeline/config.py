"""Pipeline configuration.

Minimal pydantic-settings model carrying the paths the conversion
stages and the validation boundaries need at runtime. Settings can
be overridden by environment variables (`BFFI_*`) or by a `.env`
file at the repo root.

This module deliberately stays small — it's not the place for
stage-specific configuration. Stage runners take their own
parameters; settings only carry cross-stage paths and the URI
namespaces.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Cross-stage configuration for the BFFI conversion pipeline."""

    model_config = SettingsConfigDict(
        env_prefix="BFFI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    repo_root: Path = Field(default=_REPO_ROOT)
    data_dir: Path = Field(default=_REPO_ROOT / "data")
    runs_dir: Path = Field(default=_REPO_ROOT / "runs")

    vocab_dir: Path = Field(default=_REPO_ROOT / "vocab")
    sparql_dir: Path = Field(default=_REPO_ROOT / "sparql")
    config_dir: Path = Field(default=_REPO_ROOT / "config")

    work_namespace: str = "http://urn.fi/URN:NBN:fi:bib:work:"
    expression_namespace: str = "http://urn.fi/URN:NBN:fi:bib:expression:"
    manifestation_namespace: str = "http://urn.fi/URN:NBN:fi:bib:manifestation:"
    helmet_source_uri: str = "http://urn.fi/URN:NBN:fi:bib:source:helmet"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached :class:`Settings` instance."""
    return Settings()
