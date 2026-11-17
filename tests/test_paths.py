"""Output-path rules: relative paths stay in the output root, ``..`` is refused."""

from __future__ import annotations

import os

import pytest

from dcc_plugin_autobake.agent.tools import _paths


def test_relative_path_lands_in_the_output_root(tmp_output):
    resolved = _paths.resolve_output_path("bake/normal.png")
    assert resolved == os.path.join(str(tmp_output), "bake", "normal.png")


def test_absolute_path_is_honoured(tmp_path):
    target = tmp_path / "elsewhere" / "map.png"
    assert _paths.resolve_output_path(str(target)) == os.path.normpath(str(target))


@pytest.mark.parametrize("raw", ["../escape.png", "sub/../../escape.png", "..\\escape.png"])
def test_parent_segments_are_refused(raw):
    with pytest.raises(ValueError):
        _paths.resolve_output_path(raw)


@pytest.mark.parametrize("raw", ["", "   ", None, 5])
def test_empty_and_non_string_paths_are_refused(raw):
    with pytest.raises(ValueError):
        _paths.resolve_output_path(raw)


def test_nul_byte_is_refused():
    with pytest.raises(ValueError):
        _paths.resolve_output_path("ok\x00.png")


def test_default_name_sanitises_the_object_name(tmp_output):
    path = _paths.default_output_path("Cube: 1/with bad chars", "_n")
    assert path.startswith(str(tmp_output))
    assert path.endswith("_n.png")
    assert "/" not in os.path.basename(path)
    assert ":" not in os.path.basename(path)


def test_public_path_is_relative_under_the_root(tmp_output):
    absolute = os.path.join(str(tmp_output), "map.png")
    assert _paths.public_output_path(absolute) == "map.png"


def test_public_path_keeps_absolute_paths_outside_the_root(tmp_path):
    outside = str(tmp_path / "other" / "map.png")
    assert _paths.public_output_path(outside) == os.path.abspath(outside)


def test_output_root_defaults_to_the_process_cwd(monkeypatch):
    monkeypatch.delenv(_paths.OUTPUT_DIR_ENV, raising=False)
    assert _paths.output_root() == os.getcwd()
