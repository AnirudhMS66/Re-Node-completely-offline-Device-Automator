"""The mail workflow must recognise the Gmail composer even when Gmail renames the To field."""

import time

import pytest

import config
from tests.fake_phone import FakeDevice, FakeDispatcher, Phone
from workflows import WorkflowContext, run_mail


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "MAIL_DEFAULT_RECIPIENT", "examiner@example.org")
    monkeypatch.setattr(config, "UI_WAIT_TIMEOUT_S", 0.2)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    import brain.brain as b
    monkeypatch.setattr(b.requests, "post", lambda *a, **k: (_ for _ in ()).throw(b.requests.ConnectionError("x")))


def ctx_for(phone, to_rid, to_desc):
    import main
    original = phone._screen_tree

    def patched():
        tree = original()
        for root in tree:
            stack = [root]
            while stack:
                n = stack.pop()
                if n["rid"].endswith(":id/to"):
                    n["rid"], n["desc"] = to_rid, to_desc
                stack.extend(n["children"])
        return tree
    phone._screen_tree = patched
    return WorkflowContext(device=FakeDevice(phone), dispatcher=FakeDispatcher(phone), find_element=main._find_element, capture_ui_state=main._capture_ui_state)


@pytest.mark.parametrize("to_rid,to_desc", [
    ("com.google.android.gm:id/peoplekit_autocomplete_input", ""),      # newer Gmail
    ("com.google.android.gm:id/recipient_edit_x", ""),
    ("com.google.android.gm:id/zzz_unknown", ""),                      # unknown id: topmost EditText fallback
])
def test_mail_works_when_the_to_field_is_renamed(to_rid, to_desc):
    phone = Phone()
    ctx = ctx_for(phone, to_rid, to_desc)
    assert run_mail(ctx) is True
    assert phone.sent and phone.sent[0]["to"].startswith("boss@corp.com") and "Meeting minutes" in phone.sent[0]["body"]
