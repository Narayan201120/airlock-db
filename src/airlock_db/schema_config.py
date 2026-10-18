"""Load client schema layouts and compile them into Pydantic v2 models."""

from __future__ import annotations

import hashlib
import json
import keyword
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model

from .transforms import TransformConfigError, TransformRule, parse_transforms


_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_FIELDS = 256
_MODEL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
_BASE_MODEL_NAMES = frozenset(dir(BaseModel)) | frozenset(
    {"__root__", "migrated_at", "airlock_record_number"}
)
_RESERVED_FIELD_NAMES = frozenset(name.casefold() for name in _BASE_MODEL_NAMES)
_ALLOWED_ROOT_KEYS = frozenset(
    {"version", "model_name", "id_field", "strict", "strip_whitespace", "fields"}
)
_ALLOWED_FIELD_KEYS = frozenset(
    {
        "type",
        "required",
        "strict",
        "min_length",
        "max_length",
        "pattern",
        "gt",
        "ge",
        "lt",
        "le",
        "max_digits",
        "decimal_places",
        "values",
        "anonymize",
        "transforms",
    }
)
_TYPE_ANNOTATIONS: dict[str, type[Any]] = {
    "string": str,
    "integer": int,
    "number": float,
    "decimal": Decimal,
    "boolean": bool,
    "uuid": UUID,
    "datetime": datetime,
    "date": date,
}
_NUMERIC_TYPES = frozenset({"integer", "number", "decimal"})


class SchemaConfigError(ValueError):
    """Raised when an external schema layout is invalid or unreadable."""


def valid_model_name(name: object) -> bool:
    """Return True when ``name`` can become a dynamic model class name."""

    return isinstance(name, str) and _MODEL_NAME.fullmatch(name) is not None


def valid_field_name(name: object) -> bool:
    """Return True when ``name`` can become a dynamic schema field."""

    return (
        isinstance(name, str)
        and _FIELD_NAME.fullmatch(name) is not None
        and not keyword.iskeyword(name)
        and name.casefold() not in _RESERVED_FIELD_NAMES
    )


@dataclass(frozen=True, slots=True)
class DynamicField:
    """Validated field metadata used to compile and persist one model field."""

    name: str
    type_name: str
    required: bool
    anonymize: bool
    sql_type: str
    enum_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DynamicSchema:
    """A compiled Pydantic model plus the layout metadata needed by storage."""

    model_name: str
    model: type[BaseModel]
    fields: tuple[DynamicField, ...]
    id_field: str
    fingerprint: str
    transforms: Mapping[str, tuple[TransformRule, ...]] = field(default_factory=dict)

    @property
    def field_names(self) -> frozenset[str]:
        return frozenset(field.name for field in self.fields)

    @property
    def table_name(self) -> str:
        return f"dynamic_records_{self.fingerprint}"

    @property
    def anonymized_fields(self) -> frozenset[str]:
        return frozenset(field.name for field in self.fields if field.anonymize)

    @classmethod
    def from_file(cls, path: str | Path) -> DynamicSchema:
        """Load JSON or YAML and compile the configured strict model."""

        source = Path(path)
        try:
            with source.open("rb") as stream:
                content = stream.read(_MAX_CONFIG_BYTES + 1)
        except OSError as exc:
            raise SchemaConfigError(f"cannot read schema config {source}: {exc}") from exc
        if len(content) > _MAX_CONFIG_BYTES:
            raise SchemaConfigError(
                f"schema config exceeds the {_MAX_CONFIG_BYTES}-byte limit"
            )
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError as exc:
            raise SchemaConfigError(f"schema config must be UTF-8: {exc}") from exc

        suffix = source.suffix.casefold()
        try:
            if suffix == ".json":
                raw = json.loads(
                    text,
                    object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant,
                )
            elif suffix in {".yaml", ".yml"}:
                raw = _load_unique_key_yaml(text, str(source))
            else:
                raise SchemaConfigError("schema config file must end in .json, .yaml, or .yml")
        except SchemaConfigError:
            raise
        except (json.JSONDecodeError, RecursionError) as exc:
            raise SchemaConfigError(f"invalid schema config {source}: {exc}") from exc

        try:
            return _compile_schema(raw)
        except SchemaConfigError:
            raise
        except Exception as exc:
            raise SchemaConfigError(f"invalid schema config {source}: {exc}") from exc


def _reject_json_constant(value: str) -> Any:
    raise SchemaConfigError(f"non-standard JSON constant is not supported: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaConfigError(f"duplicate schema config key: {key}")
        result[key] = value
    return result


def _load_unique_key_yaml(text: str, label: str) -> Any:
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
                raise SchemaConfigError(
                    "YAML aliases are not supported in schema configs"
                )
            return super().compose_node(parent, index)

        def construct_mapping(
            self, node: MappingNode, deep: bool = False
        ) -> dict[str, Any]:
            if not isinstance(node, MappingNode):
                raise SchemaConfigError("schema config mappings must use string keys")
            result: dict[str, Any] = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str):
                    raise SchemaConfigError("schema config mapping keys must be strings")
                if key in result:
                    raise SchemaConfigError(f"duplicate schema config key: {key}")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    _UniqueKeySafeLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        _UniqueKeySafeLoader.construct_mapping,
    )
    try:
        return yaml.load(text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise SchemaConfigError(f"invalid YAML schema config {label}: {exc}") from exc


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaConfigError(f"{label} must be true or false")
    return value


def _positive_integer(value: Any, label: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaConfigError(f"{label} must be an integer")
    if value < (0 if allow_zero else 1):
        qualifier = "zero or greater" if allow_zero else "greater than zero"
        raise SchemaConfigError(f"{label} must be {qualifier}")
    return value


def _number(value: Any, type_name: str, label: str) -> int | float | Decimal:
    if isinstance(value, bool):
        raise SchemaConfigError(f"{label} must be numeric")
    try:
        if type_name == "integer":
            if not isinstance(value, int):
                raise SchemaConfigError(f"{label} must be an integer")
            return value
        if type_name == "decimal":
            if not isinstance(value, (str, int, float, Decimal)):
                raise SchemaConfigError(f"{label} must be numeric")
            parsed = Decimal(str(value))
            if not parsed.is_finite():
                raise SchemaConfigError(f"{label} must be finite")
            return parsed
        if not isinstance(value, (int, float)):
            raise SchemaConfigError(f"{label} must be numeric")
        parsed_float = float(value)
        if not math.isfinite(parsed_float):
            raise SchemaConfigError(f"{label} must be finite")
        return parsed_float
    except (InvalidOperation, ValueError) as exc:
        if isinstance(exc, SchemaConfigError):
            raise
        raise SchemaConfigError(f"{label} must be numeric") from exc


def _compile_schema(raw: Any) -> DynamicSchema:
    if not isinstance(raw, dict):
        raise SchemaConfigError("schema config root must be an object")
    unknown_root = set(raw) - _ALLOWED_ROOT_KEYS
    if unknown_root:
        raise SchemaConfigError(
            "unknown schema config key(s): " + ", ".join(sorted(unknown_root))
        )
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise SchemaConfigError("schema config version must be the integer 1")

    model_name = raw.get("model_name")
    if not valid_model_name(model_name):
        raise SchemaConfigError("model_name must be a simple Python class name")
    strict = _require_bool(raw.get("strict", False), "strict")
    strip_whitespace = _require_bool(
        raw.get("strip_whitespace", True), "strip_whitespace"
    )

    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise SchemaConfigError("fields must be a non-empty object")
    if len(raw_fields) > _MAX_FIELDS:
        raise SchemaConfigError(f"schema config may define at most {_MAX_FIELDS} fields")

    field_definitions: dict[str, tuple[Any, Any]] = {}
    dynamic_fields: list[DynamicField] = []
    normalized_fields: dict[str, dict[str, Any]] = {}
    transforms_by_field: dict[str, tuple[TransformRule, ...]] = {}
    seen_names: set[str] = set()
    for name, spec in raw_fields.items():
        if not valid_field_name(name):
            raise SchemaConfigError(f"invalid or reserved schema field name: {name!r}")
        folded_name = name.casefold()
        if folded_name in seen_names:
            raise SchemaConfigError(f"field name is duplicated ignoring case: {name}")
        seen_names.add(folded_name)
        if not isinstance(spec, dict):
            raise SchemaConfigError(f"field {name} must be an object")
        unknown = set(spec) - _ALLOWED_FIELD_KEYS
        if unknown:
            raise SchemaConfigError(
                f"unknown option(s) for field {name}: " + ", ".join(sorted(unknown))
            )

        type_name = spec.get("type")
        if not isinstance(type_name, str):
            raise SchemaConfigError(f"type for field {name} must be a string")
        if type_name == "enum":
            annotation: Any = None
        elif type_name in _TYPE_ANNOTATIONS:
            annotation = _TYPE_ANNOTATIONS[type_name]
        else:
            supported = ", ".join((*_TYPE_ANNOTATIONS, "enum"))
            raise SchemaConfigError(
                f"unsupported type for field {name}: {type_name!r}; choose from {supported}"
            )

        required = _require_bool(spec.get("required", True), f"{name}.required")
        field_strict = _require_bool(spec.get("strict", strict), f"{name}.strict")
        anonymize = _require_bool(spec.get("anonymize", False), f"{name}.anonymize")
        field_options: dict[str, Any] = {"strict": field_strict}
        normalized: dict[str, Any] = {
            "type": type_name,
            "required": required,
            "strict": field_strict,
            "anonymize": anonymize,
        }

        values: tuple[str, ...] = ()
        if type_name == "enum":
            raw_values = spec.get("values")
            if (
                not isinstance(raw_values, list)
                or not raw_values
                or any(not isinstance(value, str) or not value for value in raw_values)
            ):
                raise SchemaConfigError(f"{name}.values must be a non-empty list of strings")
            if len(set(raw_values)) != len(raw_values):
                raise SchemaConfigError(f"{name}.values contains duplicate values")
            values = tuple(raw_values)
            enum_type = Enum(
                f"{model_name}{name.title()}Value",
                {f"VALUE_{index}": value for index, value in enumerate(values)},
                type=str,
            )
            annotation = enum_type
            normalized["values"] = list(values)
        elif "values" in spec:
            raise SchemaConfigError(f"{name}.values is only valid for enum fields")

        if type_name == "string":
            for option in ("min_length", "max_length"):
                if option in spec:
                    value = _positive_integer(
                        spec[option], f"{name}.{option}", allow_zero=True
                    )
                    field_options[option] = value
                    normalized[option] = value
            if (
                "min_length" in field_options
                and "max_length" in field_options
                and field_options["min_length"] > field_options["max_length"]
            ):
                raise SchemaConfigError(f"{name}.min_length must not exceed max_length")
            if "pattern" in spec:
                pattern = spec["pattern"]
                if not isinstance(pattern, str):
                    raise SchemaConfigError(f"{name}.pattern must be a string")
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise SchemaConfigError(f"{name}.pattern is invalid: {exc}") from exc
                field_options["pattern"] = pattern
                normalized["pattern"] = pattern
        elif any(option in spec for option in ("min_length", "max_length", "pattern")):
            raise SchemaConfigError(
                f"length and pattern constraints are only valid for string fields ({name})"
            )

        if type_name in _NUMERIC_TYPES:
            for option in ("gt", "ge", "lt", "le"):
                if option in spec:
                    bound = _number(spec[option], type_name, f"{name}.{option}")
                    field_options[option] = bound
                    normalized[option] = str(bound) if isinstance(bound, Decimal) else bound
            if "gt" in field_options and "ge" in field_options:
                raise SchemaConfigError(f"{name} cannot define both gt and ge")
            if "lt" in field_options and "le" in field_options:
                raise SchemaConfigError(f"{name} cannot define both lt and le")
            lower = field_options.get("gt", field_options.get("ge"))
            upper = field_options.get("lt", field_options.get("le"))
            if lower is not None and upper is not None and lower >= upper:
                raise SchemaConfigError(f"{name} lower bound must be below its upper bound")
            if type_name == "decimal":
                for option in ("max_digits", "decimal_places"):
                    if option in spec:
                        value = _positive_integer(
                            spec[option], f"{name}.{option}", allow_zero=(option == "decimal_places")
                        )
                        field_options[option] = value
                        normalized[option] = value
                if (
                    "max_digits" in field_options
                    and "decimal_places" in field_options
                    and field_options["decimal_places"] > field_options["max_digits"]
                ):
                    raise SchemaConfigError(
                        f"{name}.decimal_places must not exceed max_digits"
                    )
            elif "max_digits" in spec or "decimal_places" in spec:
                raise SchemaConfigError(
                    f"max_digits and decimal_places are only valid for decimal fields ({name})"
                )
        elif any(option in spec for option in ("gt", "ge", "lt", "le", "max_digits", "decimal_places")):
            raise SchemaConfigError(f"numeric constraints are not valid for {name}")

        field_transforms: tuple[TransformRule, ...] = ()
        if "transforms" in spec:
            try:
                field_transforms = parse_transforms(
                    spec["transforms"],
                    field=name,
                    type_name=type_name,
                    enum_values=values,
                )
            except TransformConfigError as exc:
                raise SchemaConfigError(str(exc)) from exc
            normalized["transforms"] = [
                rule.canonical() for rule in field_transforms
            ]
            transforms_by_field[name] = field_transforms

        if anonymize:
            if type_name != "string" or "pattern" in spec or values:
                raise SchemaConfigError(
                    f"{name}.anonymize requires a string field without pattern or choices"
                )
            min_length = field_options.get("min_length", 0)
            max_length = field_options.get("max_length", 32)
            if min_length > 32 or max_length < 32:
                raise SchemaConfigError(
                    f"{name}.anonymize needs length constraints that accept a 32-character token"
                )

        if type_name == "enum":
            annotation = enum_type
        if not required:
            annotation = Union[annotation, None]
        default = ... if required else None
        try:
            field_definitions[name] = (annotation, Field(default, **field_options))
        except Exception as exc:
            raise SchemaConfigError(f"invalid constraints for field {name}: {exc}") from exc

        sql_type = {
            "string": "TEXT",
            "enum": "TEXT",
            "uuid": "TEXT",
            "date": "TEXT",
            "datetime": "TEXT",
            "decimal": "TEXT",
            "integer": "INTEGER",
            "boolean": "INTEGER",
            "number": "REAL",
        }[type_name]
        dynamic_fields.append(
            DynamicField(name, type_name, required, anonymize, sql_type, values)
        )
        normalized_fields[name] = normalized

    id_field = raw.get("id_field")
    if not isinstance(id_field, str) or id_field not in {field.name for field in dynamic_fields}:
        raise SchemaConfigError("id_field must name one of the configured fields")
    identity = next(field for field in dynamic_fields if field.name == id_field)
    if not identity.required or identity.anonymize:
        raise SchemaConfigError("id_field must be required and cannot be anonymized")

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=strip_whitespace,
        strict=strict,
        allow_inf_nan=False,
    )
    try:
        model = create_model(model_name, __config__=model_config, **field_definitions)
    except Exception as exc:
        raise SchemaConfigError(f"could not build the configured Pydantic model: {exc}") from exc

    canonical = {
        "version": 1,
        "model_name": model_name,
        "id_field": id_field,
        "strict": strict,
        "strip_whitespace": strip_whitespace,
        "fields": list(normalized_fields.items()),
    }
    fingerprint = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    return DynamicSchema(
        model_name=model_name,
        model=model,
        fields=tuple(dynamic_fields),
        id_field=id_field,
        fingerprint=fingerprint,
        transforms=transforms_by_field,
    )
