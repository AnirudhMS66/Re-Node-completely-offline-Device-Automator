"""Dump ALL nodes (not just clickable=true ones) in the top header area of the
quick-settings panel, so we can find the Wi-Fi tile's actual clickable ancestor
and its exact bounds -- Realme/ColorOS doesn't expose tile labels as text on
the switch nodes themselves, so we need to inspect the raw XML directly."""

import time
import xml.etree.ElementTree as ET

from actuator.actuator import AndroidDispatcher
from perception.perception import connect_device

d = connect_device()
disp = AndroidDispatcher()
w, h = d.window_size()
disp.swipe_down_from_top(w, h)
time.sleep(1.5)

xml_dump = d.dump_hierarchy()
root = ET.fromstring(xml_dump)

# Print every node in the top ~500px (where the Wi-Fi/SIM tiles live per the screenshot)
for node in root.iter("node"):
    bounds = node.get("bounds", "")
    try:
        y1 = int(bounds.split(",")[1].split("]")[0])
    except (IndexError, ValueError):
        continue
    if y1 < 500:  # only the header area
        print(
            f"clickable={node.get('clickable')} "
            f"text={node.get('text')!r} "
            f"desc={node.get('content-desc')!r} "
            f"resource_id={node.get('resource-id')!r} "
            f"class={node.get('class')!r} "
            f"bounds={bounds}"
        )
