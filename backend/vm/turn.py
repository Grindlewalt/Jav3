"""One entry point the loop callers share so the guest cutover is a per-caller
flag flip, not five bespoke wirings.

`run_agent_turn` has `run_turn`'s exact event contract (yields token / tool /
tool_result / final) and its persistence hook (`on_tool_call`). With
`use_guest_loop` off it IS `run_turn`. With it on, it runs the loop in the guest
via `guest_turn`: it builds the turn's context envelope from the ambient runtime
contextvars the caller already set (web_session / ephemeral / event_chan /
artifact_slug), and pairs the guest's tool + tool_result events to feed
`on_tool_call` (the guest loop carries no db handle, so the host sink runs here).

Nesting: if a Budget is already in scope we are inside an operation (e.g. a
brokered spawn_agent running under a guest chat) — the turn then shares that
operation's guest + Budget and does NOT re-push the workspace (its parent already
did; re-pushing would wipe the parent's in-flight staged edits). A top-level turn
pushes a fresh workspace and its edits reconcile at turn end.
"""
from ..agent.loop import run_turn
from ..config import settings


async def run_agent_turn(conversation_id, system_prompt, history, *, tools=None,
                         read_only=None, model_name=None, base_url=None,
                         self_check=True, max_iterations=None, on_tool_call=None,
                         active_project=None, rewrite_rules=True):
    if not settings.use_guest_loop:
        async for ev in run_turn(conversation_id, system_prompt, history,
                                 tools=tools, model_name=model_name,
                                 base_url=base_url, self_check=self_check,
                                 max_iterations=max_iterations,
                                 on_tool_call=on_tool_call,
                                 rewrite_rules=rewrite_rules):
            yield ev
        return

    from .. import runtime
    from ..agent import budget as budget_mod
    from ..agent.tools.registry import openai_tool_specs, read_only_names
    from ..memory import standing_rules_tail
    from . import broker
    from .guest_turn import guest_turn

    nested = budget_mod.current() is not None    # already inside an operation?
    op_id = f"guest:{conversation_id}"
    if tools is None:
        tools = openai_tool_specs()              # full host registry, like run_turn
    if read_only is None:
        read_only = list(read_only_names())
    envelope = broker.TurnEnvelope(
        op_id=op_id, conversation_id=conversation_id, active_project=active_project,
        artifact_slug=runtime.artifact_slug.get(),
        web_session=runtime.web_session.get(),
        ephemeral=runtime.ephemeral.get(), event_chan=runtime.event_chan.get())

    pending: dict = {}
    async for ev in guest_turn(
            conversation_id, system_prompt, history,
            rules=standing_rules_tail() if self_check else "",
            tool_specs=tools, read_only=read_only, op_id=op_id, envelope=envelope,
            active_slug=active_project,
            push_workspace=(not nested and bool(active_project)),
            model_name=model_name, base_url=base_url, self_check=self_check,
            max_iterations=max_iterations):
        if on_tool_call is not None:
            if ev["type"] == "tool":
                pending[ev.get("id")] = (ev.get("name"), ev.get("args") or {})
            elif ev["type"] == "tool_result":
                nm, ar = pending.pop(ev.get("id"), (ev.get("name"), {}))
                await on_tool_call(nm, ar, ev.get("result", ""))
        yield ev
