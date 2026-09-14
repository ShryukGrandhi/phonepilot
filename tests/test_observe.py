from PIL import Image

from phonepilot.device import Node
from phonepilot.observe import Observation, draw_marks, select_elements
from tests.conftest import node


def N(**kw):
    return Node.from_json(node(**kw))


def test_select_elements_filters_noise_and_orders_reading_order():
    nodes = [
        N(text="", desc="", clickable=False),               # nothing to act on
        N(text="tiny", w=2, h=2),                              # too small
        N(text="off", x=5000, y=5000),                         # off screen
        N(text="B", x=400, y=300),
        N(text="A", x=100, y=300),
        N(text="A", x=100, y=300),                             # duplicate
        N(text="C", x=100, y=900),
    ]
    els = select_elements(nodes, (720, 1280))
    assert [e.node.text for e in els] == ["A", "B", "C"]
    assert [e.index for e in els] == [1, 2, 3]


def test_select_elements_caps_count():
    nodes = [N(text=str(i), y=10 + i * 15) for i in range(100)]
    assert len(select_elements(nodes, (720, 1280), max_n=20)) == 20


def test_describe_and_summary_mention_index_and_center():
    els = select_elements([N(text="Continue", res_id="app:id/go", x=360, y=980, w=240, h=64)], (720, 1280))
    line = els[0].describe()
    assert line.startswith("[1] Button text='Continue' id=go clickable @(360,980) 240x64")
    img = Image.new("RGB", (720, 1280))
    obs = Observation(1, img, img, els, "com.x", (720, 1280), 120.0)
    assert "Foreground app: com.x" in obs.summary() and "[1]" in obs.summary()
    assert obs.element(1) is els[0]


def test_element_lookup_error_lists_visible_indices():
    img = Image.new("RGB", (720, 1280))
    obs = Observation(1, img, img, select_elements([N(text="x")], (720, 1280)), None, (720, 1280), None)
    try:
        obs.element(9)
    except KeyError as exc:
        assert "[1]" in str(exc)
    else:
        raise AssertionError("expected KeyError")


def test_draw_marks_returns_new_image_and_changes_pixels():
    img = Image.new("RGB", (720, 1280), (0, 0, 0))
    els = select_elements([N(text="Go", x=360, y=640, w=200, h=80)], (720, 1280))
    marked = draw_marks(img, els)
    assert marked is not img
    assert img.getpixel((260, 600)) == (0, 0, 0)
    assert marked.getpixel((260, 600)) != (0, 0, 0)
