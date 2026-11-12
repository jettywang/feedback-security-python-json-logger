"""JSON Formatter using [`msgspec`](https://github.com/jcrist/msgspec)"""

### IMPORTS
### ============================================================================
## Future
from __future__ import annotations

## Standard Library
from typing import Any
from collections.abc import Callable

## Installed

## Application
from . import core
from . import defaults as d
from .utils import package_is_available

# We import msgspec after checking it is available
package_is_available("msgspec", throw_error=True)
import msgspec.json  # pylint: disable=wrong-import-position,wrong-import-order


### FUNCTIONS
### ============================================================================
def msgspec_default(obj: Any) -> Any:
    """msgspec default encoder function for non-standard types"""
    if d.use_exception_default(obj):
        return d.exception_default(obj)
    if d.use_traceback_default(obj):
        return d.traceback_default(obj)
    if d.use_enum_default(obj):
        return d.enum_default(obj)
    if d.use_type_default(obj):
        return d.type_default(obj)
    return d.unknown_default(obj)


### CLASSES
### ============================================================================
class MsgspecFormatter(core.BaseJsonFormatter):
    """JSON formatter using
    [`msgspec.json.Encoder`](https://jcristharif.com/msgspec/api.html#msgspec.json.Encoder)
    for encoding."""

    def __init__(
        self,
        *args,
        json_default: Callable | None = msgspec_default,
        stdlib_compat: bool = False,
        **kwargs,
    ) -> None:
        """
        Args:
            args: see [BaseJsonFormatter][pythonjsonlogger.core.BaseJsonFormatter]
            json_default: a function for encoding non-standard objects. Passing `None`
                falls back to `msgspec_default`, matching `JsonFormatter` behaviour,
                instead of enabling msgspec's strict mode.
            stdlib_compat: normalize the log record before encoding so the parsed
                output matches the standard library `JsonFormatter` contract for
                types msgspec encodes natively (datetimes use an ISO offset rather
                than `Z`, bytes use URL-safe base64, dataclasses and enums use the
                shared defaults, sets become strings).
            kwargs: see [BaseJsonFormatter][pythonjsonlogger.core.BaseJsonFormatter]
        """
        super().__init__(*args, **kwargs)

        self.json_default = json_default if json_default is not None else msgspec_default
        self.stdlib_compat = stdlib_compat
        self._encoder = msgspec.json.Encoder(enc_hook=self.json_default)
        return

    def jsonify_log_record(self, log_data: core.LogData) -> str:
        """Returns a json string of the log data."""
        if self.stdlib_compat:
            log_data = d.stdlib_compat_normalize(log_data)
        return self._encoder.encode(log_data).decode("utf8")
