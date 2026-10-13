"""Capability checks, not a Chrys release allowlist. Never starts a model."""

import dataclasses
import inspect

from common import EvalError


def installed_version():
    import chrys
    return str(getattr(chrys, "__version__", "unknown"))


def observer_capabilities():
    try:
        from chrys.foundation.events.bus import EventBus
        from chrys.foundation.events import types
        from chrys.app.cli.app import main
        required = {
            "SessionReady": {"skill_names", "runtime_details"},
            "AgentRuntimeUpdated": {"skill_names", "runtime_details"},
            "InvocationMessage": {"origin", "text", "is_final"},
            "InvocationToolCallStart": {"origin", "tool_name", "args", "call_id"},
            "InvocationToolCallResult": {"origin", "tool_name", "result", "call_id"},
        }
        for name, fields in required.items():
            event = getattr(types, name, None)
            if event is None or not dataclasses.is_dataclass(event) or not fields <= {f.name for f in dataclasses.fields(event)}:
                raise EvalError("Chrys observer capability missing: " + name)
        if not callable(main) or not inspect.iscoroutinefunction(EventBus.publish):
            raise EvalError("Chrys CLI/event publisher capability missing")
        inspect.signature(main).bind()
        inspect.signature(EventBus.publish).bind(None, None, raise_handler_errors=False)
    except (ImportError, AttributeError, TypeError, ValueError):
        raise EvalError("Chrys CLI/event API is incompatible with this observer") from None
    return {"chrys_version": installed_version(), "observer_event_api": True}


def require_cli_options(help_text, options):
    import re
    available = set(re.findall(r"--[a-z][a-z0-9-]*", help_text))
    missing = set(options) - available
    if missing:
        raise EvalError("Chrys run lacks required options: " + ", ".join(sorted(missing)))
