"""Small core helpers: UTC timestamps, per-symbol locks, SSO token checks."""
import threading
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException

from app.core import sso, symlock
from app.core.timeutil import as_utc, iso_utc


class TestTimeutil:
    def test_naive_is_read_as_utc(self):
        assert as_utc(datetime(2026, 7, 30, 5, 58)).tzinfo == timezone.utc

    def test_aware_is_left_alone(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        dt = datetime(2026, 7, 30, 11, 28, tzinfo=ist)
        assert as_utc(dt) is dt

    def test_iso_always_carries_an_offset(self):
        # Offset-less ISO is read as local time by the browser — 5h30m off.
        assert iso_utc(datetime(2026, 7, 30, 5, 58, 12)) == "2026-07-30T05:58:12+00:00"

    def test_none(self):
        assert as_utc(None) is None and iso_utc(None) is None


class TestSymlock:
    def test_case_insensitive_and_reentrant(self):
        assert symlock.lock_for("sbin-fut") is symlock.lock_for("SBIN-FUT")
        with symlock.hold("SBIN-FUT"):
            with symlock.hold("sbin-fut"):     # open_position -> average_position nests
                pass

    def test_try_hold_yields_false_while_another_thread_holds_it(self):
        got = []
        held, release = threading.Event(), threading.Event()

        def holder():
            with symlock.hold("TCS-FUT"):
                held.set()
                release.wait(2)

        t = threading.Thread(target=holder)
        t.start()
        held.wait(2)
        with symlock.try_hold("TCS-FUT") as ok:
            got.append(ok)
        with symlock.try_hold("INFY-FUT") as ok:     # other symbols never wait
            got.append(ok)
        release.set()
        t.join()
        assert got == [False, True]

    def test_try_hold_releases(self):
        with symlock.try_hold("LT-FUT") as ok:
            assert ok
        assert symlock.lock_for("LT-FUT").acquire(blocking=False)
        symlock.lock_for("LT-FUT").release()


def _token(typ="user", sub="7", secret="test-secret-not-a-real-key-padded-to-32b", exp_s=60):
    return jwt.encode({"sub": sub, "typ": typ, "exp": int(time.time()) + exp_s},
                      secret, algorithm="HS256")


class TestSSO:
    def test_valid_user_token(self):
        assert sso.require_user(f"Bearer {_token()}") == 7

    @pytest.mark.parametrize("header", [None, "", "Basic abc", "Token xyz"])
    def test_missing_or_wrong_scheme(self, header):
        with pytest.raises(HTTPException) as e:
            sso.require_user(header)
        assert e.value.status_code == 401

    def test_service_token_is_rejected(self):
        with pytest.raises(HTTPException) as e:
            sso.require_user(f"Bearer {_token(typ='service')}")
        assert e.value.detail == "Invalid token"

    def test_expired(self):
        with pytest.raises(HTTPException) as e:
            sso.require_user(f"Bearer {_token(exp_s=-10)}")
        assert e.value.detail == "Token expired"

    def test_wrong_secret(self):
        with pytest.raises(HTTPException):
            sso.require_user(f"Bearer {_token(secret='someone-elses-secret-also-32-bytes-long')}")

    def test_ws_resolver_never_raises(self):
        assert sso.user_from_token(_token()) == 7
        assert sso.user_from_token("garbage") is None
        assert sso.user_from_token(_token(exp_s=-10)) is None
