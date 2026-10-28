"""A tiny simulated Android phone: screens are real uiautomator-style XML, taps are hit-tested
against clickable nodes, and Wi-Fi / camera / clipboard / Gmail state is tracked, so the workflows
can be exercised end-to-end without hardware."""

from __future__ import annotations

from xml.sax.saxutils import quoteattr

W, H = 1080, 2400
GM = "com.google.android.gm"
NOTES = "com.oplus.note"
CAM = "com.oplus.camera"
UI = "com.android.systemui"


def N(pkg, cls="android.view.View", text="", desc="", rid="", clickable=False, b=(0, 0, 10, 10), on_click=None, children=()):
    return dict(pkg=pkg, cls=cls, text=text, desc=desc, rid=rid, clickable=clickable, b=b, on_click=on_click, children=list(children))


def _xml(node, idx=0):
    x1, y1, x2, y2 = node["b"]
    kids = "".join(_xml(c, i) for i, c in enumerate(node["children"]))
    return (
        f'<node index="{idx}" text={quoteattr(node["text"])} resource-id={quoteattr(node["rid"])} class={quoteattr(node["cls"])} '
        f'package={quoteattr(node["pkg"])} content-desc={quoteattr(node["desc"])} clickable="{str(node["clickable"]).lower()}" '
        f'enabled="true" focused="false" bounds="[{x1},{y1}][{x2},{y2}]">{kids}</node>'
    )


def _flatten(node):
    yield node
    for c in node["children"]:
        yield from _flatten(c)


class Phone:
    def __init__(self, notes=("Meeting minutes\nBuy milk\nSend report to boss@corp.com", "Second note"), camera_facing_after_launch="rear",
                 camera_label_mode="explicit", wifi=False, tile_taps_work=True, notes_pkg=NOTES, camera_reports_id=True, send_works=True):
        self.screen = "launcher"
        self.pkg = "com.android.launcher"
        self.notes = list(notes)
        self.open_note = 0
        self.wifi = wifi
        self.tile_taps_work = tile_taps_work
        self.qs_open = False
        self.clipboard = ""
        self.camera_facing = camera_facing_after_launch
        self.camera_label_mode = camera_label_mode
        self.camera_reports_id = camera_reports_id
        self.camera_open = False
        self.focus = None
        self.fields = {"to": "", "subject": "", "body": ""}
        self.sent: list[dict] = []
        self.wifi_at_send = None
        self.send_works = send_works
        self.log: list[str] = []
        self.installed = {NOTES, GM, CAM, "com.android.settings"} if notes_pkg == NOTES else {notes_pkg, GM, CAM}
        self.notes_pkg = notes_pkg
        self.permission_dialog = False

    # ---- screens -------------------------------------------------------------------------
    def _go(self, screen, pkg):
        self.screen, self.pkg = screen, pkg
        self.focus = None

    def _screen_tree(self):
        s = self.screen
        P = self.notes_pkg
        if s == "launcher":
            return [N("com.android.launcher", b=(0, 0, W, H))]
        if s == "notes_list":
            items = []
            for i, text in enumerate(self.notes):
                top = 300 + i * 300
                items.append(
                    N(P, "android.widget.LinearLayout", clickable=True, b=(40, top, W - 40, top + 260),
                      on_click=lambda i=i: self._open_note(i),
                      children=[N(P, "android.widget.TextView", text=text.splitlines()[0], rid=f"{P}:id/title", b=(60, top + 10, 900, top + 80)),
                                N(P, "android.widget.TextView", text="today", rid=f"{P}:id/time", b=(60, top + 100, 400, top + 160))])
                )
            return [N(P, "android.widget.FrameLayout", b=(0, 0, W, H), children=[
                N(P, "android.widget.TextView", text="Notes", rid=f"{P}:id/toolbar_title", b=(0, 80, 500, 200)),
                N(P, "androidx.recyclerview.widget.RecyclerView", rid=f"{P}:id/list", b=(0, 250, W, 2100), children=items),
                N(P, "android.widget.ImageButton", desc="Add note", rid=f"{P}:id/fab", clickable=True, b=(880, 2150, 1040, 2310), on_click=lambda: None),
            ])]
        if s == "note":
            return [N(P, "android.widget.FrameLayout", b=(0, 0, W, H), children=[
                N(P, "android.widget.ImageButton", desc="Navigate up", clickable=True, b=(0, 80, 120, 200), on_click=lambda: self._go("notes_list", P)),
                N(P, "android.widget.EditText", text=self.notes[self.open_note], rid=f"{P}:id/content", b=(40, 300, W - 40, 1800)),
            ])]
        if s == "gmail_inbox":
            return [N(GM, "android.widget.FrameLayout", b=(0, 0, W, H), children=[
                N(GM, "android.widget.TextView", text="Inbox", b=(0, 80, 400, 200)),
                N(GM, "android.widget.Button", text="Compose", desc="Compose", rid=f"{GM}:id/compose_button", clickable=True, b=(700, 2100, 1040, 2260), on_click=lambda: self._go("gmail_compose", GM)),
            ])]
        if s == "gmail_compose":
            return [N(GM, "android.widget.FrameLayout", b=(0, 0, W, H), children=[
                N(GM, "android.widget.ImageButton", desc="Send", rid=f"{GM}:id/send", clickable=True, b=(800, 80, 920, 200), on_click=self._send),
                N(GM, "android.widget.EditText", text=self.fields["to"], desc="To", rid=f"{GM}:id/to", clickable=True, b=(100, 260, W, 380), on_click=lambda: setattr(self, "focus", "to")),
                N(GM, "android.widget.EditText", text=self.fields["subject"] or "Subject", rid=f"{GM}:id/subject", clickable=True, b=(40, 420, W, 540), on_click=lambda: setattr(self, "focus", "subject")),
                N(GM, "android.widget.EditText", text=self.fields["body"] or "Compose email", rid=f"{GM}:id/wc_body", clickable=True, b=(40, 600, W, 2000), on_click=lambda: setattr(self, "focus", "body")),
            ])]
        if s == "camera":
            if self.camera_label_mode == "explicit":
                desc = "Switch to front camera" if self.camera_facing == "rear" else "Switch to rear camera"
            else:
                desc = "Switch camera"
            nodes = [N(CAM, "android.widget.ImageView", desc="Shutter", clickable=True, b=(440, 2000, 640, 2200), on_click=lambda: None),
                     N(CAM, "android.widget.ImageView", desc=desc, rid=f"{CAM}:id/camera_switch_btn", clickable=True, b=(800, 2000, 960, 2200), on_click=self._flip)]
            if self.permission_dialog:
                nodes.append(N("com.google.android.permissioncontroller", "android.widget.Button", text="While using the app", clickable=True, b=(100, 1200, 980, 1300), on_click=self._grant))
            return [N(CAM, "android.widget.FrameLayout", b=(0, 0, W, H), children=nodes)]
        return []

    def _qs_tree(self):
        return [N(UI, "android.widget.FrameLayout", b=(0, 0, W, 900), children=[
            N(UI, "android.widget.Switch", text="On" if self.wifi else "Off", desc="Wi-Fi", clickable=True, b=(40, 200, 340, 400), on_click=self._tile),
            N(UI, "android.widget.Switch", text="Off", desc="Bluetooth", clickable=True, b=(380, 200, 680, 400), on_click=lambda: None),
        ])]

    def tree(self):
        roots = self._screen_tree()
        if self.qs_open:
            roots = roots + self._qs_tree()
        return roots

    # ---- state transitions -----------------------------------------------------------------
    def _open_note(self, i):
        self.open_note = i
        self._go("note", self.notes_pkg)

    def _tile(self):
        if self.tile_taps_work:
            self.wifi = not self.wifi
            self.log.append(f"tile->{'on' if self.wifi else 'off'}")

    def _flip(self):
        self.camera_facing = "front" if self.camera_facing == "rear" else "rear"
        self.log.append(f"flip->{self.camera_facing}")

    def _grant(self):
        self.permission_dialog = False

    def _send(self):
        if not self.send_works:
            return
        self.wifi_at_send = self.wifi
        self.sent.append(dict(self.fields))
        self.fields = {"to": "", "subject": "", "body": ""}
        self._go("gmail_inbox", GM)

    # ---- device-side helpers --------------------------------------------------------------
    def hit(self, x, y):
        best = None
        for root in self.tree():
            for n in _flatten(root):
                x1, y1, x2, y2 = n["b"]
                if n["clickable"] and x1 <= x <= x2 and y1 <= y <= y2:
                    area = (x2 - x1) * (y2 - y1)
                    if best is None or area <= best[0]:
                        best = (area, n)
        return best[1] if best else None

    def set_field(self, text, append=False):
        if self.screen == "gmail_compose" and self.focus in self.fields:
            self.fields[self.focus] = (self.fields[self.focus] + text) if append else text
            return True
        return False


class FakeUiObject:
    def __init__(self, phone): self.phone = phone
    def set_text(self, text):
        if not self.phone.set_field(text):
            raise RuntimeError("no focused editable field")


class FakeDevice:
    def __init__(self, phone): self.phone = phone; self.info = {"productName": "fake", "displayWidth": W, "displayHeight": H, "sdkInt": 34}
    def dump_hierarchy(self): return "<hierarchy>" + "".join(_xml(r) for r in self.phone.tree()) + "</hierarchy>"
    def window_size(self): return (W, H)
    def app_current(self): return {"package": self.phone.pkg, "activity": ".Main"}
    def __call__(self, **kw): return FakeUiObject(self.phone)
    def set_clipboard(self, text, label=None): self.phone.clipboard = text
    def send_keys(self, text, clear=False): self.phone.set_field(text)
    def open_quick_settings(self): self.phone.qs_open = True
    def screenshot(self, path): pass

    def app_start(self, package, activity=None, wait=False, stop=False, use_monkey=False):
        p = self.phone
        p.log.append(f"start {package}")
        p.qs_open = False
        if package == p.notes_pkg: p._go("notes_list", package)
        elif package == GM: p._go("gmail_inbox", package)
        elif package == CAM: p._go("camera", package); p.camera_open = True

    def press(self, key):
        p = self.phone
        if key == "home": p.qs_open = False; p._go("launcher", "com.android.launcher"); p.camera_open = False
        elif key == "back": p.qs_open = False


class FakeDispatcher:
    def __init__(self, phone): self.phone = phone; self.taps = []
    def tap(self, x, y):
        self.taps.append((x, y))
        n = self.phone.hit(x, y)
        if n and n["on_click"]: n["on_click"]()
    def type_text(self, text): self.phone.set_field(text, append=True)
    def keyevent(self, key):
        if key == 279: self.phone.set_field(self.phone.clipboard)
    def swipe_down_from_top(self, w, h): self.phone.qs_open = True
    def swipe_up_to_close(self, w, h): self.phone.qs_open = False
    def swipe(self, *a, **k): pass
    def installed_packages(self): return set(self.phone.installed)
    def wifi_enabled(self): return self.phone.wifi
    def set_wifi(self, enable): self.phone.wifi = enable; self.phone.log.append(f"svc wifi {'enable' if enable else 'disable'}")
    def active_camera_id(self):
        p = self.phone
        if not (p.camera_open and p.camera_reports_id): return None
        return "1" if p.camera_facing == "front" else "0"
    def start_intent_with_extras(self, action, extras):
        p = self.phone
        p.camera_open = True; p._go("camera", CAM)
    def shell(self, *args):
        p = self.phone
        if args[:2] == ("cmd", "statusbar"): p.qs_open = True
        elif args[0] == "am" and "mailto:" in args: p._go("gmail_compose", GM)
        return ""
