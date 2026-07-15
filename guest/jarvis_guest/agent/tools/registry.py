"""Guest-side `registry` shim. loop.py imports `registry` and calls
openai_tool_specs()/read_only_names()/dispatch(). The host pushes the tool-spec
snapshot in the turn spec (the guest never compiles it). M1 grants no tools, so
dispatch is never reached; M2 wires dispatch to send a tool_broker_call to the
host, and M3 runs the clean in-guest tools locally."""
_specs: list[dict] = []
_read_only: frozenset[str] = frozenset()


def set_registry(specs, read_only) -> None:
    global _specs, _read_only
    _specs = specs or []
    _read_only = frozenset(read_only or [])


def openai_tool_specs(entries=None) -> list[dict]:
    return _specs


def read_only_names(entries=None) -> frozenset[str]:
    return _read_only


async def dispatch(name: str, args: dict) -> str:
    # M2 replaces this with a host tool_broker_call; M3 runs clean tools locally.
    return (f"error: tool '{name}' is not available in the guest yet "
            "(tool brokering lands in M2).")
