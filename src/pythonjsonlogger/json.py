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


### CONSTANTS
### ============================================================================
JSON_DEFAULT_LIMIT_MARKER = "__json_default_limit_exceeded__"
"""Marker emitted when the per-event `json_default` conversion budget is exceeded.

The marker is followed by the object's type name, e.g.
`"__json_default_limit_exceeded__:SomeClass"`. The object itself is never
included so sensitive or unbounded values cannot leak into the output.

*New in 4.3*
"""


### CLASSES
### ============================================================================
class JsonDefaultLimitExceededError(TypeError):
    """Raised when the per-event `json_default` conversion budget is exceeded.

    Only raised when `JsonFormatter` is configured with
    `json_default_on_limit="abort"`.

    *New in 4.3*
    """


class _JsonDefaultBudget:
    """Tracks custom JSON conversion calls for a single log event.

    The budget is shared by every custom conversion in the event, including
    objects nested inside lists or dicts, multiple unrelated fields, and
    converters that keep returning new objects of the same type.
    """

    def __init__(self, max_calls: int, on_limit: str) -> None:
        self.max_calls = max_calls
        self.on_limit = on_limit
        self.calls = 0
        return

    def convert(self, obj: Any, func: Callable[[Any], Any]) -> Any:
        self.calls += 1
        if self.calls > self.max_calls:
            # Note: diagnostics must only use the type name, never `str`/`repr`
            # of the object, which could be huge or sensitive.
            if self.on_limit == "abort":
                raise JsonDefaultLimitExceededError(
                    f"json_default conversion limit of {self.max_calls} calls per log "
                    f"event exceeded while encoding object of type {type(obj).__name__!r}"
                )
            return f"{JSON_DEFAULT_LIMIT_MARKER}:{type(obj).__name__}"
        return func(obj)

    def wrap_default(self, func: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """Wrap a `json_default` function so each call consumes the budget."""
        def bounded_default(obj: Any) -> Any:
            return self.convert(obj, func)

        return bounded_default

    def wrap_encoder(self, encoder: Any) -> Any:
        """Wrap a JSON encoder class so each `default` call consumes the budget."""
        budget = self

        class BoundedJsonEncoder(encoder):
            def default(self, o: Any) -> Any:
                return budget.convert(o, super().default)

        return BoundedJsonEncoder



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
        json_default_max_calls: int | None = None,
        json_default_on_limit: str = "marker",
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
            json_default_max_calls: maximum number of custom conversion calls allowed
                per log event. The budget is shared by all objects in the event,
                including nested objects and converters that return new objects of
                the same type. `None` (the default) means no limit.
            json_default_on_limit: behaviour when `json_default_max_calls` is exceeded.
                `"marker"` (the default) replaces the object with a fixed
                `__json_default_limit_exceeded__:<type name>` marker so already
                encoded fields are still logged. `"abort"` raises
                [JsonDefaultLimitExceededError][pythonjsonlogger.json.JsonDefaultLimitExceededError]
                which aborts the log event.
            kwargs: see [BaseJsonFormatter][pythonjsonlogger.core.BaseJsonFormatter]

        *Added in 4.3*: `json_default_max_calls` and `json_default_on_limit` options
        are added.
        """
        super().__init__(*args, **kwargs)

        if json_default_max_calls is not None and json_default_max_calls < 0:
            raise ValueError("json_default_max_calls must be non-negative or None")
        if json_default_on_limit not in ("marker", "abort"):
            raise ValueError("json_default_on_limit must be 'marker' or 'abort'")

        self.json_default = json_default
        self.json_encoder = json_encoder
        self.json_serializer = json_serializer
        self.json_indent = json_indent
        self.json_ensure_ascii = json_ensure_ascii
        self.json_default_max_calls = json_default_max_calls
        self.json_default_on_limit = json_default_on_limit
        if not self.json_encoder and not self.json_default:
            self.json_encoder = JsonEncoder
        return

    def jsonify_log_record(self, log_data: core.LogData) -> str:
        """Returns a json string of the log data."""
        json_default = self.json_default
        json_encoder = self.json_encoder
        if self.json_default_max_calls is not None:
            budget = _JsonDefaultBudget(self.json_default_max_calls, self.json_default_on_limit)
            if json_default is not None:
                json_default = budget.wrap_default(json_default)
            elif json_encoder is not None:
                json_encoder = budget.wrap_encoder(json_encoder)
        return self.json_serializer(
            log_data,
            default=json_default,
            cls=json_encoder,
            indent=self.json_indent,
            ensure_ascii=self.json_ensure_ascii,
        )


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
