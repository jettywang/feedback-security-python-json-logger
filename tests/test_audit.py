### IMPORTS
### ============================================================================
## Future
from __future__ import annotations

## Standard Library
import io
import json
import logging
from collections.abc import Generator

import pytest

## Application
from pythonjsonlogger.audit import (
    ACTION_DROP,
    ACTION_EMIT,
    AuditEventFilter,
    AuditJsonFormatter,
    EventSchema,
    SCHEMA_VERSION_FIELD,
    SchemaRegistry,
    POLICY_DROP,
    POLICY_PLACEHOLDER,
    event_from_log_record,
    sanitize_event,
)


### SETUP
### ============================================================================
LOGIN_SCHEMA = EventSchema(
    event_type="auth.login",
    version="auth-v1",
    fields=("user_id", "ip", "result"),
)
PAYMENT_SCHEMA = EventSchema(
    event_type="payment.charge",
    version="payment-v3",
    fields=("user_id", "amount", "currency"),
)


@pytest.fixture
def registry() -> SchemaRegistry:
    return SchemaRegistry.from_schemas([LOGIN_SCHEMA, PAYMENT_SCHEMA])


class LoggingEnvironment:
    def __init__(self) -> None:
        self.logger = logging.getLogger("pythonjsonlogger.tests.audit")
        self.logger.setLevel(logging.DEBUG)
        self.buffer = io.StringIO()
        self.handler = logging.StreamHandler(self.buffer)
        self.logger.addHandler(self.handler)
        self.logger.propagate = False

    def cleanup(self) -> None:
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(logging.WARNING)
        self.buffer.close()


@pytest.fixture
def env() -> Generator[LoggingEnvironment]:
    environment = LoggingEnvironment()
    yield environment
    environment.cleanup()


def make_record(payload: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=payload,
        args=(),
        exc_info=None,
    )


### TESTS
### ============================================================================
class TestEventSchema:
    def test_rejects_empty_fields(self) -> None:
        with pytest.raises(ValueError, match="at least one field"):
            EventSchema(event_type="x", version="1", fields=())

    def test_rejects_duplicate_fields(self) -> None:
        with pytest.raises(ValueError, match="duplicate fields"):
            EventSchema(event_type="x", version="1", fields=("a", "a"))

    @pytest.mark.parametrize("reserved", ["event_type", "schema_version"])
    def test_rejects_reserved_fields(self, reserved: str) -> None:
        with pytest.raises(ValueError, match="must not reserve"):
            EventSchema(event_type="x", version="1", fields=("a", reserved))

    def test_stores_fields_as_tuple(self) -> None:
        schema = EventSchema(event_type="x", version="1", fields=iter(["a", "b"]))
        assert schema.fields == ("a", "b")


class TestSchemaRegistry:
    def test_duplicate_registration_rejected(self, registry: SchemaRegistry) -> None:
        with pytest.raises(ValueError, match="already registered"):
            registry.register(
                EventSchema(event_type="auth.login", version="auth-v2", fields=("user_id",))
            )

    def test_get_unknown_returns_none(self, registry: SchemaRegistry) -> None:
        assert registry.get("nope") is None


class TestSanitizeEvent:
    def test_overlapping_fields_kept_per_schema(self, registry: SchemaRegistry) -> None:
        login = sanitize_event(
            {
                "event_type": "auth.login",
                "user_id": "u-1",
                "ip": "192.0.2.1",
                "result": "ok",
                "amount": 999,
            },
            registry,
        )
        assert login.action == ACTION_EMIT
        assert set(login.data) == {"event_type", "schema_version", "user_id", "ip", "result"}
        assert login.data["schema_version"] == "auth-v1"
        assert "amount" not in login.data

        payment = sanitize_event(
            {
                "event_type": "payment.charge",
                "user_id": "u-1",
                "amount": 10,
                "currency": "USD",
                "ip": "192.0.2.1",
            },
            registry,
        )
        assert payment.action == ACTION_EMIT
        assert set(payment.data) == {
            "event_type",
            "schema_version",
            "user_id",
            "amount",
            "currency",
        }
        assert payment.data["schema_version"] == "payment-v3"
        assert "ip" not in payment.data

    def test_version_matches_retained_fields(self, registry: SchemaRegistry) -> None:
        result = sanitize_event(
            {
                "event_type": "auth.login",
                "user_id": "u",
                "ip": "ip",
                "result": "ok",
                "schema_version": "forged-999",
                "extra": "leak",
            },
            registry,
        )
        assert result.data[SCHEMA_VERSION_FIELD] == "auth-v1"
        assert set(result.data) == {"event_type", "schema_version", "user_id", "ip", "result"}
        assert "extra" not in result.data

    def test_missing_allowlisted_fields_are_absent(self, registry: SchemaRegistry) -> None:
        result = sanitize_event({"event_type": "auth.login", "user_id": "u"}, registry)
        assert set(result.data) == {"event_type", "schema_version", "user_id"}

    def test_unknown_type_drop_policy(self, registry: SchemaRegistry) -> None:
        result = sanitize_event(
            {"event_type": "mystery.event", "secret": "x"},
            registry,
            unknown_policy=POLICY_DROP,
        )
        assert result.action == ACTION_DROP
        assert result.data == {}

    def test_unknown_placeholder_contains_no_payload(
        self, registry: SchemaRegistry
    ) -> None:
        result = sanitize_event(
            {"event_type": "mystery.event", "secret": "x", "user_id": "u"},
            registry,
            unknown_policy=POLICY_PLACEHOLDER,
        )
        assert result.action == ACTION_EMIT
        assert result.data == {"event_type": "mystery.event"}
        assert SCHEMA_VERSION_FIELD not in result.data

    def test_placeholder_fields_are_allowlisted(self, registry: SchemaRegistry) -> None:
        result = sanitize_event(
            {"event_type": "mystery.event", "secret": "x", "trace_id": "t-1"},
            registry,
            unknown_policy=POLICY_PLACEHOLDER,
            placeholder_fields=("trace_id",),
        )
        assert result.data == {"event_type": "mystery.event", "trace_id": "t-1"}
        assert "secret" not in result.data

    def test_missing_event_type_treated_as_unknown(self, registry: SchemaRegistry) -> None:
        dropped = sanitize_event({"user_id": "u"}, registry, unknown_policy=POLICY_DROP)
        assert dropped.action == ACTION_DROP

        placeholder = sanitize_event(
            {"user_id": "u"}, registry, unknown_policy=POLICY_PLACEHOLDER
        )
        assert placeholder.data == {"event_type": None}

    def test_non_string_event_type_treated_as_unknown(self, registry: SchemaRegistry) -> None:
        result = sanitize_event(
            {"event_type": 123, "secret": "x"}, registry, unknown_policy=POLICY_DROP
        )
        assert result.action == ACTION_DROP

    def test_invalid_policy_rejected(self, registry: SchemaRegistry) -> None:
        with pytest.raises(ValueError, match="unknown_policy"):
            sanitize_event({"event_type": "auth.login"}, registry, unknown_policy="nope")


class TestEventFromLogRecord:
    def test_dict_message(self) -> None:
        event = event_from_log_record(make_record({"event_type": "auth.login"}))
        assert event == {"event_type": "auth.login"}

    def test_json_string_message(self) -> None:
        event = event_from_log_record(make_record(json.dumps({"event_type": "auth.login"})))
        assert event == {"event_type": "auth.login"}

    def test_invalid_json_becomes_empty_event(self) -> None:
        assert event_from_log_record(make_record("not json")) == {}

    def test_json_non_object_becomes_empty_event(self) -> None:
        assert event_from_log_record(make_record(json.dumps([1, 2]))) == {}

    def test_cached_event_on_record_is_used(self) -> None:
        record = make_record({"event_type": "auth.login"})
        record._audit_event = {"event_type": "payment.charge"}  # type: ignore[attr-defined]
        assert event_from_log_record(record) == {"event_type": "payment.charge"}


class TestAuditJsonFormatter:
    def test_formats_registered_event(self, registry: SchemaRegistry) -> None:
        formatter = AuditJsonFormatter(registry=registry)
        record = make_record({"event_type": "auth.login", "user_id": "u"})
        output = json.loads(formatter.format(record))
        assert output == {
            "event_type": "auth.login",
            "schema_version": "auth-v1",
            "user_id": "u",
        }

    def test_formats_json_string_event(self, registry: SchemaRegistry) -> None:
        formatter = AuditJsonFormatter(registry=registry)
        output = json.loads(
            formatter.format(make_record(json.dumps({"event_type": "payment.charge", "amount": 5})))
        )
        assert output["schema_version"] == "payment-v3"
        assert output["amount"] == 5
        assert set(output) == {"event_type", "schema_version", "amount"}

    def test_drop_policy_returns_empty_string(self, registry: SchemaRegistry) -> None:
        formatter = AuditJsonFormatter(registry=registry, unknown_policy=POLICY_DROP)
        assert formatter.format(make_record({"event_type": "mystery.event"})) == ""

    def test_placeholder_policy_emits_safe_placeholder(self, registry: SchemaRegistry) -> None:
        formatter = AuditJsonFormatter(registry=registry, unknown_policy=POLICY_PLACEHOLDER)
        output = json.loads(
            formatter.format(make_record({"event_type": "mystery.event", "secret": "x"}))
        )
        assert output == {"event_type": "mystery.event"}

    def test_invalid_policy_rejected(self, registry: SchemaRegistry) -> None:
        with pytest.raises(ValueError, match="unknown_policy"):
            AuditJsonFormatter(registry=registry, unknown_policy="nope")


class TestAuditEventFilter:
    def test_registered_event_passes(self, registry: SchemaRegistry) -> None:
        audit_filter = AuditEventFilter(registry, unknown_policy=POLICY_DROP)
        record = make_record({"event_type": "auth.login"})
        assert audit_filter.filter(record) is True

    def test_unknown_event_dropped(self, registry: SchemaRegistry) -> None:
        audit_filter = AuditEventFilter(registry, unknown_policy=POLICY_DROP)
        record = make_record({"event_type": "mystery.event"})
        assert audit_filter.filter(record) is False

    def test_placeholder_policy_keeps_unknown_event(self, registry: SchemaRegistry) -> None:
        audit_filter = AuditEventFilter(registry, unknown_policy=POLICY_PLACEHOLDER)
        record = make_record({"event_type": "mystery.event"})
        assert audit_filter.filter(record) is True
        assert record._audit_event == {"event_type": "mystery.event"}  # type: ignore[attr-defined]

    def test_invalid_policy_rejected(self, registry: SchemaRegistry) -> None:
        with pytest.raises(ValueError, match="unknown_policy"):
            AuditEventFilter(registry, unknown_policy="nope")


class TestEndToEnd:
    def test_filter_and_formatter_share_registry_and_policy(
        self, registry: SchemaRegistry, env: LoggingEnvironment
    ) -> None:
        policy = POLICY_DROP
        formatter = AuditJsonFormatter(registry=registry, unknown_policy=policy)
        audit_filter = AuditEventFilter(registry, unknown_policy=policy)
        env.handler.setFormatter(formatter)
        env.handler.addFilter(audit_filter)

        env.logger.info({"event_type": "auth.login", "user_id": "u-1", "secret": "leak"})
        env.logger.info({"event_type": "payment.charge", "amount": 12, "currency": "USD"})
        env.logger.info({"event_type": "mystery.event", "secret": "leak"})

        lines = [line for line in env.buffer.getvalue().splitlines() if line]
        assert len(lines) == 2

        login = json.loads(lines[0])
        payment = json.loads(lines[1])
        assert set(login) == {"event_type", "schema_version", "user_id"}
        assert login["schema_version"] == "auth-v1"
        assert set(payment) == {"event_type", "schema_version", "amount", "currency"}
        assert payment["schema_version"] == "payment-v3"
        assert all("secret" not in line and "leak" not in line for line in lines)

    def test_placeholder_end_to_end(
        self, registry: SchemaRegistry, env: LoggingEnvironment
    ) -> None:
        formatter = AuditJsonFormatter(
            registry=registry,
            unknown_policy=POLICY_PLACEHOLDER,
            placeholder_fields=("trace_id",),
        )
        env.handler.setFormatter(formatter)
        env.handler.addFilter(AuditEventFilter(registry, unknown_policy=POLICY_PLACEHOLDER))

        env.logger.info({"event_type": "mystery.event", "secret": "x", "trace_id": "t-9"})
        output = json.loads(env.buffer.getvalue())
        assert output == {"event_type": "mystery.event", "trace_id": "t-9"}
