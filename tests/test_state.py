"""Unit tests for the local image-marker state file and the cross-process run lock."""

from __future__ import annotations

import threading
import time

import pytest
from filelock import FileLock, Timeout

from nalanda.state import load_state, run_lock, save_state


def test_round_trip(tmp_path):
    path = tmp_path / "state.json"
    data = {
        "Example Collection": {
            "Primary": "http://x/p.jpg",
            "Thumb": "http://x/t.jpg",
            "Backdrop": None,
        },
        "Merge": {"Primary": None, "Thumb": None, "Backdrop": None},
    }
    save_state(path, data)
    assert load_state(path) == data


def test_missing_file_is_empty(tmp_path):
    assert load_state(tmp_path / "does-not-exist.json") == {}


def test_corrupt_file_is_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert load_state(path) == {}  # tolerate corruption rather than crash the run


def test_unreadable_existing_file_raises(tmp_path, monkeypatch):
    # An existing file we can't *read* (I/O / permission error) must not be
    # treated as empty: the state file is not reconstructible, so silently
    # rebuilding would overwrite the authoritative record. Corruption (invalid
    # JSON) is still tolerated -- see above.
    from pathlib import Path

    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")  # exists, but read will fail below

    def boom(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(RuntimeError):
        load_state(path)


def test_save_state_creates_parent_dir(tmp_path):
    # a fresh /config-style mount may lack the state dir; save_state must create it
    path = tmp_path / "sub" / "state.json"
    data = {"X": {"Primary": "http://x/p.jpg", "Thumb": None, "Backdrop": None}}
    save_state(path, data)
    assert load_state(path) == data


def test_non_dict_json_is_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_state(path) == {}


def test_save_state_is_atomic_no_temp_left(tmp_path):
    path = tmp_path / "state.json"
    data = {"X": {"Primary": "http://x/p.jpg", "Thumb": None, "Backdrop": None}}
    save_state(path, data)
    assert load_state(path) == data
    assert list(tmp_path.glob("*.tmp")) == []  # temp consumed by os.replace


def test_save_state_failure_keeps_previous_file(tmp_path, monkeypatch):
    import nalanda.state as state_mod

    path = tmp_path / "state.json"
    save_state(path, {"old": {"Primary": "keep", "Thumb": None, "Backdrop": None}})

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(state_mod.os, "replace", boom)
    with pytest.raises(OSError):
        save_state(path, {"new": {"Primary": "lost", "Thumb": None, "Backdrop": None}})
    # the prior file is untouched (no truncation) and the temp is cleaned up
    assert load_state(path) == {
        "old": {"Primary": "keep", "Thumb": None, "Backdrop": None}
    }
    assert list(tmp_path.glob("*.tmp")) == []


# ----------------------------------------------------------------- run_lock


def test_run_lock_uncontended_acquires_and_releases(tmp_path):
    p = str(tmp_path / "run.lock")
    with run_lock(path=p):
        pass
    # released, so re-acquirable
    with run_lock(path=p):
        pass


def test_run_lock_without_wait_raises_when_held(tmp_path):
    p = str(tmp_path / "run.lock")
    holder = FileLock(p)
    holder.acquire()
    try:
        with pytest.raises(Timeout):
            with run_lock(path=p, wait=False):
                pass
    finally:
        holder.release()


def test_run_lock_waits_then_proceeds_with_notice(tmp_path):
    # The main thread holds the lock; a waiter (in its own thread) must block until we
    # release, logging the "queued" notice first. Each run_lock acquires+releases on one
    # thread, matching real use (CLI on its main thread, daemon on its timer thread).
    p = str(tmp_path / "run.lock")
    msgs: list[str] = []
    entered = threading.Event()

    def waiter():
        with run_lock(path=p, poll=5.0, notify=msgs.append):
            entered.set()

    t = threading.Thread(target=waiter)
    with run_lock(path=p):  # main thread holds it
        t.start()
        time.sleep(0.3)  # give the waiter time to block + log
        assert not entered.is_set()  # still blocked while we hold it
    t.join(timeout=5)  # released now -> waiter acquires
    assert entered.is_set()
    assert any("queued" in m for m in msgs)
