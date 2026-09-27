"""A turn whose latest user message is multimodal (a screenshot re-attached as
parts) must not crash the round-1 note injection: `list + str` raised
"can only concatenate list (not "str") to list" in the guest loop."""
from backend.agent import loop


def test_note_injection_handles_list_content(monkeypatch):
    monkeypatch.setattr(loop, "_triage_note", lambda names: "TRIAGE NOTE")
    tools = [{"type": "function", "function": {"name": "research", "parameters": {}}}]
    history = [
        {"role": "user", "content": "look at this page"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [
            {"type": "text", "text": "screenshot attached"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
    ]
    messages, *_ = loop._assemble_messages("sys", history, tools, True)
    last = [m for m in messages if m["role"] == "user"][-1]
    assert isinstance(last["content"], list)
    assert last["content"][-1] == {"type": "text", "text": last["content"][-1]["text"]}
    assert "TRIAGE NOTE" in last["content"][-1]["text"]
    assert last["content"][1]["type"] == "image_url"          # the image is kept
