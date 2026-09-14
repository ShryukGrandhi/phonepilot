"""ADB transport: pure parts (hierarchy parsing, text escaping) and the device surface with a stubbed shell."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from phonepilot.adb import AdbDevice, AdbError, escape_for_input, parse_uiautomator
from phonepilot.cloud import Session

XML = """UI hierchary dumped to: /dev/tty
<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.android.deskclock" content-desc="" clickable="false" bounds="[0,0][720,1280]">
    <node index="0" text="6" resource-id="" class="android.widget.TextView" package="com.android.deskclock" content-desc="" clickable="true" bounds="[320,816][400,896]"/>
    <node index="1" text="" resource-id="com.android.deskclock:id/fab" class="android.widget.ImageButton" package="com.android.deskclock" content-desc="Add alarm" clickable="true" bounds="[304,1040][416,1152]"/>
    <node index="2" text="skip" resource-id="" class="android.view.View" content-desc="" clickable="false" bounds="broken"/>
  </node>
</hierarchy>
"""


def test_parse_uiautomator_flattens_with_centers():
    nodes = parse_uiautomator(XML)
    assert [n.label for n in nodes] == ["", "6", "Add alarm"]
    six = nodes[1]
    assert (six.x, six.y, six.w, six.h) == (360, 856, 80, 80) and six.clickable
    assert nodes[2].short_id == "fab" and nodes[2].short_class == "ImageButton"
    assert parse_uiautomator("garbage") == ()


def test_escape_for_input():
    assert escape_for_input("Ada Lovelace") == "Ada%sLovelace"
    assert escape_for_input("a&b (c) 'd' $e") == "a\\&b%s\\(c\\)%s\\'d\\'%s\\$e"


class FakeTunnel:
    serial = "127.0.0.1:15555"


def make_device(monkeypatch, responses: dict[str, str | bytes]):
    session = Session.from_json({"id": "s1", "state": "ready", "screen": {"w": 720, "h": 1280}, "ops": [], "expires_at": 9e9})
    dev = AdbDevice(FakeTunnel(), session, sleep=lambda s: None)
    calls: list[str] = []

    def shell(cmd, timeout=60.0, binary=False):
        calls.append(cmd)
        for prefix, value in responses.items():
            if cmd.startswith(prefix):
                return value
        return b"" if binary else ""

    dev.shell = shell  # type: ignore[assignment]
    return dev, calls


def test_device_commands_map_to_adb_input(monkeypatch):
    buf = io.BytesIO()
    Image.new("RGB", (720, 1280), (9, 9, 9)).save(buf, format="PNG")
    dev, calls = make_device(monkeypatch, {
        "screencap": buf.getvalue(),
        "uiautomator dump": XML.encode("utf-8"),
        "dumpsys activity": "    topResumedActivity=ActivityRecord{abc u0 com.android.deskclock/.DeskClock t12}",
        "pm list packages": "package:com.android.settings\npackage:app.lawnchair\n",
        "cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER com.android.deskclock":
            "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\ncom.android.deskclock/.DeskClock\n",
        "cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER com.nope": "No activity found\n",
        "am start -W -n com.android.deskclock/.DeskClock": "Status: ok\nLaunchState: COLD\n",
    })
    assert dev.screenshot().size == (720, 1280)
    assert dev.tree()[1].text == "6"
    assert dev.current_app() == "com.android.deskclock"
    assert dev.apps(include_system=True) == ("app.lawnchair", "com.android.settings")
    dev.tap(-5, 99999)
    dev.long_press(10, 10, 1.2)
    dev.scroll("down", 0.5)
    dev.type_text("hi there", submit=True)
    dev.key("Enter")
    dev.home()
    assert dev.launch("com.android.deskclock") == "com.android.deskclock"
    with pytest.raises(AdbError):
        dev.launch("com.nope")
    with pytest.raises(ValueError):
        dev.key("ctrl+a")
    assert "input tap 0 1279" in calls
    assert "input swipe 10 10 10 10 1200" in calls
    swipes = [c.split() for c in calls if c.startswith("input swipe 360 ")]
    assert swipes and swipes[0][2] == swipes[0][4] == "360" and int(swipes[0][3]) > int(swipes[0][5]), "scroll down = finger moves up"
    assert "input text hi%sthere" in calls and calls.count("input keyevent KEYCODE_ENTER") == 2
    assert "input keyevent KEYCODE_HOME" in calls
