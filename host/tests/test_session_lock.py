import pytest

from session_lock import controller_lock


def test_controller_lock_can_be_reacquired_after_release(tmp_path):
    with controller_lock(tmp_path):
        assert (tmp_path / "controller.lock").is_file()
    with controller_lock(tmp_path):
        pass


def test_controller_lock_rejects_concurrent_owner(tmp_path):
    with controller_lock(tmp_path):
        with pytest.raises(OSError):
            with controller_lock(tmp_path):
                pass
