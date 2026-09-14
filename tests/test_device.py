import pytest
from PIL import Image

from phonepilot.cloud import Unsupported
from phonepilot.device import Device, Node, image_diff, sanitize_text, _split_percent_s
from tests.conftest import node


@pytest.fixture
def device(client, phone):
    phone.state = "ready"
    return Device(client, client.get_session("fake123"), sleep=lambda s: None)


def test_sanitize_folds_to_ascii():
    assert sanitize_text("héllo — “quotes” … naïve\n") == 'hello - "quotes" ... naive\n'
    assert sanitize_text("emoji 🎉 gone") == "emoji  gone"
    assert sanitize_text("tab\tkept? no") == "tabkept? no"  # tab is not printable ASCII per str.isprintable


def test_percent_s_is_split_so_server_accepts_it():
    assert _split_percent_s("100%s off") == ("100", "%", "s off")
    assert _split_percent_s("plain") == ("plain",)


def test_type_text_sends_chunks_and_submit(device, phone):
    sent = device.type_text("50%s é", submit=True)
    assert sent == "50%s e"
    ops = [op for op in phone.ops_log if op[0] in ("input.text", "input.keys")]
    assert ops == [("input.text", {"s": "50"}), ("input.text", {"s": "%"}), ("input.text", {"s": "s e"}), ("input.keys", {"combo": "enter"})]


def test_scroll_is_content_direction_and_swipe_is_finger_direction(device, phone):
    device.scroll("down", 0.5)
    device.swipe("down", 0.5)
    drags = [kw for op, kw in phone.ops_log if op == "input.drag"]
    assert drags[0]["y1"] > drags[0]["y2"], "scroll down = finger moves up"
    assert drags[1]["y1"] < drags[1]["y2"], "swipe down = finger moves down"
    assert drags[0]["x1"] == drags[0]["x2"] == 360


def test_tap_clamps_to_screen(device, phone):
    device.tap(-50, 99999)
    assert phone.ops_log[-1] == ("input.tap", {"x": 0, "y": 1279})


def test_key_validation(device):
    device.key("Enter")
    device.key("a")
    with pytest.raises(ValueError):
        device.key("ctrl+a")


def test_unadvertised_op_raises_without_network(client, phone):
    phone.state = "ready"
    s = client.get_session("fake123")
    d = Device(client, Session_with_ops(s, ("input.tap",)), sleep=lambda s: None)
    with pytest.raises(Unsupported):
        d.tree()
    assert not phone.ops_log


def test_tree_nodes_are_typed(device, phone):
    phone.tree = [node("Continue", res_id="com.x:id/continue_button", cls="android.widget.Button", x=360, y=980)]
    (n,) = device.tree()
    assert isinstance(n, Node) and n.label == "Continue" and n.short_id == "continue_button" and n.short_class == "Button"


def test_image_diff_zero_for_identical_and_large_for_inverted():
    a = Image.new("RGB", (72, 128), (0, 0, 0))
    b = Image.new("RGB", (72, 128), (255, 255, 255))
    assert image_diff(a, a) == 0.0
    assert image_diff(a, b) > 0.9


def test_changed_fraction_sees_a_typed_word_but_not_a_clock_tick():
    from phonepilot.device import changed_fraction

    base = Image.new("RGB", (720, 1280), (250, 250, 250))
    typed = base.copy()
    typed.paste((20, 20, 20), (140, 450, 300, 490))       # a word in a field: 160x40 px
    ticked = base.copy()
    ticked.paste((20, 20, 20), (60, 10, 68, 26))           # one clock digit: 8x16 px
    assert changed_fraction(base, base) == 0.0
    assert changed_fraction(base, typed) > 0.002
    assert changed_fraction(base, ticked) < 0.001
    assert changed_fraction(base, Image.new("RGB", (10, 10))) == 1.0
    # a colour-only highlight (same luminance-ish) still counts
    tinted = base.copy()
    tinted.paste((250, 200, 250), (500, 280, 600, 320))
    assert changed_fraction(base, tinted) > 0.002


def test_screenshot_falls_back_to_snapshot(device, phone):
    phone.fail_next_op = {"status": 500, "payload": {"error": "capture broke"}}
    img = device.screenshot()
    assert img.size == (720, 1280)


def Session_with_ops(session, ops):
    from dataclasses import replace

    return replace(session, ops=tuple(ops))
