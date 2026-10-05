"""The shared GenICam layer: the node-map walker and the typed accessors over a
faked GenApi, and the trigger chain over a node map that records each write."""

import pytest

from octacam.cameras.base import BackendError
from octacam.cameras.genicam import GenApi, NodeMapBackend, _snap_int


class N:
    """A fake node: its kind, value, metadata, and children for a category."""

    def __init__(self, kind, value=None, *, display=None, children=(), bounds=None,
                 entries=(), visibility="beginner", readable=True, writable=True):
        self.name = ""  # set by _nodemap
        self.kind = kind
        self.value = value
        self.display = display
        self.children = list(children)
        self.bounds = bounds or (None, None, None, None)
        self.entries = list(entries)
        self.visibility = visibility
        self.readable = readable
        self.writable = writable
        self.refuse = False  # a write the device refuses


def _nodemap(**nodes):
    for name, node in nodes.items():
        node.name = name
    return nodes


class DictGenApi(GenApi):
    """A GenApi over ``{name: N}``; ``writes`` logs each (name, kind, value)."""

    def __init__(self):
        self.writes = []

    def node(self, nodemap, name):
        return nodemap.get(name)

    def children(self, category):
        return category.children

    def kind(self, node):
        if node.kind == "broken":
            raise RuntimeError("the SDK choked on this node")
        return node.kind

    def name(self, node):
        return node.name

    def display_name(self, node):
        return node.display

    def tooltip(self, node):
        return None

    def visibility(self, node):
        return node.visibility

    def readable(self, node):
        return node.readable

    def writable(self, node):
        return node.writable

    def value(self, node, kind):
        return node.value

    def bounds(self, node, kind):
        return node.bounds

    def entries(self, node):
        return node.entries

    def write(self, node, kind, value):
        if node.refuse:
            raise BackendError(f"{node.name} refused")
        self.writes.append((node.name, kind, value))
        node.value = value

    def execute(self, node):
        self.writes.append((node.name, "command", None))


def _tree():
    nodes = _nodemap(
        Width=N("int", 640, bounds=(16, 1920, 16, "px")),
        Gain=N("float", 1.5, display="Gain (dB)"),
        Mode=N("enum", "Off", entries=[("Off", True), ("On", False)]),
        Hidden=N("int", 1, visibility="invisible"),
        Weird=N(None),
        Broken=N("broken"),
        Fire=N("command"),
    )
    image = N("category", display="Image Format", children=[nodes["Width"], nodes["Hidden"]])
    hidden = N("category", visibility="invisible", children=[nodes["Fire"]])
    nodes.update(_nodemap(
        ImageFormatControl=image,
        Secret=hidden,
        Unnamed=N("category", children=[nodes["Gain"]]),
    ))
    root_children = [image, hidden, nodes["Unnamed"], nodes["Mode"], nodes["Width"],
                     nodes["Weird"], nodes["Broken"], nodes["Fire"]]
    nodes["Root"] = N("category", children=root_children)
    return nodes


def test_the_walk_labels_each_feature_by_its_categorys_display_name():
    features = DictGenApi().walk(_tree())
    by_name = {f.name: f for f in features}
    assert [f.name for f in features] == ["Width", "Gain", "Mode", "Fire"]  # each once
    assert by_name["Width"].category == "Image Format"
    assert by_name["Gain"].category == "Unnamed"  # no display name: the name
    assert by_name["Mode"].category == "Other"  # directly under Root
    width = by_name["Width"]
    assert (width.value, width.min, width.max, width.inc, width.unit) == (640, 16, 1920, 16, "px")
    assert by_name["Gain"].display_name == "Gain (dB)"
    assert by_name["Mode"].entries == [
        {"value": "Off", "display": "Off", "available": True},
        {"value": "On", "display": "On", "available": False},
    ]
    assert by_name["Fire"].value is None  # a command has none


def test_a_walk_without_root_is_empty():
    assert DictGenApi().walk({}) == []


def test_read_feature_refuses_an_absent_node_and_a_category():
    api, nodes = DictGenApi(), _tree()
    assert api.read_feature(nodes, "Gain").value == 1.5
    with pytest.raises(BackendError, match="no such node"):
        api.read_feature(nodes, "Bogus")
    with pytest.raises(BackendError, match="not an editable feature"):
        api.read_feature(nodes, "ImageFormatControl")


def test_write_feature_coerces_to_the_nodes_kind_and_snaps_an_int():
    api = DictGenApi()
    nodes = _nodemap(
        Width=N("int", 640, bounds=(16, 1920, 16, None)),
        Gain=N("float", 0.0),
        Flag=N("bool", False),
        Mode=N("enum", "Off"),
        Fire=N("command"),
        Locked=N("int", 1, writable=False),
    )
    api.write_feature(nodes, "Width", "650.4")  # 16 + 40 * 16
    api.write_feature(nodes, "Gain", "2.5")
    api.write_feature(nodes, "Flag", "on")
    api.write_feature(nodes, "Mode", 1)
    assert api.writes == [
        ("Width", "int", 656), ("Gain", "float", 2.5), ("Flag", "bool", True), ("Mode", "enum", "1"),
    ]
    with pytest.raises(BackendError, match=r"not writable \(command\)"):
        api.write_feature(nodes, "Fire", 1)
    with pytest.raises(BackendError, match="not writable"):
        api.write_feature(nodes, "Locked", 2)


def test_run_command_executes_only_a_command():
    api = DictGenApi()
    nodes = _nodemap(Fire=N("command"), Gain=N("float", 0.0))
    api.run_command(nodes, "Fire")
    assert api.writes == [("Fire", "command", None)]
    with pytest.raises(BackendError, match="not a command"):
        api.run_command(nodes, "Gain")


def test_get_reads_none_and_put_refuses_where_the_access_mode_forbids():
    api = DictGenApi()
    nodes = _nodemap(Gain=N("float", 1.0, readable=False, writable=False))
    assert api.get(nodes, "Gain", "float") is None
    assert api.get(nodes, "Bogus", "float") is None
    for name in ("Gain", "Bogus"):
        with pytest.raises(BackendError, match="not writable"):
            api.put(nodes, name, "float", 2.0)


def test_snap_int_rounds_to_the_increment_grid():
    # An off-grid integer is snapped to the node's grid (offset from min)
    # before the SDK write, which would otherwise reject it.
    assert _snap_int(643, node_min=0, node_inc=16) == 640  # nearest multiple
    assert _snap_int(650, node_min=0, node_inc=16) == 656  # rounds up
    assert _snap_int(101, node_min=5, node_inc=16) == 101  # grid offset by min
    assert _snap_int(100, node_min=5, node_inc=16) == 101  # -> 5 + 6*16
    assert _snap_int(123.9, node_min=None, node_inc=None) == 124  # no inc: round
    assert _snap_int(200, node_min=0, node_inc=None) == 200


# ------------------------------------------------------------- trigger chain
class Recorder(NodeMapBackend):
    """An open camera over a DictGenApi node map, for the trigger chain."""

    def __init__(self, **nodes):
        super().__init__("REC", DictGenApi())
        self._nodemap = _nodemap(**nodes)

    def is_open(self):
        return True

    open = close = stop_grab = start_grab_preview = lambda self: None

    def start_grab_record(self):
        return True

    def _fire_trigger(self):
        return True

    def _fetch(self, timeout_ms, wants_array, answers_trigger):
        return None

    @property
    def writes(self):
        return [(name, value) for name, _kind, value in self._api.writes]


def _trigger_nodes(**extra):
    names = ("TriggerSelector", "TriggerMode", "TriggerSource", "AcquisitionMode")
    return {name: N("enum", "") for name in names} | extra


def test_a_software_preview_arms_the_frame_trigger_and_clears_the_rate_cap():
    camera = Recorder(
        **_trigger_nodes(TriggerOverlap=N("enum", "Off")),
        AcquisitionFrameRateEnable=N("bool", True),  # SFNC / Basler spelling
    )
    camera.begin_software_trigger_preview()
    assert camera.writes == [
        ("AcquisitionFrameRateEnable", False),
        ("TriggerSelector", "FrameStart"),
        ("TriggerMode", "On"),
        ("TriggerSource", "Software"),
        ("TriggerOverlap", "ReadOut"),
    ]


def test_the_best_effort_nodes_may_be_absent_or_refused():
    refused = N("enum", "Off")
    refused.refuse = True
    camera = Recorder(**_trigger_nodes(TriggerOverlap=refused))
    camera.enable_frame_trigger()  # no rate gate, a refused overlap: still armed
    assert camera.writes == [("TriggerSelector", "FrameStart"), ("TriggerMode", "On")]


def test_a_capped_free_run_writes_every_rate_node_it_has():
    camera = Recorder(
        **_trigger_nodes(),
        AcquisitionFrameRateEnabled=N("bool", False),  # the GS3 spelling
        AcquisitionFrameRateAuto=N("enum", "Continuous"),
        AcquisitionFrameRate=N("float", 10.0),
    )
    assert camera.begin_freerun(30) is True
    assert camera.writes == [
        ("TriggerMode", "Off"),
        ("AcquisitionMode", "Continuous"),
        ("AcquisitionFrameRateEnabled", True),
        ("AcquisitionFrameRateAuto", "Off"),
        ("AcquisitionFrameRate", 30.0),
    ]


def test_a_free_run_the_camera_refuses_reports_false():
    mode = N("enum", "On")
    mode.refuse = True
    camera = Recorder(TriggerMode=mode)
    assert camera.begin_freerun() is False and camera.writes == []
