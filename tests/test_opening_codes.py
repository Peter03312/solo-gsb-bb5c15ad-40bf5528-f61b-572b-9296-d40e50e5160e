"""End-to-end tests for one-time opening codes and venue redemption."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import service
from app.main import create_app
from app.service import Store
from app.timeutil import to_iso

# Slots in 2030 keep the rules independent of the real wall clock; window
# boundary tests install a fake clock in app.service.
START = "2030-06-01T20:00:00Z"
START_DT = datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc)
START_2 = "2030-06-02T20:00:00Z"
WINDOW = {
    "valid_from": "2020-01-01T00:00:00Z",
    "valid_to": "2040-01-01T00:00:00Z",
}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def fake_clock(monkeypatch):
    """Replace app.service.now_iso with a controllable fixed UTC clock."""

    class Clock:
        def __init__(self) -> None:
            self.current = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)

        def set(self, dt: datetime) -> None:
            self.current = dt

        def iso(self) -> str:
            return to_iso(self.current)

    clock = Clock()
    monkeypatch.setattr(service, "now_iso", clock.iso)
    return clock


def make_license(client, **overrides):
    body = {
        "film_id": "film-1",
        "venue_id": "hall-a",
        "valid_from": WINDOW["valid_from"],
        "valid_to": WINDOW["valid_to"],
        "max_sessions": 5,
    }
    body.update(overrides)
    resp = client.post("/licenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def verify(client, key, start=START, film="film-1", venue="hall-a"):
    return client.post(
        "/verifications",
        json={
            "film_id": film,
            "venue_id": venue,
            "start_time": start,
            "idempotency_key": key,
        },
    )


def claim(client, key):
    return client.post(f"/verifications/{key}/opening-code")


def status(client, key):
    return client.get(f"/verifications/{key}/opening-code")


def redeem(client, code, venue="hall-a"):
    return client.post(
        "/opening-codes/redeem", json={"code": code, "venue_id": venue}
    )


def cancel(client, key, reason="放映设备故障，场次取消"):
    return client.post(f"/verifications/{key}/cancel", json={"reason": reason})


# ----------------------------------------------------------------------
# claim
# ----------------------------------------------------------------------
def test_first_claim_returns_plaintext_code(client):
    make_license(client)
    verify(client, "oc-1")
    resp = claim(client, "oc-1")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["code"] is not None
    assert data["code"].startswith("OC-") and len(data["code"]) > 10
    assert data["status"] == "issued"
    assert data["replay"] is False
    assert data["redeemed_at"] is None
    assert data["idempotency_key"] == "oc-1"
    assert data["venue_id"] == "hall-a"
    assert data["start_time"].startswith("2030-06-01T20:00:00")
    assert data["window_open_at"].startswith("2030-06-01T20:00:00")
    assert data["window_close_at"].startswith("2030-06-01T20:30:00")
    assert data["issued_at"]


def test_repeat_claim_returns_status_but_never_the_code(client):
    make_license(client)
    verify(client, "oc-1")
    first = claim(client, "oc-1").json()
    second = claim(client, "oc-1").json()
    assert second["replay"] is True
    assert second["code"] is None
    assert second["status"] == "issued"
    assert second["verification_id"] == first["verification_id"]
    assert second["issued_at"] == first["issued_at"]

    # the status endpoint behaves the same and never leaks the code
    via_get = status(client, "oc-1").json()
    assert via_get["code"] is None
    assert via_get["status"] == "issued"
    assert via_get["replay"] is False


def test_concurrent_claims_mint_exactly_one_code(tmp_path):
    store = Store(str(tmp_path / "claim-concurrent.db"))
    lic = store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2040, 1, 1, tzinfo=timezone.utc),
        max_sessions=5,
    )
    approved = store.verify(
        film_id="film-x",
        venue_id="hall-1",
        start_time=START_DT,
        idempotency_key="seed",
    )
    assert approved["decision"] == "approved"
    assert lic["id"]  # silence unused lint

    results, errors = [], []

    def worker() -> None:
        try:
            results.append(store.claim_opening_code("seed"))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 15
    plaintext = [r for r in results if r["code"] is not None]
    assert len(plaintext) == 1
    assert all(r["verification_id"] == plaintext[0]["verification_id"]
               for r in results)
    assert all(r["status"] == "issued" for r in results)
    assert sum(1 for r in results if not r["replay"]) == 1


def test_claim_unknown_key_is_404(client):
    resp = claim(client, "no-such-key")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
    assert status(client, "no-such-key").status_code == 404


def test_claim_for_rejected_verification_is_422(client):
    verify(client, "oc-rej", film="unknown", venue="nowhere")
    resp = claim(client, "oc-rej")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VERIFICATION_NOT_APPROVED"
    # failed claim leaves no code behind
    assert status(client, "oc-rej").status_code == 404


def test_claim_for_cancelled_session_is_422_and_voids_existing_code(client):
    make_license(client)
    verify(client, "oc-1")
    issued = claim(client, "oc-1").json()
    assert issued["code"] is not None

    assert cancel(client, "oc-1").status_code == 200

    # repeat claim after cancellation: status-only replay now reports void;
    # the code is never re-issued and no plaintext leaks
    again = claim(client, "oc-1")
    assert again.status_code == 200
    assert again.json()["replay"] is True
    assert again.json()["code"] is None
    assert again.json()["status"] == "void"

    # and the unredeemed code can no longer be used at the door
    used = redeem(client, issued["code"])
    assert used.status_code == 422
    assert used.json()["code"] == "SCREENING_CANCELLED"


def test_claim_before_cancel_vs_after_is_serialised(tmp_path):
    store = Store(str(tmp_path / "claim-cancel.db"))
    store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2040, 1, 1, tzinfo=timezone.utc),
        max_sessions=5,
    )
    store.verify(
        film_id="film-x",
        venue_id="hall-1",
        start_time=START_DT,
        idempotency_key="seed",
    )

    outcomes, errors = [], []
    barrier = threading.Barrier(20)

    def claim_worker() -> None:
        barrier.wait()
        try:
            outcomes.append(("claim", store.claim_opening_code("seed")))
        except Exception as exc:
            outcomes.append(("claim-error", exc))

    def cancel_worker() -> None:
        barrier.wait()
        try:
            outcomes.append(
                ("cancel", store.cancel_verification("seed", reason="取消"))
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=claim_worker) for _ in range(19)]
    threads.append(threading.Thread(target=cancel_worker))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    claims = [o for kind, o in outcomes if kind == "claim"]
    claim_errors = [o for kind, o in outcomes if kind == "claim-error"]
    assert len(claims) + len(claim_errors) == 19

    # exactly one first claim ever happens: either it committed before the
    # cancel (one plaintext code; later claims replay with status only and
    # the code ends up void), or the cancel won the race and every claim is
    # rejected with SCREENING_CANCELLED — never two codes, never a usable
    # code after cancellation
    plaintext = [c for c in claims if c["code"] is not None]
    assert len(plaintext) <= 1
    for exc in claim_errors:
        assert exc.code == "SCREENING_CANCELLED"

    if plaintext:
        final = store.get_opening_code("seed")
        assert final["status"] == "void"
        assert final["code"] is None
        # the single first winner got the only plaintext that ever existed;
        # every other claim was a status-only replay of the same row
        assert all(
            c["verification_id"] == plaintext[0]["verification_id"]
            for c in claims
        )
        assert sum(1 for c in claims if not c["replay"]) == 1
    else:
        # cancel committed before any claim: no code row exists at all
        assert not claims
        from app.service import NotFoundError

        with pytest.raises(NotFoundError):
            store.get_opening_code("seed")


def test_claim_after_session_start_is_422(client, fake_clock):
    make_license(client)
    verify(client, "oc-1")
    fake_clock.set(START_DT)  # exactly the planned start
    resp = claim(client, "oc-1")
    assert resp.status_code == 422
    assert resp.json()["code"] == "SESSION_ALREADY_STARTED"
    assert status(client, "oc-1").status_code == 404


# ----------------------------------------------------------------------
# redemption happy path
# ----------------------------------------------------------------------
def test_redeem_inside_window_returns_single_receipt(client, fake_clock):
    lic = make_license(client)
    approved = verify(client, "oc-1").json()
    code = claim(client, "oc-1").json()["code"]

    fake_clock.set(datetime(2030, 6, 1, 20, 5, tzinfo=timezone.utc))
    resp = redeem(client, code)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["replay"] is False
    assert data["idempotency_key"] == "oc-1"
    assert data["verification_id"] == approved["id"]
    assert data["film_id"] == "film-1"
    assert data["venue_id"] == "hall-a"
    assert data["start_time"].startswith("2030-06-01T20:00:00")
    assert data["license_id"] == lic["id"]
    assert data["pool_id"] is None
    assert data["redeemed_at"].startswith("2030-06-01T20:05:00")
    assert data["id"] >= 1

    # status flips to redeemed; code stays hidden
    st = status(client, "oc-1").json()
    assert st["status"] == "redeemed"
    assert st["code"] is None
    assert st["redeemed_at"] == data["redeemed_at"]


def test_redeem_succeeds_at_window_endpoints(client, fake_clock):
    make_license(client)
    verify(client, "oc-a")
    code_a = claim(client, "oc-a").json()["code"]

    fake_clock.set(START_DT)  # exactly at planned start: accepted
    first = redeem(client, code_a)
    assert first.status_code == 200

    # exactly at start + 30 minutes on a second session: still accepted
    verify(client, "oc-b", start=START_2)
    code_b = claim(client, "oc-b").json()["code"]
    fake_clock.set(datetime(2030, 6, 2, 20, 30, tzinfo=timezone.utc))
    second = redeem(client, code_b)
    assert second.status_code == 200


# ----------------------------------------------------------------------
# redemption rejections (no receipt is ever created)
# ----------------------------------------------------------------------
def test_redeem_too_early_is_422_without_receipt(client, fake_clock):
    make_license(client)
    verify(client, "oc-1")
    code = claim(client, "oc-1").json()["code"]

    fake_clock.set(datetime(2030, 6, 1, 19, 59, 59, 999999,
                            tzinfo=timezone.utc))
    resp = redeem(client, code)
    assert resp.status_code == 422
    assert resp.json()["code"] == "REDEMPTION_NOT_OPEN"
    assert status(client, "oc-1").json()["status"] == "issued"

    # after the rejection the same code still redeems inside the window
    fake_clock.set(datetime(2030, 6, 1, 20, 1, tzinfo=timezone.utc))
    assert redeem(client, code).status_code == 200


def test_redeem_after_window_is_422_without_receipt(client, fake_clock):
    make_license(client)
    verify(client, "oc-1")
    code = claim(client, "oc-1").json()["code"]

    fake_clock.set(datetime(2030, 6, 1, 20, 30, 0, 1, tzinfo=timezone.utc))
    resp = redeem(client, code)
    assert resp.status_code == 422
    assert resp.json()["code"] == "REDEMPTION_WINDOW_CLOSED"
    assert status(client, "oc-1").json()["status"] == "issued"


def test_redeem_with_wrong_venue_is_422_without_receipt(client, fake_clock):
    make_license(client)
    verify(client, "oc-1")
    code = claim(client, "oc-1").json()["code"]

    fake_clock.set(datetime(2030, 6, 1, 20, 5, tzinfo=timezone.utc))
    resp = redeem(client, code, venue="hall-b")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VENUE_MISMATCH"
    assert status(client, "oc-1").json()["status"] == "issued"

    # the matching venue still redeems afterwards
    ok = redeem(client, code, venue="hall-a")
    assert ok.status_code == 200


def test_redeem_unknown_code_is_404_and_leaves_no_trace(client, fake_clock):
    resp = redeem(client, "OC-doesnotexistatall")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_redeem_cancelled_code_is_422_even_inside_window(client, fake_clock):
    make_license(client)
    verify(client, "oc-1")
    code = claim(client, "oc-1").json()["code"]
    cancel(client, "oc-1")

    fake_clock.set(datetime(2030, 6, 1, 20, 5, tzinfo=timezone.utc))
    resp = redeem(client, code)
    assert resp.status_code == 422
    assert resp.json()["code"] == "SCREENING_CANCELLED"


def test_redeem_blank_or_missing_fields_are_422(client):
    assert client.post(
        "/opening-codes/redeem", json={"code": "", "venue_id": "hall-a"}
    ).status_code == 422
    assert client.post(
        "/opening-codes/redeem", json={"code": "OC-x", "venue_id": ""}
    ).status_code == 422
    assert client.post(
        "/opening-codes/redeem", json={"code": "OC-x"}
    ).status_code == 422


# ----------------------------------------------------------------------
# replay: the first receipt is authoritative
# ----------------------------------------------------------------------
def test_redeem_retry_returns_first_receipt_even_after_window(
    client, fake_clock
):
    make_license(client)
    verify(client, "oc-1")
    code = claim(client, "oc-1").json()["code"]

    fake_clock.set(datetime(2030, 6, 1, 20, 10, tzinfo=timezone.utc))
    first = redeem(client, code).json()

    # long after the 30-minute window, retries still return the first receipt
    fake_clock.set(datetime(2030, 6, 2, 0, 0, tzinfo=timezone.utc))
    second = redeem(client, code)
    assert second.status_code == 200
    data = second.json()
    assert data["replay"] is True
    assert data["id"] == first["id"]
    assert data["redeemed_at"] == first["redeemed_at"]

    # ... even when retried with a different venue: receipt is not re-checked
    third = redeem(client, code, venue="hall-other")
    assert third.status_code == 200
    assert third.json()["id"] == first["id"]
    assert third.json()["replay"] is True


def test_concurrent_redeems_create_exactly_one_receipt(tmp_path):
    store = Store(str(tmp_path / "redeem-concurrent.db"))
    store.create_license(
        film_id="film-x",
        venue_id="hall-1",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2040, 1, 1, tzinfo=timezone.utc),
        max_sessions=5,
    )
    # real wall clock: schedule a couple of seconds in the future so the
    # race happens inside the redemption window
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
        seconds=4
    )
    store.verify(
        film_id="film-x",
        venue_id="hall-1",
        start_time=start,
        idempotency_key="seed",
    )
    issued = store.claim_opening_code("seed")
    code = issued["code"]
    assert code is not None

    # wait until the window is open
    while datetime.now(timezone.utc) < start:
        pass

    results, errors = [], []

    def worker() -> None:
        try:
            results.append(
                store.redeem_opening_code(code=code, venue_id="hall-1")
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 15
    assert len({r["id"] for r in results}) == 1
    assert len({r["redeemed_at"] for r in results}) == 1
    firsts = [r for r in results if not r["replay"]]
    replays = [r for r in results if r["replay"]]
    assert len(firsts) == 1
    assert len(replays) == 14


# ----------------------------------------------------------------------
# redemption is independent of later license state and of quota accounting
# ----------------------------------------------------------------------
def test_revocation_after_approval_does_not_block_redemption(
    client, fake_clock
):
    lic = make_license(client)
    approved = verify(client, "oc-1").json()
    assert approved["decision"] == "approved"
    code = claim(client, "oc-1").json()["code"]

    # license revoked and suspended after the approval: redemption unaffected
    client.post(f"/licenses/{lic['id']}/revoke", json={"reason": "撤片"})
    client.post(
        f"/licenses/{lic['id']}/suspensions",
        json={
            "suspend_from": "2030-06-01T00:00:00Z",
            "suspend_to": "2030-06-30T00:00:00Z",
            "reason": "暂停",
        },
    )

    fake_clock.set(datetime(2030, 6, 1, 20, 5, tzinfo=timezone.utc))
    resp = redeem(client, code)
    assert resp.status_code == 200
    assert resp.json()["license_id"] == lic["id"]


def test_redemption_changes_neither_verification_nor_quota(client, fake_clock):
    lic = make_license(client, max_sessions=2)
    approved = verify(client, "oc-1").json()
    assert approved["remaining_quota"] == 1
    code = claim(client, "oc-1").json()["code"]

    fake_clock.set(datetime(2030, 6, 1, 20, 5, tzinfo=timezone.utc))
    assert redeem(client, code).status_code == 200

    # original verification result and balance snapshot are untouched
    fetched = client.get("/verifications/oc-1").json()
    assert fetched["decision"] == "approved"
    assert fetched["remaining_quota"] == 1
    assert fetched["cancelled"] is False

    # license usage counts verifications, not redemptions: still 1
    lic_after = client.get(f"/licenses/{lic['id']}").json()
    assert lic_after["used_sessions"] == 1
    assert lic_after["remaining_sessions"] == 1

    # replaying the verification after redemption still returns the original
    replay = verify(client, "oc-1").json()
    assert replay["replay"] is True
    assert replay["remaining_quota"] == 1


def test_cancel_after_redemption_fails_and_receipt_survives(
    client, fake_clock
):
    make_license(client)
    verify(client, "oc-1")
    code = claim(client, "oc-1").json()["code"]

    fake_clock.set(datetime(2030, 6, 1, 20, 5, tzinfo=timezone.utc))
    receipt = redeem(client, code).json()

    # already-started (and redeemed) session can no longer be cancelled
    resp = cancel(client, "oc-1")
    assert resp.status_code == 422
    assert resp.json()["code"] == "SESSION_ALREADY_STARTED"

    st = status(client, "oc-1").json()
    assert st["status"] == "redeemed"
    again = redeem(client, code)
    assert again.json()["id"] == receipt["id"]
    assert again.json()["replay"] is True
