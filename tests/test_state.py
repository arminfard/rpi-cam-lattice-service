"""StateStore: JSON persistence with atomic writes and tolerant loading."""

import json

from rpi_cam_lattice_service.state import StateStore


def test_roundtrip_persists_across_instances(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(str(path))
    assert store.get("ingress") is None

    store.set("ingress", {"video_id": "abc", "push_url": "srt://x"})
    store.set("created_time", "2026-09-16T00:00:00+00:00")

    reloaded = StateStore(str(path))
    assert reloaded.get("ingress") == {"video_id": "abc", "push_url": "srt://x"}
    assert reloaded.get("created_time") == "2026-09-16T00:00:00+00:00"


def test_delete_removes_key_and_rewrites_file(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(str(path))
    store.set("ingress", {"video_id": "abc"})
    store.delete("ingress")
    store.delete("missing")  # no-op

    assert store.get("ingress") is None
    assert json.loads(path.read_text()) == {}
    assert not (tmp_path / "state.json.tmp").exists()


def test_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.json"
    StateStore(str(path)).set("k", 1)
    assert json.loads(path.read_text()) == {"k": 1}


def test_corrupt_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    store = StateStore(str(path))
    assert store.get("anything") is None
    store.set("k", "v")
    assert json.loads(path.read_text()) == {"k": "v"}


def test_non_object_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]")
    assert StateStore(str(path)).get("k") is None


def test_empty_path_is_in_memory_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = StateStore("")
    store.set("k", 1)
    assert store.get("k") == 1
    assert list(tmp_path.iterdir()) == []
