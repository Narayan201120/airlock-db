"""Shared strict reads for JSON and YAML configuration files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ConfigLoadError(ValueError):
    """Raised when a JSON or YAML config file cannot be loaded."""


def load_config_object(
    path: str | Path,
    *,
    label: str,
    max_bytes: int,
) -> Any:
    """Read a UTF-8 JSON or YAML file, rejecting duplicate keys and aliases.

    ``label`` names the config kind in messages, for example
    ``"schema config"`` or ``"fixed-width layout"``.
    """

    source = Path(path)
    try:
        with source.open("rb") as stream:
            content = stream.read(max_bytes + 1)
    except OSError as exc:
        raise ConfigLoadError(f"cannot read {label} {source}: {exc}") from exc
    if len(content) > max_bytes:
        raise ConfigLoadError(f"{label} exceeds the {max_bytes}-byte limit")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ConfigLoadError(f"{label} must be UTF-8: {exc}") from exc

    suffix = source.suffix.casefold()
    try:
        if suffix == ".json":
            return json.loads(
                text,
                object_pairs_hook=_unique_json_object(label),
                parse_constant=_reject_json_constant,
            )
        if suffix in {".yaml", ".yml"}:
            return _load_unique_key_yaml(text, str(source), label)
        raise ConfigLoadError(f"{label} file must end in .json, .yaml, or .yml")
    except ConfigLoadError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ConfigLoadError(f"invalid {label} {source}: {exc}") from exc


def _reject_json_constant(value: str) -> Any:
    raise ConfigLoadError(f"non-standard JSON constant is not supported: {value}")


def _unique_json_object(label: str):
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ConfigLoadError(f"duplicate {label} key: {key}")
            result[key] = value
        return result

    return hook


def _load_unique_key_yaml(text: str, source: str, label: str) -> Any:
    """Parse YAML with the safe loader, rejecting aliases and duplicate keys.

    PyYAML is imported here so a plain JSON run never pays its import cost.
    """

    import yaml
    from yaml.events import AliasEvent
    from yaml.nodes import MappingNode

    class _UniqueKeySafeLoader(yaml.SafeLoader):
        """Safe YAML loader that rejects ambiguous keys and aliases."""

        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(AliasEvent):
                raise ConfigLoadError(
                    f"YAML aliases are not supported in {label}s"
                )
            return super().compose_node(parent, index)

        def construct_mapping(
            self, node: MappingNode, deep: bool = False
        ) -> dict[str, Any]:
            if not isinstance(node, MappingNode):
                raise ConfigLoadError(f"{label} mappings must use string keys")
            result: dict[str, Any] = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str):
                    raise ConfigLoadError(
                        f"{label} mapping keys must be strings"
                    )
                if key in result:
                    raise ConfigLoadError(f"duplicate {label} key: {key}")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    _UniqueKeySafeLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        _UniqueKeySafeLoader.construct_mapping,
    )
    try:
        return yaml.load(text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"invalid YAML {label} {source}: {exc}") from exc
