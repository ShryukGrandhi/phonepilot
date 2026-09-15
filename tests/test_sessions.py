import pytest

from phonepilot.cloud import ProvisioningFailed
from phonepilot.sessions import acquire, leased, release


def test_acquire_creates_and_waits(client, phone):
    phone.polls_until_ready = 2
    logs = []
    lease = acquire(client, None, 600, logs.append)
    assert lease.created_here and lease.session.is_ready
    assert any("created session fake123" in m for m in logs) and any("ready after" in m for m in logs)


def test_acquire_reuses_existing_without_owning_it(client, phone):
    phone.state = "ready"
    lease = acquire(client, "fake123", 600, lambda m: None)
    assert not lease.created_here and lease.session.id == "fake123"


def test_leased_releases_only_what_it_created(client, phone):
    phone.state = "ready"
    with leased(client, "fake123", log=lambda m: None):
        pass
    assert phone.state == "ready", "reused session must not be ended"
    with leased(client, None, 600, log=lambda m: None) as lease:
        assert lease.created_here
    assert phone.state == "closing"


def test_leased_keep_flag_skips_release(client, phone):
    logs = []
    with leased(client, None, 600, keep=True, log=logs.append):
        pass
    assert phone.state == "ready" and any("--keep" in m for m in logs)


def test_leased_releases_on_exception(client, phone):
    with pytest.raises(RuntimeError):
        with leased(client, None, 600, log=lambda m: None):
            raise RuntimeError("agent blew up")
    assert phone.state == "closing"


def test_release_tolerates_missing_session(client, phone):
    logs = []
    release(client, "nope", logs.append)
    assert "already gone" in logs[0]


def test_acquire_propagates_provisioning_error(client, phone):
    phone.state = "error"
    with pytest.raises(ProvisioningFailed):
        acquire(client, "fake123", 600, lambda m: None)


def test_acquire_deletes_a_created_session_that_fails_to_provision(client, phone):
    """Seen live: a fresh session went provisioning -> error and sat in the account list. We must DELETE it."""
    phone.fail_provision = "provider could not boot the device"
    logs: list[str] = []
    with pytest.raises(ProvisioningFailed):
        acquire(client, None, 600, logs.append)
    assert phone.state == "closing", "the errored session was released"
    assert any("released" in m or "closing" in m for m in logs)


def test_acquire_does_not_delete_a_foreign_session_that_is_in_error(client, phone):
    phone.state = "error"
    with pytest.raises(ProvisioningFailed):
        acquire(client, "fake123", 600, lambda m: None)
    assert phone.state == "error", "not created here -> not ours to delete"
