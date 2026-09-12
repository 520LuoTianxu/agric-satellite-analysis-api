"""Progress outages must recover without turning completed jobs back into downloads."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from openfarm_common import job_progress_redis as progress


@pytest.fixture(autouse=True)
def reset_client():
    progress.reset_client_for_tests()
    yield
    progress.reset_client_for_tests()


def test_startup_failure_recovers_once_after_cooldown():
    client = MagicMock()
    with (
        patch(
            "redis.Redis.from_url", side_effect=[OSError("offline"), client]
        ) as connect,
        patch.object(progress.time, "monotonic", return_value=10) as clock,
    ):
        assert progress._get_client() is None
        clock.return_value = 39
        assert progress._get_client() is None
        assert connect.call_count == 1
        clock.return_value = 41
        with ThreadPoolExecutor(max_workers=8) as pool:
            clients = list(pool.map(lambda _: progress._get_client(), range(16)))
        assert all(value is client for value in clients)
        assert connect.call_count == 2


def test_runtime_failure_enters_cooldown_then_recovers():
    broken, healthy = MagicMock(), MagicMock()
    broken.hgetall.side_effect = OSError("disconnected")
    healthy.hgetall.return_value = {"done": "2"}
    with (
        patch("redis.Redis.from_url", side_effect=[broken, healthy]) as connect,
        patch.object(progress.time, "monotonic", return_value=10) as clock,
    ):
        assert progress.read_progress("job") is None
        assert progress.read_progress("job") is None
        assert broken.hgetall.call_count == 1
        assert connect.call_count == 1
        clock.return_value = 41
        assert progress.read_progress("job") == {"done": 2}


@pytest.mark.parametrize("step", ["complete", "completed", "failed", "cancelled"])
def test_terminal_database_progress_wins_over_stale_snapshot(step):
    stored = {"current_step": step, "scenes_done": 10}
    assert (
        progress.apply_redis_to_progress(
            stored, {"current_step": "download_bands", "done": 3, "total": 10}
        )
        == stored
    )
