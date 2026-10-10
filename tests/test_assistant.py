#!/usr/bin/env python3
"""
Tests for the admin panel's assistant chat and business version history.
Claude is replaced by a scripted stub, so no network or API key is needed.
Run: python3 tests/test_assistant.py   (or: pytest tests/test_assistant.py)
"""
import os
import sys
from types import SimpleNamespace as NS

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import anthropic
import httpx2

import business
import server
from test_admin import KHA_PROMPT, _logged_in

PRICE_LINE = next(line for line in business.business_files("glam-by-dang")["context.md"].splitlines()
                  if "$" in line)


def text(t):
    return NS(type="text", text=t)


def tool(name, args, id="t1"):
    return NS(type="tool_use", name=name, input=args, id=id)


def reply(*blocks, stop="end_turn"):
    return NS(content=list(blocks), stop_reason=stop, usage=NS(input_tokens=10, output_tokens=5))


class StubClient:
    """Plays back scripted responses and records what each call was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self.create))

    async def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _with_stub(*responses):
    c = _logged_in()
    server.assistant.chats.clear()
    server.assistant.proposals.clear()
    stub = StubClient(*responses)
    server.assistant._client = stub
    return c, stub


def _price_edit(new="$999"):
    return {"slug": "glam-by-dang", "file": "context.md", "summary": "New price",
            "edits": [{"find": PRICE_LINE, "replace": PRICE_LINE + f" (now {new})"}]}


def test_proposal_then_apply_saves_and_records_versions():
    c, stub = _with_stub(
        reply(tool("read_business", {"slug": "glam-by-dang"}, "a"), stop="tool_use"),
        reply(tool("propose_change", _price_edit(), "b"), stop="tool_use"),
        reply(text("I proposed one change to context.md.")),
    )
    r = c.post("/admin/api/assistant", json={"message": "update the price", "viewing": "glam-by-dang"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["reply"] == "I proposed one change to context.md."
    [p] = data["proposals"]
    assert p["file"] == "context.md" and "+" in p["diff"] and "(now $999)" in p["diff"]
    # The tool results went back to the model, and the viewing note reached it
    assert "===== context.md =====" in stub.calls[1]["messages"][-1]["content"][0]["content"]
    assert "glam-by-dang" in stub.calls[0]["messages"][0]["content"][-1]["text"]
    assert stub.calls[0]["model"] == "claude-opus-5-5"
    # Nothing is saved until the admin applies it
    assert "(now $999)" not in business.business_files("glam-by-dang")["context.md"]

    r = c.post(f"/admin/api/assistant/proposals/{p['id']}/apply")
    assert r.status_code == 200, r.text
    assert "(now $999)" in business.business_files("glam-by-dang")["context.md"]
    assert c.post(f"/admin/api/assistant/proposals/{p['id']}/apply").status_code == 409

    versions = c.get("/admin/api/businesses/glam-by-dang/versions").json()["versions"]
    assert [v["note"] for v in versions] == ["Assistant: New price", "Starting point"]

    # Restoring the starting point brings Kha's prompt back exactly
    r = c.post(f"/admin/api/businesses/glam-by-dang/versions/{versions[1]['id']}/restore")
    assert r.status_code == 200, r.text
    assert business.load_business("glam-by-dang").system_prompt == KHA_PROMPT
    assert c.get("/admin/api/businesses/glam-by-dang/versions").json()["versions"][0]["note"].startswith("Restored")

    # The chat can be reloaded
    chat = c.get(f"/admin/api/assistant/{data['chat_id']}").json()
    assert [m["role"] for m in chat["transcript"]] == ["user", "assistant"]
    assert chat["proposals"][0]["status"] == "applied"


def test_bad_edits_are_refused_back_to_the_model():
    bad_find = {**_price_edit(), "edits": [{"find": "not in the file at all", "replace": "x"}]}
    twice = {**_price_edit(), "edits": [{"find": "\n", "replace": "\n\n"}]}
    placeholder = {**_price_edit(), "edits": [{"find": PRICE_LINE, "replace": "{{nope}}"}]}
    bad_json = {"slug": "glam-by-dang", "file": "business.json", "summary": "x",
                "edits": [{"find": "\"timezone\"", "replace": "timezone"}]}
    c, stub = _with_stub(
        reply(tool("propose_change", bad_find, "a"), tool("propose_change", twice, "b"),
              tool("propose_change", placeholder, "c"), tool("propose_change", bad_json, "d"),
              tool("read_business", {"slug": "nobody"}, "e"), stop="tool_use"),
        reply(text("I couldn't make those changes.")),
    )
    r = c.post("/admin/api/assistant", json={"message": "break things"})
    assert r.status_code == 200 and r.json()["proposals"] == []
    results = stub.calls[1]["messages"][-1]["content"]
    assert all(res["is_error"] for res in results)
    assert "isn't in" in results[0]["content"]
    assert "appears" in results[1]["content"]
    assert "nope" in results[2]["content"]
    assert "break the business" in results[3]["content"]
    assert "No business" in results[4]["content"]


def test_a_second_proposal_for_the_same_file_builds_on_and_replaces_the_first():
    second = {**_price_edit(), "edits": [{"find": "(now $999)", "replace": "(now $1,000)"}]}
    c, _ = _with_stub(
        reply(tool("propose_change", _price_edit(), "a"), stop="tool_use"),
        reply(tool("propose_change", second, "b"), stop="tool_use"),
        reply(text("Done.")),
    )
    data = c.post("/admin/api/assistant", json={"message": "x"}).json()
    [p] = data["proposals"]
    assert "(now $1,000)" in p["diff"]
    statuses = [q["status"] for q in c.get(f"/admin/api/assistant/{data['chat_id']}").json()["proposals"]]
    assert statuses == ["replaced", "pending"]


def test_apply_conflicts_when_the_file_changed_and_dismiss_works():
    c, _ = _with_stub(
        reply(tool("propose_change", _price_edit(), "a"), stop="tool_use"),
        reply(text("Proposed.")),
        reply(tool("propose_change", _price_edit("$5"), "a"), stop="tool_use"),
        reply(text("Proposed again.")),
    )
    data = c.post("/admin/api/assistant", json={"message": "x"}).json()
    pid = data["proposals"][0]["id"]
    detail = c.get("/admin/api/businesses/glam-by-dang").json()
    files = detail["files"]
    files["context.md"] += "\nEdited by hand.\n"
    assert c.put("/admin/api/businesses/glam-by-dang", json={"files": files}).status_code == 200
    r = c.post(f"/admin/api/assistant/proposals/{pid}/apply")
    assert r.status_code == 409 and "changed since" in r.json()["detail"]

    data = c.post("/admin/api/assistant", json={"message": "redo", "chat_id": data["chat_id"]}).json()
    pid = data["proposals"][0]["id"]
    assert c.post(f"/admin/api/assistant/proposals/{pid}/dismiss").json()["proposal"]["status"] == "dismissed"
    assert c.post(f"/admin/api/assistant/proposals/{pid}/apply").status_code == 409
    assert "(now $5)" not in business.business_files("glam-by-dang")["context.md"]


def test_pause_turn_resumes_and_refusal_is_reported():
    c, stub = _with_stub(
        reply(text("Searching..."), stop="pause_turn"),
        reply(text("The site shows no prices; paste them instead.")),
        reply(stop="refusal"),
    )
    r = c.post("/admin/api/assistant", json={"message": "check the site"}).json()
    assert r["reply"] == "The site shows no prices; paste them instead."
    assert len(stub.calls) == 2
    # The paused turn was sent back unchanged, as the last message
    assert stub.calls[1]["messages"][-1]["role"] == "assistant"
    r = c.post("/admin/api/assistant", json={"message": "x", "chat_id": r["chat_id"]}).json()
    assert r["reply"] == "I can't help with that request."


def test_images_are_sent_and_checked():
    c, stub = _with_stub(reply(text("I see the price list.")))
    img = {"media_type": "image/png", "data": "aGVsbG8="}
    assert c.post("/admin/api/assistant", json={"message": "x", "images": [img]}).status_code == 200
    assert stub.calls[0]["messages"][0]["content"][0]["type"] == "image"
    bad = {"media_type": "application/pdf", "data": "aGVsbG8="}
    assert c.post("/admin/api/assistant", json={"message": "x", "images": [bad]}).status_code == 400
    assert c.post("/admin/api/assistant", json={"message": "x", "images": [img] * 5}).status_code == 400
    assert c.post("/admin/api/assistant", json={"message": "  "}).status_code == 400


def test_api_errors_become_a_clear_message_and_admin_is_required():
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    c, _ = _with_stub(err)
    r = c.post("/admin/api/assistant", json={"message": "x"})
    assert r.status_code == 502 and "couldn't reach Claude" in r.json()["detail"]
    c.post("/admin/api/logout")
    assert c.post("/admin/api/assistant", json={"message": "x"}).status_code == 403
    assert c.post("/admin/api/assistant/proposals/1/apply").status_code == 403


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} passed")
