"""Per-turn context for a guest run, kept task-local.

The run-turn server handles each host connection as its own asyncio task, and one
guest can be running SEVERAL turns at once: a nested spawn_agent runs while its
parent turn is suspended awaiting the broker, and a deployed team runs many turns
concurrently. Each of those needs its own op_id, tool specs, standing rules, and
active project. Module globals would have the last turn to start silently overwrite
every other turn's op_id (the gateway then rejects the stale id) — so the loop's
five shims read these values from contextvars instead.

`enter(spec, slug)` binds a turn's context and returns the tokens to `reset()` in a
finally. Because contextvars copy into the tasks a turn spawns (gather children,
brokered sub-calls), the whole turn — and only that turn — sees its own values.
"""
import contextvars

op_id: contextvars.ContextVar = contextvars.ContextVar("guest_op_id", default=None)
# The capability token proving this turn may act as its op_id. Task-local for
# exactly the reason op_id is: one guest runs several turns at once, and a
# module global would hand every turn the last-started turn's credential —
# which is the substitution the token exists to prevent, reintroduced from the
# inside. Sent with every model_call and tool_broker_call.
op_token: contextvars.ContextVar = contextvars.ContextVar("guest_op_token",
                                                          default=None)
gateway_port: contextvars.ContextVar = contextvars.ContextVar(
    "guest_gateway_port", default=5555)
specs: contextvars.ContextVar = contextvars.ContextVar("guest_tool_specs", default=())
read_only: contextvars.ContextVar = contextvars.ContextVar(
    "guest_read_only", default=frozenset())
rules: contextvars.ContextVar = contextvars.ContextVar("guest_rules", default="")
active_slug: contextvars.ContextVar = contextvars.ContextVar(
    "guest_active_slug", default=None)

_VARS = (op_id, op_token, gateway_port, specs, read_only, rules, active_slug)


def enter(spec: dict, slug: str | None) -> list:
    """Bind this turn's context; returns tokens to hand back to reset()."""
    return [
        op_id.set(spec.get("op_id")),
        op_token.set(spec.get("op_token")),
        gateway_port.set(spec.get("gateway_port") or 5555),
        specs.set(tuple(spec.get("tool_specs") or ())),
        read_only.set(frozenset(spec.get("read_only") or ())),
        rules.set(spec.get("rules", "")),
        active_slug.set(slug),
    ]


def reset(tokens: list) -> None:
    for var, tok in zip(_VARS, tokens):
        var.reset(tok)
