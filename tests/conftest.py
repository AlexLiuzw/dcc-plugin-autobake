"""Shared fixtures + a deliberately small fake Blender host.

The fake host is a set of real module objects (``bpy``, ``bpy.types``,
``bpy.props``, ``bpy.utils``, ``gpu_extras.presets``) because the vendored
add-on imports from those paths. ``bpy.utils.register_class`` actually
records what it registers and publishes operator ids onto ``bpy.ops``, so a
test can check registration state rather than trusting a flag.

It is *not* a Blender emulator: it implements only the surface the auto-bake
code touches. Anything else raises, which means a tool reaching for an API we
did not model fails loudly instead of silently passing.

``bpy.ops.object.bake`` writes a deterministic gradient into the active image
node and renames the datablock, so the PNG on disk has real bytes, real
dimensions, and different content per map — the assertions about file size
and dimensions are then testing something.
"""

from __future__ import annotations

import io
import struct
import sys
import types as _types
import zlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# images / materials / objects
# ---------------------------------------------------------------------------


class FakeImage:
    def __init__(self, name, width, height):
        self.name = name
        self.size = (int(width), int(height))
        self.filepath_raw = ""
        self.file_format = ""
        self.fill = 0

    def save(self):
        if not self.filepath_raw:
            raise RuntimeError("no filepath_raw set")
        Path(self.filepath_raw).write_bytes(self._png())

    def _png(self):
        width, height = self.size
        rows = []
        for y in range(height):
            row = bytearray([0])
            for x in range(width):
                value = (x * 7 + y * 13 + self.fill) & 0xFF
                row.extend((value, 255 - value, (value * 3) & 0xFF, 255))
            rows.append(bytes(row))
        raw = b"".join(rows)

        def chunk(tag, payload):
            return (
                struct.pack(">I", len(payload))
                + tag
                + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
            )

        header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b"")
        )


class FakeImages:
    def __init__(self):
        self._items = {}

    def new(self, name, width, height):
        unique = name
        index = 1
        while unique in self._items:
            unique = "{0}.{1:03d}".format(name, index)
            index += 1
        image = FakeImage(unique, width, height)
        self._items[unique] = image
        return image

    def get(self, name):
        return self._items.get(name)

    def __iter__(self):
        return iter(list(self._items.values()))


class FakeSocket:
    def __init__(self, name="socket"):
        self.name = name
        self.default_value = 0.0


#: Blender's ``node.type`` values for the node ids the add-on creates.
_NODE_TYPES = {
    "ShaderNodeBsdfDiffuse": "BSDF_DIFFUSE",
    "ShaderNodeBsdfGlossy": "BSDF_GLOSSY",
    "ShaderNodeOutputMaterial": "OUTPUT_MATERIAL",
    "ShaderNodeBevel": "BEVEL",
    "ShaderNodeTexImage": "TEX_IMAGE",
    "ShaderNodeAmbientOcclusion": "AMBIENT_OCCLUSION",
    "ShaderNodeMath": "MATH",
}


class FakeNode:
    def __init__(self, node_id):
        # ``node.type`` is the enum ("TEX_IMAGE"), not the id ("ShaderNodeTexImage");
        # the add-on compares against the enum in every branch.
        self.type = _NODE_TYPES.get(node_id, node_id)
        self.image = None
        self.select = False
        self.location = (0, 0)
        self.inputs = [FakeSocket("in{0}".format(i)) for i in range(4)]
        self.outputs = [FakeSocket("out{0}".format(i)) for i in range(3)]
        self.samples = 0
        self.operation = "ADD"
        self.name = node_id


class FakeNodes:
    def __init__(self):
        self._nodes = []
        self.active = None

    def new(self, node_type):
        node = FakeNode(node_type)
        self._nodes.append(node)
        return node

    def clear(self):
        self._nodes = []
        self.active = None

    def __iter__(self):
        return iter(list(self._nodes))

    def __len__(self):
        return len(self._nodes)


class FakeLinks:
    def __init__(self):
        self.created = []

    def new(self, source, target):
        self.created.append((source, target))
        return None


class FakeNodeTree:
    def __init__(self):
        self.nodes = FakeNodes()
        self.links = FakeLinks()


class FakeMaterial:
    def __init__(self, name):
        self.name = name
        self.use_nodes = True
        self.node_tree = FakeNodeTree()


class FakeMaterials(list):
    def new(self, name):
        material = FakeMaterial(name)
        self.append(material)
        return material


class FakeProps:
    """Stand-in for the upstream ``Scene.at_tool`` / ``Object.at_objtool`` groups."""

    def __init__(self, **values):
        self.__dict__.update(values)


class FakeObject:
    def __init__(self, name, obj_type="MESH"):
        self.name = name
        self.type = obj_type
        self.selected = False
        self.materials = FakeMaterials()

    @property
    def active_material(self):
        return self.materials[0] if self.materials else None

    def select_set(self, value):
        self.selected = bool(value)

    @property
    def data(self):
        return _types.SimpleNamespace(materials=self.materials)


class FakeObjects(dict):
    def __iter__(self):
        return iter(list(self.values()))


# ---------------------------------------------------------------------------
# render settings
# ---------------------------------------------------------------------------


class FakeBakeSettings:
    use_selected_to_active = False
    normal_g = "POS_Y"
    use_pass_direct = True
    use_pass_indirect = True


class FakeRender:
    def __init__(self):
        self.engine = "BLENDER_EEVEE_NEXT"
        self.bake = FakeBakeSettings()
        self.use_bake_multires = True


class FakeCycles:
    def __init__(self, with_feature_set):
        self.bake_type = "COMBINED"
        self.device = "CPU"
        if with_feature_set:
            self.feature_set = "EXPERIMENTAL"


class FakeSceneInstance:
    def __init__(self, with_legacy_knobs=True):
        self.render = FakeRender()
        self.cycles = FakeCycles(with_feature_set=with_legacy_knobs)
        if not with_legacy_knobs:
            del self.render.use_bake_multires
        self.at_tool = FakeProps(
            image_width_prop_int=64,
            image_height_prop_int=64,
            bevel_samples_prop_int=8,
            bevel_radius_prop_float=0.02,
            at_ao_distance=0.1,
            at_ao_samples=8,
            at_ao_exponentiation=1.0,
            at_dx_normal=False,
            at_glossy_preview=False,
            at_image_name="",
        )


# ---------------------------------------------------------------------------
# bpy modules
# ---------------------------------------------------------------------------


def _make_types_module():
    module = _types.ModuleType("bpy.types")

    class Operator:
        bl_idname = ""
        bl_label = ""

        @classmethod
        def poll(cls, context):
            return True

    class Panel:
        bl_idname = ""
        bl_label = ""
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "Misc"

        @classmethod
        def poll(cls, context):
            return True

    class Menu:
        bl_idname = ""

    class UIList:
        bl_idname = ""

    class PropertyGroup:
        pass

    class Scene:
        pass

    class Object:
        pass

    module.Operator = Operator
    module.Panel = Panel
    module.Menu = Menu
    module.UIList = UIList
    module.PropertyGroup = PropertyGroup
    module.Scene = Scene
    module.Object = Object
    return module


def _make_props_module():
    module = _types.ModuleType("bpy.props")

    def _property(*_args, **_kwargs):
        # The vendored add-on only creates these at class-definition time; the
        # fake host reads its settings from ``FakeProps`` instead, so the
        # returned marker never has to behave like a real RNA property.
        return None

    for name in (
        "IntProperty",
        "FloatProperty",
        "BoolProperty",
        "StringProperty",
        "PointerProperty",
        "EnumProperty",
        "CollectionProperty",
    ):
        setattr(module, name, _property)
    return module


class FakeOps:
    """``bpy.ops`` — only the operators the plugin and the add-on use."""

    def __init__(self, host):
        self._host = host
        self.object = _types.SimpleNamespace(bake=self._bake)
        self.image = _types.SimpleNamespace(new=self._image_new)
        self.wm = _types.SimpleNamespace(window_new=lambda **k: {"FINISHED"})

    def _image_new(self, name, width, height, **_kwargs):
        self._host.data.images.new(name, int(width), int(height))
        return {"FINISHED"}

    def _bake(self, type="NORMAL", use_clear=True, **_kwargs):
        host = self._host
        host.bake_calls.append({"type": type, "use_clear": use_clear})
        obj = host.context.active_object
        if obj is None:
            raise RuntimeError("nothing to bake: no active object")
        material = obj.active_material
        target = material.node_tree.nodes.active
        if target is None or target.image is None:
            raise RuntimeError("Blender bakes into the material's active image node")
        target.image.fill = 11 if type == "NORMAL" else 97
        return {"FINISHED"}


class FakeTimers:
    def __init__(self):
        self.registered = []

    def register(self, callback, first_interval=0.0, persistent=False):
        self.registered.append(callback)
        return object()

    def unregister(self, _handle):
        return None

    def is_registered(self, callback):
        return callback in self.registered

    def run_all(self):
        """Run every queued callback once (stands in for a main-loop tick)."""
        pending, self.registered = list(self.registered), []
        for callback in pending:
            callback()
        return len(pending)


class _ViewLayerObjects:
    """``view_layer.objects`` — assigning ``.active`` also updates the context.

    Blender derives ``context.active_object`` from the view layer, so the fake
    has to do the same: otherwise a tool that activates an object would keep
    baking the previously selected one, and the test would pass on the wrong
    evidence.
    """

    def __init__(self, host):
        self._host = host
        self._active = None

    @property
    def active(self):
        return self._active

    @active.setter
    def active(self, value):
        self._active = value
        self._host.context.active_object = value


class FakeHost(_types.ModuleType):
    """A ``bpy`` module good enough for the auto-bake code paths."""

    def __init__(self, with_legacy_knobs=True):
        super().__init__("bpy")
        self.with_legacy_knobs = with_legacy_knobs
        self.types = _make_types_module()
        self.props = _make_props_module()
        self.utils = _types.ModuleType("bpy.utils")
        self.utils.register_class = self._register_class
        self.utils.unregister_class = self._unregister_class
        self.data = _types.SimpleNamespace(
            images=FakeImages(),
            objects=FakeObjects(),
            materials=_types.SimpleNamespace(new=FakeMaterial),
        )
        self.scene_instance = FakeSceneInstance(with_legacy_knobs=with_legacy_knobs)
        self.view_layer = _types.SimpleNamespace(objects=_ViewLayerObjects(self))
        self.app = _types.SimpleNamespace(
            version=(5, 2, 3),
            timers=FakeTimers(),
            handlers=_types.SimpleNamespace(load_post=[]),
            is_job_running=lambda _name: False,
        )
        self.bake_calls = []
        self.registered_classes = []
        self.ops = FakeOps(self)
        self.context = _types.SimpleNamespace(
            scene=self.scene_instance,
            active_object=None,
            view_layer=self.view_layer,
        )
        # ``bpy.types.Scene.at_tool`` is set by the add-on's register(); the
        # instance attribute above is what tools read, like a real per-scene
        # property.
        self._attributes = {}

    # -- registration ----------------------------------------------

    def _register_class(self, cls):
        if cls in self.registered_classes:
            raise ValueError(
                "Register class: already registered as {0!r}".format(
                    getattr(cls, "__name__", cls)
                )
            )
        self.registered_classes.append(cls)
        key = getattr(cls, "bl_idname", "") or getattr(cls, "__name__", "anon")
        setattr(self.types, key, cls)
        if "." in key:
            namespace, _, name = key.partition(".")
            holder = getattr(self.ops, namespace, None)
            if holder is None:
                holder = _types.SimpleNamespace()
                setattr(self.ops, namespace, holder)
            setattr(holder, name, lambda *a, **k: {"FINISHED"})

    def _unregister_class(self, cls):
        if cls in self.registered_classes:
            self.registered_classes.remove(cls)
        key = getattr(cls, "bl_idname", "") or getattr(cls, "__name__", "")
        if getattr(self.types, key, None) is cls:
            delattr(self.types, key)

    # -- helpers for tests -----------------------------------------

    def add_object(self, name, obj_type="MESH", material="baker_material"):
        obj = FakeObject(name, obj_type)
        obj.at_objtool = FakeProps(prepare_statement_prop_bool=False)
        if material:
            obj.materials.append(FakeMaterial(material))
        self.data.objects[name] = obj
        return obj

    def select(self, name):
        for candidate in self.data.objects.values():
            candidate.select_set(False)
        obj = self.data.objects[name]
        obj.select_set(True)
        self.context.active_object = obj
        self.view_layer.objects.active = obj
        return obj

    @property
    def scene(self):
        return self.scene_instance


def _install_bpy(host, monkeypatch=None):
    """Register the fake host plus the submodules the add-on imports."""
    modules = {
        "bpy": host,
        "bpy.types": host.types,
        "bpy.props": host.props,
        "bpy.utils": host.utils,
    }
    gpu_extras = _types.ModuleType("gpu_extras")
    presets = _types.ModuleType("gpu_extras.presets")
    presets.draw_texture_2d = lambda *args, **kwargs: None
    gpu_extras.presets = presets
    modules["gpu_extras"] = gpu_extras
    modules["gpu_extras.presets"] = presets
    for name, module in modules.items():
        if monkeypatch is not None:
            monkeypatch.setitem(sys.modules, name, module)
        else:
            sys.modules[name] = module
    _rebind_vendored_bpy(host)
    return modules


def _rebind_vendored_bpy(host):
    """Point every already-imported vendored module at *host*.

    The vendored add-on does ``import bpy`` at module scope, so its module
    globals hold whichever ``bpy`` was live when it was first imported. In a
    real Blender run that never changes; across tests it does, and a stale
    reference would silently drive the previous fake host.
    """
    prefix = "dcc_plugin_autobake.agent.vendor.autobake_tools"
    for name, module in list(sys.modules.items()):
        if name == prefix or name.startswith(prefix + "."):
            if getattr(module, "bpy", None) is not None:
                module.bpy = host


def _uninstall_bpy():
    for name in ("bpy", "bpy.types", "bpy.props", "bpy.utils", "gpu_extras",
                 "gpu_extras.presets"):
        sys.modules.pop(name, None)


@pytest.fixture()
def host(monkeypatch):
    """A 4.x-shaped fake host (both legacy Cycles knobs present)."""
    from dcc_plugin_autobake.agent import _addon
    from dcc_plugin_autobake.agent.tools import _bpy

    fake = FakeHost(with_legacy_knobs=True)
    _install_bpy(fake, monkeypatch)
    _bpy.reset_for_tests()
    _addon.reset_for_tests()
    yield fake
    _bpy.reset_for_tests()
    _addon.reset_for_tests()


@pytest.fixture()
def new_host(with_legacy_knobs=False):
    """A 5.x-shaped fake host by default (``feature_set`` / ``use_bake_multires`` gone)."""
    from dcc_plugin_autobake.agent import _addon
    from dcc_plugin_autobake.agent.tools import _bpy

    fake = FakeHost(with_legacy_knobs=with_legacy_knobs)
    _install_bpy(fake)
    _bpy.reset_for_tests()
    _addon.reset_for_tests()
    try:
        yield fake
    finally:
        _uninstall_bpy()
        _bpy.reset_for_tests()
        _addon.reset_for_tests()


@pytest.fixture()
def tmp_output(tmp_path, monkeypatch):
    target = tmp_path / "out"
    target.mkdir()
    monkeypatch.setenv("LAP_AUTOBAKE_OUTPUT_DIR", str(target))
    return target
