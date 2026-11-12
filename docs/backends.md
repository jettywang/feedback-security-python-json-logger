# Encoder Backends and Migration

The library ships one always-available backend,
[`JsonFormatter`](reference/pythonjsonlogger/json.md), built on the standard
library [`json`](https://docs.python.org/3/library/json.html) module, and two
optional backends installed separately:
[`OrjsonFormatter`](reference/pythonjsonlogger/orjson.md) and
[`MsgspecFormatter`](reference/pythonjsonlogger/msgspec.md). All backends share
the same record processing in
[`BaseJsonFormatter`](reference/pythonjsonlogger/core.md); only the final
serialization differs.

## Where the backends differ

Optional encoders have native support for several rich types. Native encoding
runs before the encoder hook, so a `json_default` function cannot change those
outputs.

| Input | `JsonFormatter` (stdlib) | `MsgspecFormatter` default |
| --- | --- | --- |
| aware UTC `datetime` | `2024-05-05T16:45:30+00:00` | `2024-05-05T16:45:30Z` |
| naive `datetime` / `date` / `time` | `isoformat()` string | same `isoformat()`-style string |
| `bytes` / `bytearray` | URL-safe base64 (`-_8=`) | standard base64 (`+/8=`) |
| dataclass instance | `dataclasses.asdict(...)` dictionary | native struct-style dictionary |
| `Enum` member / class | member value / list of values | same, when supported by msgspec |
| `uuid.UUID` | hyphenated string | same hyphenated string |
| `set` / `frozenset` | string via fallback (`"{1, 2}"`) | native JSON array |
| `BaseException` | `"ClassName: message"` plus notes | same, via shared `exception_default` |
| traceback | formatted stack string | same, via shared `traceback_default` |
| unregistered object | `__str__`, then `__repr__`, then class name | same shared `unknown_default` fallback |
| non-ASCII text | escaped by default (`ensure_ascii=True`) | raw compact UTF-8 |

Exceptions and tracebacks use the same shared functions in
[`defaults`](reference/pythonjsonlogger/defaults.md) on every backend, so the
rendered strings match. The visible schema differences are datetimes, the
base64 alphabet for bytes, and sets.

## Compatibility options

### Match the stdlib contract with msgspec

Construct the msgspec backend with `stdlib_compat=True`. The log record is
normalized before encoding using
[`stdlib_compat_normalize`](reference/pythonjsonlogger/defaults.md), so the
parsed JSON matches the stdlib backend for the types msgspec otherwise encodes
natively:

```python
from pythonjsonlogger.msgspec import MsgspecFormatter

formatter = MsgspecFormatter(stdlib_compat=True)
```

Effects:

- UTC datetimes serialize with an explicit `+00:00` offset instead of `Z`
- bytes use URL-safe base64
- dataclasses and enums go through the shared `defaults` functions
- sets and frozensets become strings, matching the stdlib fallback

### Normalize values yourself

For full control, convert values before logging (for example call
`.isoformat()` on datetimes or `base64.urlsafe_b64encode(...)` on bytes), or
override `process_log_record` to walk the record once. Native types never reach
an encoder hook, so normalization must happen before serialization.

### Accept the native contract

If downstream consumers parse the JSON and accept RFC 3339 UTC (`Z`) and
standard base64, keep the msgspec defaults and pin the native contract in your
own schema instead.

### `json_default=None` semantics

On `JsonFormatter`, omitting `json_default` installs the full shared encoder
chain. `MsgspecFormatter(json_default=None)` treats `None` the same way and
falls back to `msgspec_default` rather than enabling msgspec's strict mode,
which raises on unsupported types.

## Unregistered objects and sensitive data

The shared `unknown_default` fallback never emits the default
`object.__repr__` output. An object without its own `__str__` or `__repr__`
serializes as its class name instead of `"<module.Class object at 0x...>"`,
which would expose internal object identity. Explicitly defined `__str__` and
`__repr__` methods are still used, and objects whose custom methods raise fall
back to the class name (or `"__could_not_encode__"` for anonymous classes).
Register a dedicated `json_default` hook for any type whose string form may
contain sensitive data rather than relying on the fallback.
