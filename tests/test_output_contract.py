"""Shared output contract for the JSON formatter backends.

These tests pin the *parsed* JSON contract (field names and value types) that
must hold across the standard library and optional encoder backends. They are
written so the suite passes when only the standard library backend is
installed: optional backends are added conditionally.

Two migration concerns are covered explicitly:

- value contracts for dates, exceptions and other registered types
- the fallback for unregistered objects must never expose the default
  ``object.__repr__`` output (which includes an in-memory address)
"""

### IMPORTS
### ============================================================================
## Future
from __future__ import annotations

## Standard Library
from dataclasses import dataclass
import datetime
import enum
import io
import json
import logging
import re
import uuid
from collections.abc import Generator
from typing import Any

## Installed
import pytest

## Application
import pythonjsonlogger
from pythonjsonlogger.core import BaseJsonFormatter
from pythonjsonlogger.json import JsonFormatter

if pythonjsonlogger.MSGSPEC_AVAILABLE:
    from pythonjsonlogger.msgspec import MsgspecFormatter

### SETUP
### ============================================================================
BACKENDS: list[tuple[str, type[BaseJsonFormatter], dict[str, Any]]] = [
    ("stdlib", JsonFormatter, {})
]
if pythonjsonlogger.MSGSPEC_AVAILABLE:
    BACKENDS.append(("msgspec", MsgspecFormatter, {}))
    BACKENDS.append(("msgspec-stdlib-compat", MsgspecFormatter, {"stdlib_compat": True}))

BACKEND_IDS = [backend[0] for backend in BACKENDS]


@dataclass
class SampleDataclass:
    name: str
    count: int


class SampleEnum(enum.Enum):
    TEXT = "text"
    NUMBER = 7


class UnregisteredPlain:
    """Object without custom ``__str__``/``__repr__``."""


class UnregisteredStr:
    def __str__(self) -> str:
        return "registered-via-str"


class UnregisteredRepr:
    def __repr__(self) -> str:
        return "registered-via-repr"


class UnregisteredBroken:
    def __str__(self) -> str:
        raise ValueError("str is broken")

    def __repr__(self) -> str:
        raise ValueError("repr is broken")


_LOGGER_COUNT = 0


@pytest.fixture
def env() -> Generator[tuple[logging.Logger, io.StringIO]]:
    global _LOGGER_COUNT  # pylint: disable=global-statement
    _LOGGER_COUNT += 1
    logger = logging.getLogger(f"pythonjsonlogger.contract.{_LOGGER_COUNT}")
    logger.setLevel(logging.DEBUG)
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    logger.addHandler(handler)
    yield logger, buffer
    logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)
    buffer.close()
    return


def render(
    env: tuple[logging.Logger, io.StringIO],
    backend: type[BaseJsonFormatter],
    formatter_kwargs: dict[str, Any],
    payload: dict[str, Any],
) -> Any:
    logger, buffer = env
    handler = logger.handlers[0]
    handler.setFormatter(backend(**formatter_kwargs))
    logger.info(payload)
    return json.loads(buffer.getvalue())


def render_extra(
    env: tuple[logging.Logger, io.StringIO],
    backend: type[BaseJsonFormatter],
    formatter_kwargs: dict[str, Any],
    payload: dict[str, Any],
) -> Any:
    """Render payload values via `extra`, avoiding logging's `str(record.msg)`."""
    logger, buffer = env
    handler = logger.handlers[0]
    handler.setFormatter(backend(**formatter_kwargs))
    logger.info("contract", extra=payload)
    result = json.loads(buffer.getvalue())
    return {key: value for key, value in result.items() if key in payload}


### TESTS
### ============================================================================
def test_standard_backend_always_available() -> None:
    """The contract suite must always exercise the standard library branch."""
    assert JsonFormatter in [backend[1] for backend in BACKENDS]
    return


@pytest.mark.parametrize("backend_name,backend,kwargs", BACKENDS, ids=BACKEND_IDS)
def test_scalar_field_names_and_types(
    env: tuple[logging.Logger, io.StringIO],
    backend_name: str,
    backend: type[BaseJsonFormatter],
    kwargs: dict[str, Any],
) -> None:
    payload = {
        "message": "hello",
        "flag": True,
        "count": 42,
        "ratio": 0.5,
        "missing": None,
        "nested": {"inner": "value"},
        "items": [1, "two", False],
    }
    result = render(env, backend, kwargs, payload)

    assert set(result) == set(payload)
    assert isinstance(result["message"], str)
    assert isinstance(result["flag"], bool)
    assert isinstance(result["count"], int)
    assert isinstance(result["ratio"], float)
    assert result["missing"] is None
    assert result["nested"] == {"inner": "value"}
    assert result["items"] == [1, "two", False]
    return


@pytest.mark.parametrize("backend_name,backend,kwargs", BACKENDS, ids=BACKEND_IDS)
def test_date_fields_are_iso_strings(
    env: tuple[logging.Logger, io.StringIO],
    backend_name: str,
    backend: type[BaseJsonFormatter],
    kwargs: dict[str, Any],
) -> None:
    aware = datetime.datetime(2024, 5, 5, 16, 45, 30, tzinfo=datetime.timezone.utc)
    naive = datetime.datetime(2024, 5, 5, 16, 45, 30)
    result = render(
        env,
        backend,
        kwargs,
        {
            "aware": aware,
            "naive": naive,
            "day": datetime.date(2024, 5, 5),
            "moment": datetime.time(16, 45, 30),
        },
    )

    assert all(isinstance(result[key], str) for key in ("aware", "naive", "day", "moment"))
    assert result["naive"] == "2024-05-05T16:45:30"
    assert result["day"] == "2024-05-05"
    assert result["moment"] == "16:45:30"
    if backend_name == "msgspec":
        # Native msgspec uses the RFC 3339 UTC designator.
        assert result["aware"] == "2024-05-05T16:45:30Z"
    else:
        # Standard library contract (and the msgspec compatibility mode).
        assert result["aware"] == "2024-05-05T16:45:30+00:00"
    return


@pytest.mark.parametrize("backend_name,backend,kwargs", BACKENDS, ids=BACKEND_IDS)
def test_timestamp_field_contract(
    env: tuple[logging.Logger, io.StringIO],
    backend_name: str,
    backend: type[BaseJsonFormatter],
    kwargs: dict[str, Any],
) -> None:
    logger, buffer = env
    logger.handlers[0].setFormatter(backend(timestamp=True, **kwargs))
    logger.info("hello")
    result = json.loads(buffer.getvalue())

    assert isinstance(result["timestamp"], str)
    if backend_name == "msgspec":
        assert result["timestamp"].endswith("Z")
    else:
        assert result["timestamp"].endswith("+00:00")
    return


@pytest.mark.parametrize("backend_name,backend,kwargs", BACKENDS, ids=BACKEND_IDS)
def test_bytes_fields_are_base64_strings(
    env: tuple[logging.Logger, io.StringIO],
    backend_name: str,
    backend: type[BaseJsonFormatter],
    kwargs: dict[str, Any],
) -> None:
    result = render(
        env,
        backend,
        kwargs,
        {"safe": b"plain", "alphabet": b"\xfb\xff", "array": bytearray(b"plain")},
    )

    assert all(isinstance(result[key], str) for key in ("safe", "alphabet", "array"))
    assert result["safe"] == "cGxhaW4="
    if backend_name == "msgspec":
        # Native msgspec uses the standard base64 alphabet.
        assert result["alphabet"] == "+/8="
    else:
        # Standard library contract uses URL-safe base64.
        assert result["alphabet"] == "-_8="
        assert result["array"] == "cGxhaW4="
    return


@pytest.mark.parametrize("backend_name,backend,kwargs", BACKENDS, ids=BACKEND_IDS)
def test_exception_and_traceback_contract(
    env: tuple[logging.Logger, io.StringIO],
    backend_name: str,
    backend: type[BaseJsonFormatter],
    kwargs: dict[str, Any],
) -> None:
    try:
        raise ValueError("boom")
    except ValueError as exc:
        traceback_obj = exc.__traceback__
        exception_obj = exc
        logger, buffer = env
        logger.handlers[0].setFormatter(backend(**kwargs))
        logger.exception("caught")
        exc_info_result = json.loads(buffer.getvalue())
        buffer.seek(0)
        buffer.truncate(0)

    payload_result = render_extra(
        env,
        backend,
        kwargs,
        {"error": exception_obj, "traceback": traceback_obj},
    )

    assert isinstance(exc_info_result["exc_info"], str)
    assert "ValueError: boom" in exc_info_result["exc_info"]

    assert payload_result["error"] == "ValueError: boom"
    assert isinstance(payload_result["traceback"], str)
    assert "test_output_contract.py" in payload_result["traceback"]
    assert "object at 0x" not in payload_result["traceback"]
    return


@pytest.mark.skipif(
    not pythonjsonlogger.MSGSPEC_AVAILABLE, reason="msgspec backend is not installed"
)
def test_exception_notes_contract(env: tuple[logging.Logger, io.StringIO]) -> None:
    err = ValueError("boom")
    err.add_note("extra context")
    result = render(env, MsgspecFormatter, {}, {"error": err})
    assert result["error"] == "ValueError: boom\nextra context"
    return


@pytest.mark.parametrize("backend_name,backend,kwargs", BACKENDS, ids=BACKEND_IDS)
def test_enum_and_dataclass_contract(
    env: tuple[logging.Logger, io.StringIO],
    backend_name: str,
    backend: type[BaseJsonFormatter],
    kwargs: dict[str, Any],
) -> None:
    result = render(
        env,
        backend,
        kwargs,
        {
            "enum_value": SampleEnum.TEXT,
            "enum_number": SampleEnum.NUMBER,
            "enum_class": SampleEnum,
            "dataclass": SampleDataclass(name="sample", count=3),
            "uuid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
            "class_ref": SampleDataclass,
        },
    )

    assert result["enum_value"] == "text"
    assert result["enum_number"] == 7
    assert result["enum_class"] == ["text", 7]
    assert result["dataclass"] == {"name": "sample", "count": 3}
    assert result["uuid"] == "12345678-1234-5678-1234-567812345678"
    assert result["class_ref"] == "SampleDataclass"
    return


@pytest.mark.parametrize("backend_name,backend,kwargs", BACKENDS, ids=BACKEND_IDS)
def test_unregistered_object_fallback_never_exposes_default_repr(
    env: tuple[logging.Logger, io.StringIO],
    backend_name: str,
    backend: type[BaseJsonFormatter],
    kwargs: dict[str, Any],
) -> None:
    result = render_extra(
        env,
        backend,
        kwargs,
        {
            "plain": UnregisteredPlain(),
            "via_str": UnregisteredStr(),
            "via_repr": UnregisteredRepr(),
            "broken": UnregisteredBroken(),
            "nested": {"plain": UnregisteredPlain()},
            "listed": [UnregisteredPlain()],
        },
    )

    for key in ("plain", "via_str", "via_repr", "broken", "nested", "listed"):
        assert key in result
    assert result["via_str"] == "registered-via-str"
    assert result["via_repr"] == "registered-via-repr"
    assert result["plain"] == "UnregisteredPlain"
    assert result["broken"] == "UnregisteredBroken"
    assert result["nested"]["plain"] == "UnregisteredPlain"
    assert result["listed"][0] == "UnregisteredPlain"

    serialized = json.dumps(result)
    assert "object at 0x" not in serialized
    assert not re.search(r"<[\w.]+ object at 0x[0-9a-fA-F]+>", serialized)
    return


@pytest.mark.skipif(
    not pythonjsonlogger.MSGSPEC_AVAILABLE, reason="msgspec backend is not installed"
)
def test_msgspec_none_default_uses_shared_hook(env: tuple[logging.Logger, io.StringIO]) -> None:
    logger, buffer = env
    logger.handlers[0].setFormatter(MsgspecFormatter(json_default=None))
    logger.info("contract", extra={"plain": UnregisteredPlain()})
    assert json.loads(buffer.getvalue())["plain"] == "UnregisteredPlain"
    return


@pytest.mark.skipif(
    not pythonjsonlogger.MSGSPEC_AVAILABLE, reason="msgspec backend is not installed"
)
def test_msgspec_stdlib_compat_matches_stdlib(
    env: tuple[logging.Logger, io.StringIO],
) -> None:
    payload = {
        "aware": datetime.datetime(2024, 5, 5, 16, 45, 30, tzinfo=datetime.timezone.utc),
        "naive": datetime.datetime(2024, 5, 5, 16, 45, 30),
        "bytes": b"\xfb\xff",
        "enum": SampleEnum.TEXT,
        "dataclass": SampleDataclass(name="sample", count=3),
        "set": {1, 2},
    }

    compat_result = render(env, MsgspecFormatter, {"stdlib_compat": True}, payload)
    stdlib_result = render(env, JsonFormatter, {}, payload)

    assert compat_result == stdlib_result
    return
