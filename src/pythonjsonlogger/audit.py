"""Audit collector support: schema-registered allowlist formatting.

This module is built around a trust boundary: JSON log content arriving from an
integration is untrusted, while the :class:`SchemaRegistry` configuration is
trusted. Each registered :class:`EventSchema` declares an event type, a schema
version and the exact set of fields that may be retained for that event type.

Because field selection and the version marker come from the same registered
schema, the emitted ``schema_version`` can never disagree with the fields that
were actually retained, and a value supplied inside the log payload cannot
influence it.

Unknown (unregistered) event types are handled by an explicit policy:

- ``"drop"``: the event is discarded and nothing is emitted.
- ``"placeholder"``: a safe placeholder containing no payload fields (unless an
  explicit placeholder allowlist is configured) is emitted instead.
"""

### IMPORTS
### ============================================================================
## Future
from __future__ import annotations

## Standard Library
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

## Application
from .core import LogData
from .json import JsonFormatter


### CONSTANTS
### ============================================================================
EVENT_TYPE_FIELD: str = "event_type"
"""Key used to route an event to its registered schema."""

SCHEMA_VERSION_FIELD: str = "schema_version"
"""Key under which the registered schema version is emitted."""

POLICY_DROP: str = "drop"
"""Unknown event policy: discard the event."""

POLICY_PLACEHOLDER: str = "placeholder"
"""Unknown event policy: emit a safe placeholder instead of the event."""

UNKNOWN_EVENT_POLICIES: frozenset[str] = frozenset({POLICY_DROP, POLICY_PLACEHOLDER})

ACTION_EMIT: Literal["emit"] = "emit"
ACTION_DROP: Literal["drop"] = "drop"

_RESERVED_SCHEMA_FIELDS: frozenset[str] = frozenset({EVENT_TYPE_FIELD, SCHEMA_VERSION_FIELD})
"""Field names managed by the collector and therefore not allowed in a schema."""


### CLASSES
### ============================================================================
@dataclass(frozen=True)
class EventSchema:
    """A registered event schema.

    Args:
        event_type: value of ``event_type`` that selects this schema.
        version: schema version emitted alongside the retained fields.
        fields: ordered allowlist of payload field names permitted in output.

    Raises:
        ValueError: if the field allowlist is empty, contains duplicates or
            reserves a field name managed by the collector.
    """

    event_type: str
    version: str
    fields: Iterable[str]

    def __post_init__(self) -> None:
        fields_tuple = tuple(self.fields)
        if not fields_tuple:
            raise ValueError(f"schema {self.event_type!r} must declare at least one field")
        if len(set(fields_tuple)) != len(fields_tuple):
            raise ValueError(f"schema {self.event_type!r} contains duplicate fields")
        reserved = _RESERVED_SCHEMA_FIELDS.intersection(fields_tuple)
        if reserved:
            raise ValueError(
                f"schema {self.event_type!r} must not reserve field(s): {sorted(reserved)}"
            )
        object.__setattr__(self, "fields", fields_tuple)


@dataclass
class SchemaRegistry:
    """Trusted registry mapping event types to their schemas."""

    _schemas: dict[str, EventSchema] = field(default_factory=dict)

    def register(self, schema: EventSchema) -> None:
        """Register a schema.

        Args:
            schema: the schema to register.

        Raises:
            ValueError: if a schema is already registered for the event type.
        """
        if schema.event_type in self._schemas:
            raise ValueError(f"a schema is already registered for {schema.event_type!r}")
        self._schemas[schema.event_type] = schema
        return

    def get(self, event_type: str) -> EventSchema | None:
        """Return the schema registered for ``event_type``, if any."""
        return self._schemas.get(event_type)

    @classmethod
    def from_schemas(cls, schemas: Iterable[EventSchema]) -> SchemaRegistry:
        """Create a registry pre-populated with the given schemas."""
        registry = cls()
        for schema in schemas:
            registry.register(schema)
        return registry


@dataclass(frozen=True)
class AuditResult:
    """Outcome of sanitizing a single event.

    Args:
        action: ``"emit"`` with sanitized ``data``, or ``"drop"`` with empty data.
        data: the fields safe to serialize.
    """

    action: Literal["emit", "drop"]
    data: LogData


### FUNCTIONS
### ============================================================================
def event_from_log_record(record: logging.LogRecord) -> LogData:
    """Extract an event mapping from a ``LogRecord``.

    The event may be logged either as a mapping (e.g. ``logger.info({...})``) or
    as a JSON encoded string. Anything that cannot be decoded into a JSON object
    is treated as an empty event and therefore becomes an unknown event.

    Args:
        record: the log record to extract the event from.
    """
    cached = getattr(record, "_audit_event", None)
    if isinstance(cached, Mapping):
        return dict(cached)

    message = record.msg
    if isinstance(message, Mapping):
        return dict(message)
    if isinstance(message, str):
        try:
            decoded = json.loads(message)
        except (TypeError, ValueError):
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def sanitize_event(
    event: Mapping[str, Any],
    registry: SchemaRegistry,
    *,
    unknown_policy: str = POLICY_PLACEHOLDER,
    placeholder_fields: Iterable[str] = (),
) -> AuditResult:
    """Apply a registered schema to an event using field allowlisting.

    Args:
        event: untrusted event payload read from a JSON log.
        registry: trusted schema registry keyed by ``event_type``.
        unknown_policy: handling of unregistered event types, either
            :data:`POLICY_DROP` or :data:`POLICY_PLACEHOLDER`.
        placeholder_fields: explicit allowlist of payload fields that may be
            copied into a placeholder. Defaults to allowing none.

    Raises:
        ValueError: if ``unknown_policy`` is not recognised.
    """
    if unknown_policy not in UNKNOWN_EVENT_POLICIES:
        raise ValueError(
            f"unknown_policy must be one of {sorted(UNKNOWN_EVENT_POLICIES)}, "
            f"got {unknown_policy!r}"
        )

    event_type = event.get(EVENT_TYPE_FIELD)
    if not isinstance(event_type, str):
        event_type = None
    schema = registry.get(event_type) if event_type is not None else None

    if schema is None:
        if unknown_policy == POLICY_DROP:
            return AuditResult(ACTION_DROP, {})
        placeholder: LogData = {EVENT_TYPE_FIELD: event_type}
        for name in placeholder_fields:
            if name in event:
                placeholder[name] = event[name]
        return AuditResult(ACTION_EMIT, placeholder)

    # Both the version marker and the retained fields originate from the
    # registered schema, so log content can never influence the schema version.
    sanitized: LogData = {
        EVENT_TYPE_FIELD: schema.event_type,
        SCHEMA_VERSION_FIELD: schema.version,
    }
    for name in schema.fields:
        if name in event:
            sanitized[name] = event[name]
    return AuditResult(ACTION_EMIT, sanitized)


### FILTERS & FORMATTERS
### ============================================================================
class AuditEventFilter(logging.Filter):
    """Logging filter that drops records rejected by the unknown event policy.

    Pair this with :class:`AuditJsonFormatter` using the same policy so dropped
    events never reach the formatter and cannot produce blank output lines.
    Records parsed by the filter cache their event on the record for the
    formatter.
    """

    def __init__(
        self,
        registry: SchemaRegistry,
        *,
        unknown_policy: str = POLICY_PLACEHOLDER,
    ) -> None:
        """Args:
            registry: trusted schema registry.
            unknown_policy: handling of unregistered event types.
        """
        if unknown_policy not in UNKNOWN_EVENT_POLICIES:
            raise ValueError(
                f"unknown_policy must be one of {sorted(UNKNOWN_EVENT_POLICIES)}, "
                f"got {unknown_policy!r}"
            )
        super().__init__()
        self.registry = registry
        self.unknown_policy = unknown_policy
        return

    def filter(self, record: logging.LogRecord) -> bool:
        event = event_from_log_record(record)
        record._audit_event = event  # type: ignore[attr-defined]
        result = sanitize_event(event, self.registry, unknown_policy=self.unknown_policy)
        return result.action != ACTION_DROP


class AuditJsonFormatter(JsonFormatter):
    """JSON formatter that emits only schema-registered allowlisted fields.

    One formatter instance can serve every registered event type: the event is
    routed using its ``event_type`` and the output schema (fields and version
    marker) is taken entirely from the trusted registry.
    """

    def __init__(
        self,
        *args: Any,
        registry: SchemaRegistry,
        unknown_policy: str = POLICY_PLACEHOLDER,
        placeholder_fields: Iterable[str] = (),
        **kwargs: Any,
    ) -> None:
        """Args:
            args: see :class:`pythonjsonlogger.json.JsonFormatter`.
            registry: trusted schema registry keyed by ``event_type``.
            unknown_policy: handling of unregistered event types, either
                :data:`POLICY_DROP` or :data:`POLICY_PLACEHOLDER`.
            placeholder_fields: payload fields allowed inside a placeholder.
            kwargs: see :class:`pythonjsonlogger.json.JsonFormatter`.
        """
        if unknown_policy not in UNKNOWN_EVENT_POLICIES:
            raise ValueError(
                f"unknown_policy must be one of {sorted(UNKNOWN_EVENT_POLICIES)}, "
                f"got {unknown_policy!r}"
            )
        super().__init__(*args, **kwargs)
        self.registry = registry
        self.unknown_policy = unknown_policy
        self.placeholder_fields = tuple(placeholder_fields)
        return

    def format(self, record: logging.LogRecord) -> str:
        """Format a record according to its registered schema.

        Returns an empty string for events dropped under the ``"drop"`` policy;
        attach an :class:`AuditEventFilter` to the handler to suppress them
        entirely.
        """
        event = event_from_log_record(record)
        result = sanitize_event(
            event,
            self.registry,
            unknown_policy=self.unknown_policy,
            placeholder_fields=self.placeholder_fields,
        )
        if result.action == ACTION_DROP:
            return ""
        return self.serialize_log_record(result.data)
