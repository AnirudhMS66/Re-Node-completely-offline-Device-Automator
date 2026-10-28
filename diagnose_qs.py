"""Diagnostic: swipe open quick-settings and print every clickable element found,
so we can see exactly what's in the hierarchy for the Wi-Fi tile."""

import time

from actuator.actuator import AndroidDispatcher
from perception.perception import connect_device, get_screen_hierarchy

d = connect_device()
disp = AndroidDispatcher()
w, h = d.window_size()
disp.swipe_down_from_top(w, h)
time.sleep(1.5)

state = get_screen_hierarchy(d)
print(f"Found {len(state['elements'])} elements:")
for e in state["elements"]:
    print(e)
