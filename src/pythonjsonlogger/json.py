"""JSON formatter using the standard library's `json` for encoding.

Module contains the `JsonFormatter` and a custom `JsonEncoder` which supports a greater
variety of types.
"""

### IMPORTS
### ============================================================================
## Future
from __future__ import annotations

## Standard Library
import datetime
import json
from typing import Any
from collections.abc import Callable
import warnings

## Application
from . import core
from . import defaults as d


### CLASSES
### ============================================================================
TRUNCATED_KEY = "_truncated"
"""Key used to record truncation details when `JsonFormatter`'s `max_bytes` is enforced.

*New in 4.3*
"""

TRUNCATION_MARKER = "…"
"""Marker appended to string values truncated by `JsonFormatter`'s `max_bytes` enforcement.

*New in 4.3*
"""


class JsonEncoder(json.JSONEncoder):
    """A custom encoder extending [json.JSONEncoder](https://docs.python.org/3/library/json.html#json.JSONEncoder)"""

    def default(self, o: Any) -> Any:
        if d.use_datetime_any(o):
            return self.format_datetime_obj(o)

        if d.use_exception_default(o):
            return d.exception_default(o)

        if d.use_traceback_default(o):
            return d.traceback_default(o)

        if d.use_enum_default(o):
            return d.enum_default(o)

        if d.use_bytes_default(o):
            return d.bytes_default(o)

        if d.use_dataclass_default(o):
            return d.dataclass_default(o)

        if d.use_type_default(o):
            return d.type_default(o)

        try:
            return super().default(o)
        except TypeError:
            return d.unknown_default(o)

    def format_datetime_obj(self, o: datetime.time | datetime.date | datetime.datetime) -> str:
        """Format datetime objects found in `self.default`

        This allows subclasses to change the datetime format without understanding the
        internals of the default method.
        """
        return d.datetime_any(o)


class JsonFormatter(core.BaseJsonFormatter):
    """JSON formatter using the standard library's [`json`](https://docs.python.org/3/library/json.html) for encoding"""

    def __init__(
        self,
        *args,
        json_default: Callable | None = None,
        json_encoder: Callable | None = None,
        json_serializer: Callable = json.dumps,
        json_indent: int | str | None = None,
        json_ensure_ascii: bool = True,
        max_bytes: int | None = None,
        **kwargs,
    ) -> None:
        """
        Args:
            args: see [BaseJsonFormatter][pythonjsonlogger.core.BaseJsonFormatter]
            json_default: a function for encoding non-standard objects
            json_encoder: custom JSON encoder
            json_serializer: a [`json.dumps`](https://docs.python.org/3/library/json.html#json.dumps)-compatible callable
                that will be used to serialize the log record.
            json_indent: indent parameter for the `json_serializer`
            json_ensure_ascii: `ensure_ascii` parameter for the `json_serializer`
            max_bytes: optional per-event byte budget (UTF-8) for the formatted record,
                including `prefix`. Records over budget are shrunk by dropping non-required
                fields, then truncating string values; what was removed is recorded under
                the [`TRUNCATED_KEY`][pythonjsonlogger.json.TRUNCATED_KEY] field without
                echoing the removed values. The output is always valid JSON.
            kwargs: see [BaseJsonFormatter][pythonjsonlogger.core.BaseJsonFormatter]

        *Added in 4.3*: `max_bytes` argument.
        """
        super().__init__(*args, **kwargs)

        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")

        self.json_default = json_default
        self.json_encoder = json_encoder
        self.json_serializer = json_serializer
        self.json_indent = json_indent
        self.json_ensure_ascii = json_ensure_ascii
        self.max_bytes = max_bytes
        if not self.json_encoder and not self.json_default:
            self.json_encoder = JsonEncoder
        return

    def jsonify_log_record(self, log_data: core.LogData) -> str:
        """Returns a json string of the log data."""
        return self.json_serializer(
            log_data,
            default=self.json_default,
            cls=self.json_encoder,
            indent=self.json_indent,
            ensure_ascii=self.json_ensure_ascii,
        )

    def serialize_log_record(self, log_data: core.LogData) -> str:
        """Returns the final representation of the data to be logged.

        When `max_bytes` is set, the returned string (including `prefix`) is kept
        within that UTF-8 byte budget by dropping fields that are not required for
        incident retrieval (anything not derived from `fmt`, `defaults`,
        `static_fields`, or `timestamp`), largest first, and then truncating the
        longest string values. Details of what was removed are recorded under
        [`TRUNCATED_KEY`][pythonjsonlogger.json.TRUNCATED_KEY] using field names and
        byte counts only, never the removed values.

        If the budget is too small to hold even the truncation details, a minimal
        valid JSON object is returned instead.
        """
        if self.max_bytes is None:
            return super().serialize_log_record(log_data)

        max_bytes = self.max_bytes
        data = dict(log_data)
        original_bytes = self._serialized_size(self.prefix + self.jsonify_log_record(data))
        if original_bytes <= max_bytes:
            return self.prefix + self.jsonify_log_record(data)

        protected = self._budget_protected_fields()
        dropped: list[dict[str, Any]] = []
        truncated: dict[str, dict[str, Any]] = {}
        original_strings: dict[str, str] = {}

        def truncation_info() -> dict[str, Any]:
            info: dict[str, Any] = {"original_bytes": original_bytes}
            if dropped:
                info["dropped_fields"] = dropped
            if truncated:
                info["truncated_fields"] = list(truncated.values())
            return info

        def build_output() -> str:
            candidate = dict(data)
            candidate[TRUNCATED_KEY] = truncation_info()
            return self.prefix + self.jsonify_log_record(candidate)

        while self._serialized_size(build_output()) > max_bytes:
            # 1. Drop fields not required for incident retrieval, largest first
            droppable = [key for key in data if key not in protected]
            if droppable:
                key = max(droppable, key=lambda k: self._value_size(data[k]))
                dropped.append({"field": key, "bytes": self._value_size(data[key])})
                del data[key]
                continue

            # 2. Truncate the longest remaining string value
            string_fields = [key for key, value in data.items() if isinstance(value, str) and value]
            if string_fields:
                key = max(string_fields, key=lambda k: len(data[k].encode("utf-8")))
                original_strings.setdefault(key, data[key])
                self._truncate_string_to_fit(key, original_strings[key], data, truncation_info, max_bytes)
                value = data[key]
                kept = value[: -len(TRUNCATION_MARKER)] if value.endswith(TRUNCATION_MARKER) else value
                truncated[key] = {
                    "field": key,
                    "original_bytes": len(original_strings[key].encode("utf-8")),
                    "kept_bytes": len(kept.encode("utf-8")),
                }
                continue

            # 3. Last resort: drop the largest remaining field, even a protected one
            if data:
                key = max(data, key=lambda k: self._value_size(data[k]))
                dropped.append({"field": key, "bytes": self._value_size(data[key])})
                del data[key]
                continue

            # Budget is too small to hold anything useful; fall back to minimal valid JSON
            return self.prefix + "{}"

        return build_output()

    def _serialized_size(self, output: str) -> int:
        """Size of the formatted output in UTF-8 bytes."""
        return len(output.encode("utf-8"))

    def _value_size(self, value: Any) -> int:
        """Approximate serialized size of a single value in UTF-8 bytes."""
        return len(
            self.json_serializer(
                value,
                default=self.json_default,
                cls=self.json_encoder,
                ensure_ascii=self.json_ensure_ascii,
            ).encode("utf-8")
        )

    def _budget_protected_fields(self) -> set[str]:
        """Field names that are required for incident retrieval and dropped last."""
        protected = {self._get_rename(field) for field in self._required_fields}
        protected.update(self._get_rename(field) for field in self.defaults)
        protected.update(self._get_rename(field) for field in self.static_fields)
        if self.timestamp:
            key = self.timestamp if isinstance(self.timestamp, str) else "timestamp"
            protected.add(self._get_rename(key))
        if self.rename_fields_keep_missing:
            protected.update(self.rename_fields.values())
        return protected

    def _truncate_string_to_fit(
        self,
        key: str,
        original_value: str,
        data: dict[str, Any],
        truncation_info: Callable[[], dict[str, Any]],
        max_bytes: int,
    ) -> None:
        """Shrink `data[key]` to the longest prefix of `original_value` that fits the budget.

        Slicing is done on code points so multi-byte characters are never split and the
        output remains valid JSON. If even an empty value does not fit, the field is
        emptied and left for a later iteration to drop.
        """

        def fits(keep: int) -> bool:
            data[key] = original_value[:keep] + TRUNCATION_MARKER
            candidate = dict(data)
            candidate[TRUNCATED_KEY] = truncation_info()
            return self._serialized_size(self.prefix + self.jsonify_log_record(candidate)) <= max_bytes

        low, high = 0, len(original_value)
        best = -1
        while low <= high:
            mid = (low + high) // 2
            if fits(mid):
                best = mid
                low = mid + 1
            else:
                high = mid - 1

        if best >= 0:
            data[key] = original_value[:best] + TRUNCATION_MARKER
        else:
            data[key] = ""
        return


### DEPRECATED COMPATIBILITY
### ============================================================================
def __getattr__(name: str):
    if name == "RESERVED_ATTRS":
        warnings.warn(
            "RESERVED_ATTRS has been moved to pythonjsonlogger.core",
            DeprecationWarning,
        )
        return core.RESERVED_ATTRS
    raise AttributeError(f"module {__name__} has no attribute {name}")
