#!/usr/bin/env python3
"""
Tests for event management: JotForm waivers joining the line, the public pages, and running
the line from the panel. No network or API key needed.
Run: python3 tests/test_events.py   (or: pytest tests/test_events.py)
"""
import json
import os
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient

import events
import server
from test_accounts import _owner, _setup

EVENTS = "/admin/api/businesses/glam-by-dang/events"


def _hook_path(admin) -> str:
    url = admin.get(EVENTS).json()["settings"]["webhook_url"]
    return url.removeprefix("https://itsmia.info")


def _waiver(client, hook, submission_id, first, last):
    raw = {"q3_fullName": {"first": first, "last": last}, "q4_email": "x@example.com", "q9_signature": "data:..."}
    return client.post(hook, data={"submissionID": submission_id, "formID": "1", "rawRequest": json.dumps(raw),
                                   "pretty": f"Full Name:{first} {last}"})


def test_jotform_names():
    assert events.jotform_guest_name(json.dumps({"q3_fullName": {"first": "Ana", "last": "Lima"}})) == ("Ana", "Lima")
    assert events.jotform_guest_name(json.dumps({"q5_firstName": "Bo", "q6_lastName": "Chen"})) == ("Bo", "Chen")
    assert events.jotform_guest_name(json.dumps({"q2_yourName": "Cara  de la Cruz "})) == ("Cara", "de la Cruz")
    assert events.jotform_guest_name("not json", "Full Name:Dee Ellis, Email:d@e.co") == ("Dee", "Ellis")
    assert events.jotform_guest_name(json.dumps({"q4_email": "a@b.co"})) is None
    assert events.public_name("Ana", "lima") == "Ana L." and events.public_name("Bo", "") == "Bo"
    assert events.venmo_handle("@glambydangnyc") == "glambydangnyc"
    assert events.venmo_handle("https://venmo.com/u/glambydangnyc") == "glambydangnyc"
    assert events.venmo_handle("") == ""
    try:
        events.venmo_handle("not a handle!")
        assert False, "should refuse"
    except ValueError:
        pass


def test_public_pages_only_exist_once_events_are_set_up():
    admin, _ = _setup()
    public = TestClient(server.app)
    assert public.get("/events/glam-by-dang").status_code == 404
    assert public.get("/events/glam-by-dang/api/line").status_code == 404
    assert public.get("/events/nope").status_code == 404
    admin.get(EVENTS)   # opening the Events page sets them up
    assert public.get("/events/glam-by-dang").status_code == 200
    assert public.get("/events/glam-by-dang/line").status_code == 200
    assert public.get("/events/glam-by-dang/display").status_code == 200
    qr = public.get("/events/glam-by-dang/qr.svg")
    assert qr.status_code == 200 and qr.text.startswith("<svg")


def test_waivers_join_the_line_and_the_public_sees_initials_only():
    admin, _ = _setup()
    hook = _hook_path(admin)
    public = TestClient(server.app)

    # No event running: the waiver is accepted but nobody is added
    r = _waiver(public, hook, "s1", "Ana", "Lima")
    assert r.status_code == 200 and r.json()["added"] is False
    assert public.post("/hooks/jotform/wrong-token", data={"submissionID": "x"}).status_code == 404

    assert admin.put(f"{EVENTS}/settings", json={"jotform_url": "https://form.jotform.com/123", "venmo": "@glambydangnyc"}).status_code == 200
    line = public.get("/events/glam-by-dang/api/line").json()
    assert line["event"] is None and line["links"]["join"] == ""
    assert line["links"]["tip"] == "https://venmo.com/u/glambydangnyc"
    assert line["links"]["review"].startswith("https://search.google.com/")
    # Join goes to the line page until an event is running
    assert public.get("/events/glam-by-dang/join", follow_redirects=False).headers["location"] == "/events/glam-by-dang/line"

    assert admin.post(EVENTS, json={"name": "Sarah & Tom's wedding"}).status_code == 200
    assert admin.post(EVENTS, json={"name": "Another"}).status_code == 409
    assert public.get("/events/glam-by-dang/join", follow_redirects=False).headers["location"] == "https://form.jotform.com/123"
    assert _waiver(public, hook, "s1", "Ana", "Lima").json()["added"] is True
    assert _waiver(public, hook, "s1", "Ana", "Lima").json()["added"] is False   # JotForm resent it
    assert _waiver(public, hook, "s2", "Bo", "Chen").json()["added"] is True
    assert _waiver(public, hook, "s3", "Cara", "Diaz").json()["added"] is True

    line = public.get("/events/glam-by-dang/api/line?s=s2").json()
    assert line["event"]["name"] == "Sarah & Tom's wedding"
    assert line["waiting"] == ["Ana L.", "Bo C.", "Cara D."]
    assert line["you"] == {"name": "Bo C.", "status": "waiting", "place": 2}
    assert "Lima" not in json.dumps(line) and "s1" not in json.dumps(line)
    assert public.get("/events/glam-by-dang/api/line?s=nope").json()["you"] is None

    # The thank-you page can POST the submission id; it lands on their spot
    r = public.post("/events/glam-by-dang/line", data={"submission_id": "s2"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/events/glam-by-dang/line?s=s2"


def test_running_the_line_from_the_panel():
    admin, _ = _setup()
    hook = _hook_path(admin)
    public = TestClient(server.app)
    admin.post(EVENTS, json={"name": "Party"})
    for sid, first, last in [("a", "Ana", "Lima"), ("b", "Bo", "Chen"), ("c", "Cara", "Diaz")]:
        _waiver(public, hook, sid, first, last)
    d = admin.post(f"{EVENTS}/guests", json={"first_name": "Dee", "last_name": "Ellis"}).json()
    ids = {g["first_name"]: g["id"] for g in d["guests"]}
    assert [g["walk_in"] for g in d["guests"]] == [False, False, False, True]
    assert d["guests"][0]["last_name"] == "Lima"   # the panel sees full names

    # Move Dee to the front, Ana into the chair, Bo is a no-show
    admin.post(f"{EVENTS}/order", json={"guest_ids": [ids["Dee"], ids["Ana"], ids["Bo"], ids["Cara"]]})
    admin.post(f"{EVENTS}/guests/{ids['Ana']}", json={"status": "now"})
    admin.post(f"{EVENTS}/guests/{ids['Bo']}", json={"status": "no_show"})
    line = public.get("/events/glam-by-dang/api/line?s=c").json()
    assert line["now"] == ["Ana L."] and line["waiting"] == ["Dee E.", "Cara D."]
    assert line["you"]["place"] == 2

    # A guest who joins after a reorder goes to the back
    _waiver(public, hook, "e", "Eve", "Fox")
    assert public.get("/events/glam-by-dang/api/line").json()["waiting"] == ["Dee E.", "Cara D.", "Eve F."]

    admin.post(f"{EVENTS}/guests/{ids['Ana']}", json={"status": "done"})
    admin.post(f"{EVENTS}/guests/{ids['Bo']}", json={"status": "waiting"})   # undo the no-show
    assert admin.post(f"{EVENTS}/guests/{ids['Bo']}", json={"status": "bogus"}).status_code == 400
    assert admin.delete(f"{EVENTS}/guests/{ids['Cara']}").status_code == 200
    line = public.get("/events/glam-by-dang/api/line?s=a").json()
    assert line["waiting"] == ["Dee E.", "Bo C.", "Eve F."] and line["done"] == 1
    assert line["you"]["status"] == "done"

    # Ending clears the public line; the event moves to past events
    d = admin.post(f"{EVENTS}/end").json()
    assert d["event"] is None and d["past"][0]["name"] == "Party" and d["past"][0]["done"] == 1
    line = public.get("/events/glam-by-dang/api/line").json()
    assert line["event"] is None and line["waiting"] == []
    assert admin.post(f"{EVENTS}/guests", json={"first_name": "Late"}).status_code == 409
    assert _waiver(public, hook, "z", "Zed", "Q").json()["added"] is False


def test_settings_are_checked_and_owners_only_run_their_own_events():
    admin, _ = _setup()
    assert admin.put(f"{EVENTS}/settings", json={"jotform_url": "http://insecure.example"}).status_code == 400
    assert admin.put(f"{EVENTS}/settings", json={"venmo": "two words"}).status_code == 400
    assert admin.post("/admin/api/businesses", json={"slug": "lash-lab", "name": "Lash Lab", "owner_name": "Rosa"}).status_code == 200
    kha, _ = _owner(admin)
    assert kha.get(EVENTS).status_code == 200
    assert kha.post(EVENTS, json={"name": "Kha's party"}).status_code == 200
    assert kha.get("/admin/api/businesses/lash-lab/events").status_code == 404
    assert kha.post("/admin/api/businesses/lash-lab/events", json={"name": "x"}).status_code == 404
    assert TestClient(server.app).get(EVENTS).status_code == 403


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"{len(tests)} passed")
