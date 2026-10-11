"""
The shared HTTP plumbing: one retry loop, one bearer-request definition.

Everything that fetches a timeseries goes through these two pieces; the
consumers keep their own requests.get call (the test seam) and their own error
messages — this is transport only.
"""

import pytest
import requests

from src.interfaces.http_fetch import bearer_request_kwargs, retry_request


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
