"""
Round 1 regression tests — the eight independent bugs.

Hermetic by construction: no Chromium, no network, no mobifacil. The browser is
never launched; ``run_flow`` is monkeypatched at the API boundary and every
retry/backoff sleep is stubbed out.

Run with:  pytest tests/test_round1.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core.models.schemas import (
    SAFE_ID_RE,
    ReservationRecord,
    ReservationRequest,
    ReservationStatus,
)


def _now():
    return datetime.now(timezone.utc)


def _record(rid="aaaa1111", **kw) -> ReservationRecord:
    base = dict(
        id=rid, origin_id="19052", destination_id="-3", date="2026-07-25",
        departure="09:00", seat="15", created_at=_now(), updated_at=_now(),
    )
    base.update(kw)
    return ReservationRecord(**base)


# ─────────────────────────────────────────────────────────────────
# #2 — reservation id validation + duplicate rejection
# ─────────────────────────────────────────────────────────────────
def _req(**kw):
    body = dict(origin_id="19052", destination_id="-3", date="2026-07-25",
                departure="09:00", seat="15")
    body.update(kw)
    return ReservationRequest(**body)


@pytest.mark.parametrize("bad", [
    "../../../etc/passwd",
    "../escape",
    "a/b",
    "a\\b",
    "with space",
    "dots..dots",
    "",
    "x" * 65,
])
def test_unsafe_ids_are_rejected(bad):
    with pytest.raises(Exception) as e:      # pydantic ValidationError
        _req(id=bad)
    assert "id" in str(e.value)


@pytest.mark.parametrize("good", ["abc123", "A_b-C", "0" * 64, "x"])
def test_safe_ids_are_accepted(good):
    assert _req(id=good).id == good


def test_id_stays_optional():
    assert _req().id is None


def test_path_traversal_id_cannot_reach_the_log_filename():
    """The write path used to build `<date>-<id>.log` with no sanitisation, so a
    `../` id steered the mkdir outside the reservation log directory."""
    from core.utils.logger import reservation_log_path
    with pytest.raises(ValueError):
        reservation_log_path("../../../../tmp/pwned")


def test_reservation_log_degrades_instead_of_failing_the_flow():
    """If the guard ever fires mid-flow, losing the log must beat losing the booking."""
    from core.utils.logger import reservation_log
    with reservation_log("../nope") as path:
        assert path is None


def test_store_rejects_duplicate_id():
    from core.state import ReservationStore

    async def go():
        store = ReservationStore()
        assert await store.add(_record("dup00001")) is not None
        # Same id again: must be refused, not silently overwrite the live record
        # (whose relock_<id> job would then re-lock a different route).
        assert await store.add(_record("dup00001", seat="99")) is None
        assert (await store.get("dup00001")).seat == "15"
        # ...unless explicitly asked to replace.
        assert await store.add(_record("dup00001", seat="99"), replace=True) is not None
        assert (await store.get("dup00001")).seat == "99"

    asyncio.run(go())


def test_post_reservations_rejects_a_duplicate_id_with_409(monkeypatch, client):
    from core.api import routes as routes_mod

    async def _flow(params, rid):
        return (0, {"arrivalHour": ""})

    monkeypatch.setattr(routes_mod, "run_flow_guarded", _flow)
    monkeypatch.setattr(routes_mod, "schedule_relock", lambda *a, **k: None)

    import zoneinfo
    dep = _now().astimezone(zoneinfo.ZoneInfo("America/Sao_Paulo")) + timedelta(hours=6)
    body = {
        "id": "dupe0001", "origin_id": "19052", "destination_id": "-3",
        "date": dep.strftime("%Y-%m-%d"), "departure": dep.strftime("%H:%M"), "seat": "15",
    }
    assert client.post("/reservations", json=body).status_code == 201
    second = client.post("/reservations", json=body)
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]


def test_post_reservations_rejects_a_traversal_id_with_422(client):
    r = client.post("/reservations", json={
        "id": "../../../../etc/x", "origin_id": "19052", "destination_id": "-3",
        "date": "2026-07-25", "departure": "09:00", "seat": "15",
    })
    assert r.status_code == 422


def test_shared_id_pattern_is_used_by_both_boundaries():
    """API validation and the filesystem guard must not drift apart."""
    import core.utils.logger as logger_mod
    assert logger_mod.SAFE_ID_RE is SAFE_ID_RE


# ─────────────────────────────────────────────────────────────────
# #4 — retry() must not sleep after the final attempt
# ─────────────────────────────────────────────────────────────────
def test_retry_does_not_sleep_after_the_last_attempt(monkeypatch):
    from core.services import browser as browser_mod

    sleeps = []
    monkeypatch.setattr(browser_mod.time, "sleep", lambda s: sleeps.append(s))

    def always_fails():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        browser_mod.retry(always_fails, "unit", attempts=1)
    assert sleeps == [], "1 attempt means no backoff at all"

    sleeps.clear()
    with pytest.raises(RuntimeError):
        browser_mod.retry(always_fails, "unit", attempts=3)
    assert len(sleeps) == 2, "3 attempts sleep only between them, not after the last"


def test_retry_still_returns_on_a_later_success(monkeypatch):
    from core.services import browser as browser_mod
    monkeypatch.setattr(browser_mod.time, "sleep", lambda s: None)

    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("transient")
        return "ok"

    assert browser_mod.retry(flaky, "unit", attempts=3) == "ok"
    assert len(calls) == 2


def test_retry_chains_the_original_error(monkeypatch):
    from core.services import browser as browser_mod
    monkeypatch.setattr(browser_mod.time, "sleep", lambda s: None)
    err = ValueError("root cause")

    with pytest.raises(RuntimeError) as e:
        browser_mod.retry(lambda: (_ for _ in ()).throw(err), "lbl", attempts=2)
    assert e.value.__cause__ is err
    assert "lbl" in str(e.value)


def test_max_retries_default_actually_retries():
    """MAX_RETRIES=1 made the retry wrapper a no-op for every step using the default."""
    from core.config import settings
    assert settings.max_retries >= 2


# ─────────────────────────────────────────────────────────────────
# #6 — rehydrate must not resurrect hard-failed reservations
# ─────────────────────────────────────────────────────────────────
def test_rehydrate_skips_hard_failed_but_keeps_soft_failed(monkeypatch):
    from core.scheduler import jobs as jobs_mod

    scheduled = []
    monkeypatch.setattr(jobs_mod, "schedule_relock", lambda rid, dep=None: scheduled.append(rid))

    future = _now() + timedelta(hours=6)
    records = [
        _record("locked01", status=ReservationStatus.locked, exit_code=0, departure_datetime=future),
        _record("softfail", status=ReservationStatus.failed, exit_code=1, departure_datetime=future),
        _record("hardfail", status=ReservationStatus.failed, exit_code=2, departure_datetime=future),
        _record("expired1", status=ReservationStatus.expired, exit_code=3, departure_datetime=future),
        _record("neverlck", status=ReservationStatus.failed, exit_code=1, departure_datetime=None),
        _record("toolate1", status=ReservationStatus.locked, exit_code=0,
                departure_datetime=_now() + timedelta(minutes=5)),  # inside the cutoff
    ]

    class _Store:
        async def list(self):
            return records

    monkeypatch.setattr(jobs_mod, "store", _Store())
    asyncio.run(jobs_mod.rehydrate_relocks())

    assert "hardfail" not in scheduled, "exit 2 removed its own job; do not re-arm it"
    assert "toolate1" not in scheduled, "inside the pre-departure cutoff"
    assert sorted(scheduled) == ["locked01", "softfail"]


# ─────────────────────────────────────────────────────────────────
# #12 — misfire_grace_time
# ─────────────────────────────────────────────────────────────────
def test_relock_jobs_have_a_real_misfire_grace(monkeypatch):
    """APScheduler's 1 s default silently DISCARDS a job that fires late."""
    from core.scheduler import jobs as jobs_mod

    captured = {}
    monkeypatch.setattr(jobs_mod.scheduler, "add_job",
                        lambda *a, **kw: captured.update(kw))
    jobs_mod.schedule_relock("abc12345")
    assert captured.get("misfire_grace_time") == jobs_mod._MISFIRE_GRACE_SECONDS
    assert jobs_mod._MISFIRE_GRACE_SECONDS >= 60


def test_reschedule_preserves_misfire_grace():
    """Guards the APScheduler footgun: reschedule_job forwards extra kwargs to the
    TRIGGER, so passing misfire_grace_time there is silently dropped. It must be set
    by add_job and survive the soft-fail reschedule."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from core.scheduler.jobs import _MISFIRE_GRACE_SECONDS

    s = BackgroundScheduler()
    s.add_job(lambda: None, IntervalTrigger(minutes=20), id="j",
              misfire_grace_time=_MISFIRE_GRACE_SECONDS)
    s.reschedule_job("j", trigger=IntervalTrigger(
        minutes=20, start_date=_now() + timedelta(minutes=5)))
    assert s.get_job("j").misfire_grace_time == _MISFIRE_GRACE_SECONDS


# ─────────────────────────────────────────────────────────────────
# #8 — admin auth must 401 (not 500) on non-ASCII credentials
# ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("user,pw", [
    ("admin", "sénha-inválida"),   # non-ASCII password
    ("ádmin", "whatever"),          # non-ASCII username
    ("admin", "wrong"),             # plain ASCII wrong password
])
def test_admin_rejects_bad_credentials_with_401(user, pw, client):
    r = client.get("/admin/stats", auth=(user, pw))
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate") == "Basic"


def test_admin_accepts_the_configured_password(client):
    from core.config import settings
    r = client.get("/admin/stats", auth=("admin", settings.admin_password))
    assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────
# #3 — dead config is gone
# ─────────────────────────────────────────────────────────────────
def test_wait_after_lock_setting_is_removed():
    from core.config import settings
    assert not hasattr(settings, "wait_after_lock"), (
        "WAIT_AFTER_LOCK was documented in three places but read nowhere; "
        "an operator tuning it changed nothing"
    )
