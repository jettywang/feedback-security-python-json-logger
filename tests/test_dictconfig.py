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
from collections.abc import Generator

## Installed
import pytest

### SETUP
### ============================================================================
_LOGGER_COUNT = 0
EXT_VAL = 999


class Dummy:
    pass


def my_json_default(obj: Any) -> Any:
    if isinstance(obj, Dummy):
        return "DUMMY"
    return obj


LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "pythonjsonlogger.json.JsonFormatter",
            "json_default": "ext://tests.test_dictconfig.my_json_default",
            "static_fields": {"ext-val": "ext://tests.test_dictconfig.EXT_VAL"},
        }
    },
    "handlers": {
        "default": {
            "level": "DEBUG",
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",  # Default is stderr
        },
    },
    "loggers": {
        "": {"handlers": ["default"], "level": "WARNING", "propagate": False},  # root logger
    },
}


@dataclass
class LoggingEnvironment:
    logger: logging.Logger
    buffer: io.StringIO

    def load_json(self) -> Any:
        return json.loads(self.buffer.getvalue())


@pytest.fixture
def env() -> Generator[LoggingEnvironment]:
    global _LOGGER_COUNT  # pylint: disable=global-statement
    _LOGGER_COUNT += 1
    logging.config.dictConfig(LOGGING_CONFIG)
    default_formatter = logging.root.handlers[0].formatter
    logger = logging.getLogger(f"pythonjsonlogger.tests.{_LOGGER_COUNT}")
    logger.setLevel(logging.DEBUG)
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(default_formatter)
    logger.addHandler(handler)
    yield LoggingEnvironment(logger=logger, buffer=buffer)
    logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)
    buffer.close()
    return


### TESTS
### ============================================================================
def test_external_reference_support(env: LoggingEnvironment):

    assert logging.root.handlers[0].formatter.json_default is my_json_default  # type: ignore[union-attr]

    env.logger.info("hello", extra={"dummy": Dummy()})
    log_json = env.load_json()

    assert log_json["ext-val"] == EXT_VAL
    assert log_json["dummy"] == "DUMMY"
    return


def test_defaults_external_reference_support() -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "()": "pythonjsonlogger.json.JsonFormatter",
                    "defaults": {"ext-val": "ext://tests.test_dictconfig.EXT_VAL"},
                }
            },
            "handlers": {
                "default": {
                    "level": "DEBUG",
                    "formatter": "default",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                },
            },
            "loggers": {
                "": {"handlers": ["default"], "level": "WARNING", "propagate": False},
            },
        }
    )
    formatter = logging.root.handlers[0].formatter
    assert formatter.defaults["ext-val"] == EXT_VAL  # type: ignore[union-attr]
    assert type(formatter.defaults) is dict  # type: ignore[union-attr]
    return


def test_reserved_field_mapping_support() -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "()": "pythonjsonlogger.json.JsonFormatter",
                    "rename_fields": {"audit_subject": "audit.subject"},
                    "reserved_field_mapping": {"audit_subject": "audit.subject"},
                    "static_fields": {
                        "audit_subject": "ext://tests.test_dictconfig.EXT_VAL"
                    },
                }
            },
            "handlers": {
                "default": {
                    "level": "DEBUG",
                    "formatter": "default",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                },
            },
            "loggers": {
                "": {"handlers": ["default"], "level": "WARNING", "propagate": False},
            },
        }
    )
    formatter = logging.root.handlers[0].formatter
    assert formatter.reserved_field_mapping == {  # type: ignore[union-attr]
        "audit_subject": "audit.subject"
    }
    assert type(formatter.reserved_field_mapping) is dict  # type: ignore[union-attr]
    # ext:// references inside nested configuration dicts are converted
    assert formatter.static_fields["audit_subject"] == EXT_VAL  # type: ignore[union-attr]

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(formatter)
    logger = logging.getLogger("pythonjsonlogger.tests.dictconfig.reserved")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.info({"audit.subject": "untrusted"})

    log_json = json.loads(buffer.getvalue())
    assert log_json["audit.subject"] == EXT_VAL
    logger.removeHandler(handler)
    return

