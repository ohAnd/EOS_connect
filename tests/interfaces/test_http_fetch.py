"""
The shared HTTP plumbing: one retry loop, one bearer-request definition.

Everything that fetches a timeseries goes through these two pieces; the
consumers keep their own requests.get call (the test seam) and their own error
messages — this is transport only.
"""

import pytest
import requests

from src.interfaces.http_fetch import (
    ESCALATING_TIMEOUT_BASE_S,
    ESCALATING_TIMEOUT_CAP_S,
    bearer_request_kwargs,
    escalating_timeouts,
    retry_request,
)


class TestBearerRequestKwargs:
    def test_a_token_becomes_a_bearer_header(self):
        kwargs = bearer_request_kwargs("secret")

        assert kwargs["headers"]["Authorization"] == "Bearer secret"
        assert kwargs["headers"]["Content-Type"] == "application/json"
        assert kwargs["timeout"] == 10

    def test_no_token_means_no_authorization_header(self):
        kwargs = bearer_request_kwargs(None)

        assert "Authorization" not in kwargs["headers"]

    def test_verify_appears_only_when_certificates_are_being_ignored(self):
        # requests' default is verify=True; passing it unconditionally would
        # break every test fake whose signature has no verify parameter.
        assert "verify" not in bearer_request_kwargs("t")
        assert bearer_request_kwargs("t", ssl_ignore=True)["verify"] is False

    def test_timeout_is_adjustable(self):
        assert bearer_request_kwargs(None, timeout=5)["timeout"] == 5


class TestRetryRequest:
    def test_first_success_returns_immediately(self):
        calls = []

        def request_func():
            calls.append(1)
            return "payload"

        result = retry_request(request_func, lambda kind, exc: ("error", kind))

        assert result == "payload"
        assert len(calls) == 1

    @pytest.mark.parametrize(
        "exception,expected_kind",
        [
            (requests.exceptions.Timeout(), "timeout"),
            (requests.exceptions.ConnectionError(), "request_failed"),
            (ValueError("bad json"), "invalid_json"),
            (KeyError("missing"), "parsing_error"),
        ],
    )
    def test_the_error_handler_sees_the_failure_kind(self, exception, expected_kind):
        def request_func():
            raise exception

        result = retry_request(
            request_func, lambda kind, exc: kind, max_retries=2, delay=0
        )

        assert result == expected_kind

    def test_a_later_attempt_can_still_succeed(self):
        attempts = []

        def request_func():
            attempts.append(1)
            if len(attempts) < 2:
                raise requests.exceptions.ConnectionError()
            return "recovered"

        result = retry_request(
            request_func, lambda kind, exc: "gave up", max_retries=3, delay=0
        )

        assert result == "recovered"
        assert len(attempts) == 2


class TestEscalatingTimeouts:
    """
    Repeating a request with the timeout that just expired cannot succeed against a
    server that is only slow; the ladder is what turns the retry into a real second
    chance without spending another request (#358).
    """

    @pytest.mark.parametrize(
        "attempts,expected",
        [
            (2, (5, 30)),
            (3, (5, 12, 30)),  # Forecast.Solar and Open-Meteo
            (5, (5, 8, 12, 19, 30)),  # akkudoktor PV
        ],
    )
    def test_the_ladder_is_pinned_to_its_ends(self, attempts, expected):
        assert escalating_timeouts(attempts) == expected

    @pytest.mark.parametrize("attempts", [2, 3, 4, 5, 8])
    def test_every_ladder_starts_at_the_base_and_ends_at_the_cap(self, attempts):
        ladder = escalating_timeouts(attempts)

        assert len(ladder) == attempts
        assert ladder[0] == ESCALATING_TIMEOUT_BASE_S
        assert ladder[-1] == ESCALATING_TIMEOUT_CAP_S

    @pytest.mark.parametrize("attempts", [2, 3, 4, 5, 8])
    def test_an_attempt_never_gets_less_time_than_the_one_before(self, attempts):
        ladder = escalating_timeouts(attempts)

        assert list(ladder) == sorted(ladder)

    def test_a_single_attempt_gets_the_base(self):
        # Not the cap: one attempt is the caller saying "try once", and the whole
        # point of the base is that a healthy API fails fast.
        assert escalating_timeouts(1) == (ESCALATING_TIMEOUT_BASE_S,)
        assert escalating_timeouts(0) == (ESCALATING_TIMEOUT_BASE_S,)

    def test_a_cap_below_the_base_does_not_make_the_ladder_shrink(self):
        assert escalating_timeouts(3, base=10, cap=4) == (10, 10, 10)

    def test_the_ends_are_configurable(self):
        assert escalating_timeouts(3, base=2, cap=8) == (2, 4, 8)


class TestRetryRequestWithEscalatingTimeouts:
    def test_the_ladder_replaces_the_retry_count(self):
        attempts = []

        def request_func(timeout):
            attempts.append(timeout)
            raise requests.exceptions.Timeout()

        retry_request(
            request_func,
            lambda kind, exc: kind,
            max_retries=99,  # ignored - the ladder says how many attempts there are
            delay=0,
            timeouts=(5, 12, 30),
        )

        assert attempts == [5, 12, 30]

    def test_a_longer_timeout_lets_a_slow_server_finish(self):
        attempts = []

        def request_func(timeout):
            attempts.append(timeout)
            if timeout < 12:  # the server needs more than the first attempt allows
                raise requests.exceptions.Timeout()
            return "forecast"

        result = retry_request(
            request_func, lambda kind, exc: "gave up", delay=0, timeouts=(5, 12, 30)
        )

        assert result == "forecast"
        assert attempts == [5, 12]  # no third request against the quota

    def test_the_error_handler_still_sees_the_failure_kind(self):
        def request_func(_timeout):
            raise requests.exceptions.Timeout()

        result = retry_request(
            request_func, lambda kind, exc: kind, delay=0, timeouts=(5, 12)
        )

        assert result == "timeout"

    def test_an_empty_ladder_falls_back_to_the_retry_count(self):
        # Guards the `tuple(timeouts) if timeouts else ...` branch: an empty
        # sequence must not mean "zero attempts, return None".
        attempts = []

        def request_func():
            attempts.append(1)
            return "payload"

        assert retry_request(request_func, lambda k, e: k, timeouts=()) == "payload"
        assert len(attempts) == 1
