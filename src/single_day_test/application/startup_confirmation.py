"""Interactive launch-configuration confirmation shared by CLI entrypoints."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping


def print_and_confirm_launch(
    name: str,
    configuration: Mapping[str, object],
    input_reader: Callable[[str], str] | None = None,
) -> None:
    """Print validated effective configuration and wait for explicit approval."""
    print(f"{name} launch configuration:")
    print(json.dumps(configuration, default=str, indent=2, sort_keys=True))
    (input_reader or input)("Press Enter to start, or Ctrl+C to cancel: ")
