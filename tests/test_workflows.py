import time

import pytest

import config
from tests.fake_phone import CAM, GM, FakeDevice, FakeDispatcher, Phone
from perception.perception import dump_nodes, extract_note_text, find_by_chain, find_first_list_item
import workflows
from workflows import WorkflowContext, match_workflow, run_mail, run_mirror


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "MAIL_DEFAULT_RECIPIENT", "examiner@example.org")
    monkeypatch.setattr(config, "UI_WAIT_TIMEOUT_S", 0.2)
    # no Ollama in tests -> brain must fall back cleanly, never raise
    import brain.brain as b
    monkeypatch.setattr(b.requests, "post", lambda *a, **k: (_ for _ in ()).throw(b.requests.ConnectionError("no ollama")))


def make_ctx(phone):
    import main
    dev, disp = FakeDevice(phone), FakeDispatcher(phone)
    return WorkflowContext(device=dev, dispatcher=disp, find_element=main._find_element, capture_ui_state=main._capture_ui_state), disp


# ---------------- command gate ----------------
@pytest.mark.parametrize("cmd,expected", [
    ("mirror", "MIRROR"), ("Mirror", "MIRROR"), ("  open the mirror ", "MIRROR"),
    ("mail", "MAIL"), ("MAIL", "MAIL"), ("send mail", "MAIL"), ("email", "MAIL"),
    ("mirror legacy", None), ("turn on wifi", None), ("read my mail inbox", None), ("go home", None), ("", None),
])
def test_match_workflow(cmd, expected):
    assert match_workflow(cmd) == expected


def test_existing_skills_untouched():
    from skills import match_known_skill
    assert match_known_skill("turn on wifi") == "toggle_wifi"
    assert match_known_skill("mirror legacy") == "open_selfie_camera"
    assert match_known_skill("go back") == "go_back"


# ---------------- perception helpers ----------------
def test_note_parsing_and_first_item():
    phone = Phone(); ctx, _ = make_ctx(phone)
    phone.screen = "notes_list"; phone.pkg = phone.notes_pkg
    nodes = dump_nodes(ctx.device)
    first = find_first_list_item(nodes, pkg=phone.notes_pkg)
    assert first is not None and first["y"] < 600          # first note, not the FAB or 2nd item
    phone.screen = "note"
    assert extract_note_text(dump_nodes(ctx.device), pkg=phone.notes_pkg).startswith("Meeting minutes\nBuy milk")


def test_find_by_chain_prefers_specific():
    phone = Phone(); ctx, _ = make_ctx(phone); phone.screen = "camera"; phone.pkg = CAM
    node, sel = find_by_chain(dump_nodes(ctx.device), workflows._FLIP_CHAIN)
    assert "camera_switch_btn" in node["resource_id"]


# ---------------- mirror ----------------
def test_mirror_flips_rear_to_front():
    phone = Phone(); ctx, _ = make_ctx(phone)
    assert run_mirror(ctx) is True
    assert phone.camera_facing == "front" and phone.log.count("flip->front") == 1


def test_mirror_already_front_does_not_flip():
    phone = Phone(camera_facing_after_launch="front"); ctx, _ = make_ctx(phone)
    assert run_mirror(ctx) is True
    assert phone.camera_facing == "front" and not any(l.startswith("flip") for l in phone.log)


def test_mirror_without_dumpsys_uses_label():
    phone = Phone(camera_reports_id=False); ctx, _ = make_ctx(phone)
    assert run_mirror(ctx) is True and phone.camera_facing == "front"
    phone = Phone(camera_reports_id=False, camera_facing_after_launch="front"); ctx, _ = make_ctx(phone)
    assert run_mirror(ctx) is True and phone.camera_facing == "front" and not phone.log.count("flip->rear")


def test_mirror_generic_label_no_dumpsys_policy_tap_and_skip(monkeypatch):
    phone = Phone(camera_reports_id=False, camera_label_mode="generic"); ctx, _ = make_ctx(phone)
    assert run_mirror(ctx) is True and phone.camera_facing == "front"      # default: assume rear, tap
    monkeypatch.setattr(config, "MIRROR_UNKNOWN_FACING_ACTION", "skip")
    phone = Phone(camera_reports_id=False, camera_label_mode="generic"); ctx, _ = make_ctx(phone)
    assert run_mirror(ctx) is True and phone.camera_facing == "rear"       # skip: untouched


def test_mirror_dismisses_permission_dialog():
    phone = Phone(); phone.permission_dialog = True; ctx, _ = make_ctx(phone)
    assert run_mirror(ctx) is True and not phone.permission_dialog


# ---------------- mail ----------------
def test_mail_full_sequence_note_has_email():
    phone = Phone(); ctx, _ = make_ctx(phone)
    assert run_mail(ctx) is True
    assert len(phone.sent) == 1
    mail = phone.sent[0]
    assert "boss@corp.com" in mail["to"]                                 # parsed from note
    assert mail["body"] == "Meeting minutes\nBuy milk\nSend report to boss@corp.com"
    assert mail["subject"] == "Meeting minutes"
    assert phone.clipboard == mail["body"]
    assert phone.wifi_at_send is True                                    # Wi-Fi was ON when Send happened
    assert phone.wifi is False                                           # ...and is OFF afterwards
    assert phone.pkg == "com.android.launcher"                           # ended on Home
    assert not phone.qs_open


def test_mail_uses_default_recipient_when_note_has_none():
    phone = Phone(notes=("Shopping list\nEggs",)); ctx, _ = make_ctx(phone)
    assert run_mail(ctx) is True
    assert phone.sent[0]["to"].startswith("examiner@example.org")


def test_mail_rejects_placeholder_recipient(monkeypatch):
    monkeypatch.setattr(config, "MAIL_DEFAULT_RECIPIENT", "your.recipient@example.com")
    phone = Phone(notes=("Shopping list\nEggs",)); ctx, _ = make_ctx(phone)
    assert run_mail(ctx) is False
    assert phone.sent == [] and phone.wifi is False                      # never got as far as enabling Wi-Fi


def test_mail_wifi_failsafe_when_tile_tap_does_nothing():
    phone = Phone(tile_taps_work=False); ctx, _ = make_ctx(phone)
    assert run_mail(ctx) is True
    assert "svc wifi enable" in phone.log and "svc wifi disable" in phone.log
    assert phone.wifi_at_send is True and phone.wifi is False


def test_mail_forces_wifi_off_if_crash_after_enable():
    phone = Phone(send_works=False); ctx, _ = make_ctx(phone)
    ctx.dispatcher.tap  # noqa
    # make Send blow up after Wi-Fi is on
    orig = workflows._wait_for
    def boom(c, chain, *a, **k):
        if chain is workflows._SEND_CHAIN: raise RuntimeError("simulated crash")
        return orig(c, chain, *a, **k)
    workflows._wait_for = boom
    try:
        assert run_mail(ctx) is False
    finally:
        workflows._wait_for = orig
    assert phone.wifi is False                                           # air-gap restored by the finally block


def test_mail_no_notes_app():
    phone = Phone(); phone.installed.discard(phone.notes_pkg); ctx, _ = make_ctx(phone)
    assert run_mail(ctx) is False


def test_mail_other_notes_package_autodetected():
    phone = Phone(notes_pkg="com.miui.notes"); ctx, _ = make_ctx(phone)
    assert run_mail(ctx) is True and len(phone.sent) == 1


def test_brain_fallback_is_labelled(caplog):
    from brain.brain import reason_about_command
    r = reason_about_command("mail", expected_intent="MAIL")
    assert r["source"] == "deterministic-fallback" and r["intent"] == "MAIL" and len(r["plan"]) >= 5


def test_brain_parses_ollama_json(monkeypatch):
    import brain.brain as b
    class R:
        def raise_for_status(self): pass
        def json(self): return {"response": '{"intent":"mirror","confidence":0.9,"reasoning":"selfie view","plan":["a","b","c"]}'}
    monkeypatch.setattr(b.requests, "post", lambda *a, **k: R())
    r = b.reason_about_command("mirror", expected_intent="MIRROR")
    assert r["intent"] == "MIRROR" and r["source"].startswith("ollama:") and r["plan"] == ["a", "b", "c"]
