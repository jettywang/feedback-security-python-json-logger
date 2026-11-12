"""Lock in formatter config parsing/merging rules and pre-check ambiguous configs.

Production logging config changes are published through ``logging.config.dictConfig``.
Two fields configured to the same output name (e.g. a ``static_fields`` key matching
a ``fmt`` field, or two ``rename_fields`` targets colliding) are silently accepted by
the formatter: the merge in ``add_fields`` is last-write-wins, so downstream audit
systems cannot tell which field was kept.

These tests:

- lock in the existing parsing and merging rules (including the silent overwrite),
- provide a reference pre-check (``precheck_formatter_config``) that rejects ambiguous
  configs before they are published, distinguishing static-field collisions
  (``"static_fields"``) from rename collisions (``"rename_fields"``),
- observe, from both the direct-construction and the ``dictConfig`` entry points, where
  config failures surface and what JSON valid configs produce.

Everything runs against in-memory streams; no real logging platform is involved.
"""

### IMPORTS
### ============================================================================
## Future
from __future__ import annotations

## Standard Library
from dataclasses import dataclass
import io
import json
import logging
import logging.config
from typing import Any
from collections.abc import Generator, Iterable, Mapping

## Installed
import pytest

## Application
from pythonjsonlogger.json import JsonFormatter

### SETUP
### ============================================================================
_LOGGER_COUNT = 0

DICTCONFIG_LOGGER_NAME = "pythonjsonlogger.tests.field_collisions.dictconfig"
DICTCONFIG_BUFFER = io.StringIO()
"""Stream referenced by dictConfig payloads via ``ext://`` (no real log platform)."""


@dataclass
class LoggingEnvironment:
    logger: logging.Logger
    buffer: io.StringIO
    handler: logging.Handler

    def set_formatter(self, formatter: JsonFormatter) -> None:
        self.handler.setFormatter(formatter)
        return

    def load_json(self) -> Any:
        return json.loads(self.buffer.getvalue())


@pytest.fixture
def env() -> Generator[LoggingEnvironment]:
    global _LOGGER_COUNT  # pylint: disable=global-statement
    _LOGGER_COUNT += 1
    logger = logging.getLogger(f"pythonjsonlogger.tests.field_collisions.{_LOGGER_COUNT}")
    logger.setLevel(logging.DEBUG)
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    logger.addHandler(handler)
    yield LoggingEnvironment(logger=logger, buffer=buffer, handler=handler)
    logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)
    buffer.close()
    return


@pytest.fixture(autouse=True)
def clean_dictconfig_state() -> Generator[None]:
    DICTCONFIG_BUFFER.seek(0)
    DICTCONFIG_BUFFER.truncate(0)
    yield
    logging.getLogger(DICTCONFIG_LOGGER_NAME).handlers.clear()
    DICTCONFIG_BUFFER.seek(0)
    DICTCONFIG_BUFFER.truncate(0)
    return


def dictconfig_dict(formatter_payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a formatter payload in a minimal dictConfig schema dict."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": formatter_payload},
        "handlers": {
            "buffer": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "stream": "ext://tests.test_field_collisions.DICTCONFIG_BUFFER",
            }
        },
        "loggers": {
            DICTCONFIG_LOGGER_NAME: {
                "handlers": ["buffer"],
                "level": "DEBUG",
                "propagate": False,
            }
        },
    }


### PRE-CHECK (reference implementation under test)
### ============================================================================
@dataclass(frozen=True)
class OutputNameCollision:
    """Two distinct config sources write the same output field name."""

    output_name: str
    kind: str  # "static_fields" or "rename_fields"
    sources: tuple[str, ...]  # "origin:field" entries, e.g. ("fmt:message", "static_fields:x")


def find_output_name_collisions(
    required_fields: Iterable[str],
    *,
    rename_fields: Mapping[str, str] | None = None,
    static_fields: Mapping[str, Any] | None = None,
    defaults: Mapping[str, Any] | None = None,
    timestamp: bool | str = False,
) -> list[OutputNameCollision]:
    """Find output field names written by more than one distinct config source.

    Mirrors the output-name resolution in ``BaseJsonFormatter.add_fields``: every
    source field is renamed through ``rename_fields`` before being written.

    A collision involving ``static_fields`` is reported with kind ``"static_fields"``;
    any other collision only exists because ``rename_fields`` mapped distinct source
    fields together and is reported with kind ``"rename_fields"``. Overlaps that need
    no rename and involve no static field (e.g. ``defaults`` being overridden by a
    ``fmt`` field, which is documented behaviour) are not ambiguous and are ignored.
    """
    rename_fields = rename_fields or {}
    sources: dict[str, list[tuple[str, str]]] = {}

    def add(origin: str, field: str) -> None:
        output_name = rename_fields.get(field, field)
        sources.setdefault(output_name, []).append((origin, field))
        return

    for field in defaults or {}:
        add("defaults", field)
    for field in required_fields:
        add("fmt", field)
    for field in static_fields or {}:
        add("static_fields", field)
    if timestamp:
        add("timestamp", timestamp if isinstance(timestamp, str) else "timestamp")

    collisions = []
    for output_name, entries in sources.items():
        distinct = sorted(set(entries))
        if len(distinct) < 2:
            continue
        origins = {origin for origin, _ in distinct}
        renamed = any(field in rename_fields for _, field in distinct)
        if "static_fields" in origins:
            kind = "static_fields"
        elif renamed:
            kind = "rename_fields"
        else:
            continue
        collisions.append(
            OutputNameCollision(
                output_name=output_name,
                kind=kind,
                sources=tuple(f"{origin}:{field}" for origin, field in distinct),
            )
        )
    return collisions


def precheck_formatter_config(config: Mapping[str, Any]) -> list[OutputNameCollision]:
    """Pre-flight a formatter config before it is constructed or published.

    Accepts both direct-construction kwargs (``fmt``) and a dictConfig formatter
    payload (``format``, optional ``()``/``class`` keys). Invalid configs are rejected
    by the exceptions raised while resolving them; ambiguous configs are reported as
    collisions.
    """
    kwargs = dict(config)
    kwargs.pop("()", None)
    kwargs.pop("class", None)
    if "format" in kwargs and "fmt" not in kwargs:
        kwargs["fmt"] = kwargs.pop("format")
    formatter = JsonFormatter(**kwargs)
    return find_output_name_collisions(
        formatter._required_fields,  # pylint: disable=protected-access
        rename_fields=formatter.rename_fields,
        static_fields=formatter.static_fields,
        defaults=formatter.defaults,
        timestamp=formatter.timestamp,
    )


### SHARED CONFIGS
### ============================================================================
VALID_KWARGS: dict[str, Any] = {
    "fmt": "%(levelname)s %(message)s",
    "rename_fields": {"levelname": "log.level"},
    "static_fields": {"service": "audit"},
    "defaults": {"environment": "prod"},
}
VALID_PAYLOAD: dict[str, Any] = {
    "()": "pythonjsonlogger.json.JsonFormatter",
    "format": "%(levelname)s %(message)s",
    "rename_fields": {"levelname": "log.level"},
    "static_fields": {"service": "audit"},
    "defaults": {"environment": "prod"},
}
VALID_JSON = {
    "log.level": "INFO",
    "message": "hello",
    "service": "audit",
    "environment": "prod",
}

STATIC_COLLISION_KWARGS: dict[str, Any] = {
    "fmt": "%(message)s",
    "static_fields": {"message": "STATIC"},
}
STATIC_COLLISION_PAYLOAD: dict[str, Any] = {
    "()": "pythonjsonlogger.json.JsonFormatter",
    "format": "%(message)s",
    "static_fields": {"message": "STATIC"},
}

RENAME_COLLISION_KWARGS: dict[str, Any] = {
    "fmt": "%(levelname)s %(message)s",
    "rename_fields": {"levelname": "out", "message": "out"},
}
RENAME_COLLISION_PAYLOAD: dict[str, Any] = {
    "()": "pythonjsonlogger.json.JsonFormatter",
    "format": "%(levelname)s %(message)s",
    "rename_fields": {"levelname": "out", "message": "out"},
}

INVALID_RESERVED_ATTRS_KWARGS: dict[str, Any] = {"reserved_attrs": "message"}
INVALID_RESERVED_ATTRS_PAYLOAD: dict[str, Any] = {
    "()": "pythonjsonlogger.json.JsonFormatter",
    "reserved_attrs": "message",
}


### TESTS: merge rules (direct construction)
### ============================================================================
def test_merge_stage_order_defaults_static_fields_message_dict(env: LoggingEnvironment):
    """Merge order is defaults < static_fields < logged dict; later stages silently win."""
    env.set_formatter(
        JsonFormatter(
            defaults={"shared": "defaults", "defaults_only": 1},
            static_fields={"shared": "static_fields", "static_only": 2},
        )
    )

    env.logger.info({"shared": "message_dict", "dict_only": 3})
    log_json = env.load_json()

    assert log_json["shared"] == "message_dict"
    assert log_json["defaults_only"] == 1
    assert log_json["static_only"] == 2
    assert log_json["dict_only"] == 3
    return


def test_static_fields_override_defaults_without_message_dict(env: LoggingEnvironment):
    env.set_formatter(
        JsonFormatter(
            defaults={"shared": "defaults"},
            static_fields={"shared": "static_fields"},
        )
    )

    env.logger.info("plain message")
    log_json = env.load_json()

    assert log_json["shared"] == "static_fields"
    return


def test_record_extra_overrides_static_fields(env: LoggingEnvironment):
    """Record extras merge after static_fields, so an extra key wins the same output name."""
    env.set_formatter(JsonFormatter(static_fields={"stream": "static"}))

    env.logger.info("hello", extra={"stream": "extra"})
    log_json = env.load_json()

    assert log_json["stream"] == "extra"
    return


def test_static_field_collision_silently_overrides_fmt_field(env: LoggingEnvironment):
    """The incident: static_fields and fmt both write "message"; the static value wins."""
    env.set_formatter(JsonFormatter(**STATIC_COLLISION_KWARGS))

    env.logger.info("real message")
    log_json = env.load_json()

    assert log_json == {"message": "STATIC"}
    return


def test_rename_collision_last_fmt_field_wins(env: LoggingEnvironment):
    """Two fields renamed to the same output name: the later fmt field is kept."""
    env.set_formatter(JsonFormatter(**RENAME_COLLISION_KWARGS))

    env.logger.info("hello")
    log_json = env.load_json()

    assert log_json == {"out": "hello"}
    return


def test_rename_onto_unrenamed_field_overwrites_it(env: LoggingEnvironment):
    """A rename target matching another field's output name silently overwrites it."""
    env.set_formatter(
        JsonFormatter("%(levelname)s %(message)s", rename_fields={"message": "levelname"})
    )

    env.logger.info("hello")
    log_json = env.load_json()

    assert log_json == {"levelname": "hello"}
    return


### TESTS: pre-check rejects ambiguous configs
### ============================================================================
@pytest.mark.parametrize(
    "kwargs, output_name",
    [
        # static_fields key matches a fmt field
        (STATIC_COLLISION_KWARGS, "message"),
        # static_fields key renamed onto a fmt field
        (
            {
                "fmt": "%(levelname)s",
                "rename_fields": {"level": "levelname"},
                "static_fields": {"level": "prod"},
            },
            "levelname",
        ),
        # static_fields key matches the timestamp field
        ({"timestamp": True, "static_fields": {"timestamp": "STATIC"}}, "timestamp"),
        # static_fields key matches a defaults field
        (
            {"defaults": {"environment": "prod"}, "static_fields": {"environment": "dev"}},
            "environment",
        ),
    ],
)
def test_precheck_flags_static_field_collisions(kwargs: dict[str, Any], output_name: str):
    collisions = precheck_formatter_config(kwargs)

    assert [(collision.output_name, collision.kind) for collision in collisions] == [
        (output_name, "static_fields")
    ]
    assert any(source.startswith("static_fields:") for source in collisions[0].sources)
    return


@pytest.mark.parametrize(
    "kwargs, output_name",
    [
        # two fmt fields renamed to the same output name
        (RENAME_COLLISION_KWARGS, "out"),
        # rename target matches an unrenamed fmt field
        (
            {"fmt": "%(levelname)s %(message)s", "rename_fields": {"message": "levelname"}},
            "levelname",
        ),
        # defaults field renamed onto a fmt field
        (
            {
                "fmt": "%(message)s",
                "defaults": {"app": "myapp"},
                "rename_fields": {"app": "message"},
            },
            "message",
        ),
    ],
)
def test_precheck_flags_rename_collisions(kwargs: dict[str, Any], output_name: str):
    collisions = precheck_formatter_config(kwargs)

    assert [(collision.output_name, collision.kind) for collision in collisions] == [
        (output_name, "rename_fields")
    ]
    assert not any(source.startswith("static_fields:") for source in collisions[0].sources)
    return


@pytest.mark.parametrize(
    "kwargs",
    [
        # no shared output names at all
        VALID_KWARGS,
        # defaults overridden by a fmt field is documented behaviour, not ambiguous
        {"fmt": "%(message)s", "defaults": {"message": "fallback"}},
        # repeating a field in fmt writes the same value twice, not ambiguous
        {"fmt": "%(message)s %(message)s"},
        # rename that collides with nothing
        {"fmt": "%(message)s", "rename_fields": {"message": "msg"}},
        # empty config
        {},
    ],
)
def test_precheck_allows_unambiguous_configs(kwargs: dict[str, Any]):
    assert precheck_formatter_config(kwargs) == []
    return


### TESTS: entry points observe the same collisions
### ============================================================================
@pytest.mark.parametrize(
    "kwargs, payload, kind",
    [
        (STATIC_COLLISION_KWARGS, STATIC_COLLISION_PAYLOAD, "static_fields"),
        (RENAME_COLLISION_KWARGS, RENAME_COLLISION_PAYLOAD, "rename_fields"),
    ],
)
def test_precheck_distinguishes_collision_kinds_across_entries(
    kwargs: dict[str, Any], payload: dict[str, Any], kind: str
):
    """Direct kwargs and the dictConfig payload report the same collision."""
    direct_collisions = precheck_formatter_config(kwargs)
    dictconfig_collisions = precheck_formatter_config(payload)

    assert direct_collisions == dictconfig_collisions
    assert [collision.kind for collision in direct_collisions] == [kind]
    return


@pytest.mark.parametrize(
    "kwargs, payload",
    [
        (STATIC_COLLISION_KWARGS, STATIC_COLLISION_PAYLOAD),
        (RENAME_COLLISION_KWARGS, RENAME_COLLISION_PAYLOAD),
    ],
)
def test_ambiguous_configs_are_silently_accepted_by_both_entries(
    kwargs: dict[str, Any], payload: dict[str, Any]
):
    """Neither entry point fails on ambiguous configs; the pre-check must reject them."""
    formatter = JsonFormatter(**kwargs)  # direct construction does not raise
    assert isinstance(formatter, JsonFormatter)

    logging.config.dictConfig(dictconfig_dict(payload))  # dictConfig does not raise
    assert logging.getLogger(DICTCONFIG_LOGGER_NAME).handlers

    assert precheck_formatter_config(kwargs), "pre-check must reject the ambiguous config"
    return


### TESTS: config failure locations per entry point
### ============================================================================
def test_invalid_reserved_attrs_fails_at_construction():
    """Direct construction raises TypeError from the formatter constructor."""
    with pytest.raises(TypeError, match="reserved_attrs must be a sequence"):
        JsonFormatter(**INVALID_RESERVED_ATTRS_KWARGS)
    return


def test_invalid_reserved_attrs_fails_at_dictconfig_call():
    """dictConfig wraps the same TypeError in ValueError at the dictConfig() call."""
    with pytest.raises(ValueError, match="Unable to configure formatter") as exc_info:
        logging.config.dictConfig(dictconfig_dict(INVALID_RESERVED_ATTRS_PAYLOAD))

    assert isinstance(exc_info.value.__cause__, TypeError)
    assert "reserved_attrs must be a sequence" in str(exc_info.value.__cause__)
    return


def test_precheck_rejects_invalid_reserved_attrs():
    """The pre-check surfaces the same invalid config before either entry point."""
    with pytest.raises(TypeError, match="reserved_attrs must be a sequence"):
        precheck_formatter_config(INVALID_RESERVED_ATTRS_KWARGS)
    with pytest.raises(TypeError, match="reserved_attrs must be a sequence"):
        precheck_formatter_config(INVALID_RESERVED_ATTRS_PAYLOAD)
    return


### TESTS: valid config JSON matches across entry points
### ============================================================================
def test_valid_config_json_direct_construction(env: LoggingEnvironment):
    env.set_formatter(JsonFormatter(**VALID_KWARGS))

    env.logger.info("hello")
    log_json = env.load_json()

    assert log_json == VALID_JSON
    assert precheck_formatter_config(VALID_KWARGS) == []
    return


def test_valid_config_json_dictconfig():
    assert precheck_formatter_config(VALID_PAYLOAD) == []

    logging.config.dictConfig(dictconfig_dict(VALID_PAYLOAD))
    logger = logging.getLogger(DICTCONFIG_LOGGER_NAME)
    logger.info("hello")
    log_json = json.loads(DICTCONFIG_BUFFER.getvalue())

    assert log_json == VALID_JSON
    return
