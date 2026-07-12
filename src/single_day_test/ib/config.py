from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..domain.errors import InputValidationError


@dataclass(frozen=True)
class IbConfig:
    host: str
    port: int
    client_id: int
    connect_timeout: float

    @classmethod
    def from_yaml(cls, path: str | Path, environment: str) -> "IbConfig":
        try:
            document: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            profile: Any = document[environment]
            return cls(str(profile["host"]), int(profile["port"]), int(profile["client_id"]), float(profile["connect_timeout"]))
        except (OSError, TypeError, KeyError, ValueError, yaml.YAMLError) as exc:
            raise InputValidationError(f"Invalid IB YAML profile {environment!r} in {path}") from exc
