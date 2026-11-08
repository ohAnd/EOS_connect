"""
Shared HTTP plumbing for the timeseries fetchers.

One retry loop and one bearer-request definition instead of per-interface
copies: the two ``_retry_request`` methods had drifted only in their docstrings,
and of the five hand-rolled bearer blocks only the managed-load one honoured
``ssl_ignore``. This module is transport, not messaging — the probe keeps its
curated status mapping on top, the interfaces keep their own error handlers.

The request itself stays a ``requests.get`` call in each consumer module (fed
by `bearer_request_kwargs`) rather than being wrapped here: the test suites
patch each module's ``requests.get`` as their seam, and ``verify`` is only
passed when certificate checking is actually being switched off, so existing
fakes keep their signatures.
"""

import time

import requests

DEFAULT_TIMEOUT_SECONDS = 10

# Bounds for `escalating_timeouts`.  The base is what a healthy public forecast API
# needs several times over, so the common case still fails fast; the cap is the
# patience a slow spell is worth before the cached forecast is the better answer.
ESCALATING_TIMEOUT_BASE_S = 5
ESCALATING_TIMEOUT_CAP_S = 30


def escalating_timeouts(
    attempts, base=ESCALATING_TIMEOUT_BASE_S, cap=ESCALATING_TIMEOUT_CAP_S
):
    """
    Per-attempt timeouts that grow from ``base`` to ``cap``.

    Repeating a request with the timeout that just expired cannot succeed against a
    server that is merely slow - it only spends the request budget (Forecast.Solar
    meters the public tier at 12 per hour, see issue #358).  Giving each attempt more
    time than the last turns the same number of requests into a real second chance.

    The ladder is defined by its ends rather than by a growth factor, so the attempt
    count can change without anyone recomputing where it lands: the first attempt gets
    ``base``, the last gets ``cap``, and the ones between are spread geometrically and
    rounded to whole seconds.

        escalating_timeouts(2) -> (5, 30)
        escalating_timeouts(3) -> (5, 12, 30)
        escalating_timeouts(5) -> (5, 8, 12, 19, 30)

    Args:
        attempts: Number of attempts the ladder has to cover.
        base: Timeout of the first attempt, in seconds.
        cap: Timeout of the last attempt, in seconds. Clamped up to ``base`` when
            someone passes a cap below it, so the ladder never shrinks.

    Returns:
        tuple: One timeout per attempt, non-decreasing.
    """
    cap = max(base, cap)
    if attempts <= 1:
        return (base,)
    ratio = cap / base
    return tuple(
        round(base * ratio ** (step / (attempts - 1))) for step in range(attempts)
    )


def bearer_request_kwargs(token=None, timeout=DEFAULT_TIMEOUT_SECONDS, ssl_ignore=False):
    """
    Keyword arguments for a JSON GET with optional bearer auth.

    Args:
        token: Bearer token; omitted from the headers when falsy.
        timeout: Per-request timeout in seconds.
        ssl_ignore: Skip certificate verification, for a self-signed instance.
            Only then is ``verify`` included at all — True is requests' default.

    Returns:
        dict: pass as ``requests.get(url, **bearer_request_kwargs(...))``.
    """
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    kwargs = {"headers": headers, "timeout": timeout}
    if ssl_ignore:
        kwargs["verify"] = False
    return kwargs


def retry_request(request_func, error_handler, max_retries=3, delay=1, timeouts=None):
    """
    Centralized retry logic for API requests.

    Args:
        request_func (callable): Function that performs the request and returns
            the result. Called with no arguments, or with the attempt's timeout
            when ``timeouts`` is given - see below.
        error_handler (callable): Called as ``error_handler(kind, exception)``
            on final failure; its return value is returned.
        max_retries (int): Attempts before the error handler is called. Ignored
            when ``timeouts`` is given.
        delay (int): Delay in seconds between attempts.
        timeouts (sequence, optional): One timeout per attempt, typically from
            `escalating_timeouts`. Its length replaces ``max_retries``, and each
            attempt's value is passed to ``request_func`` as its only argument -
            so a caller that wants a growing timeout writes
            ``def request_func(timeout): requests.get(url, timeout=timeout)``.
            Left out, ``request_func`` keeps being called with no arguments: the
            pure parse closures that also come through here have no timeout to
            take, and neither do the callers whose timeout is already fixed.

    Returns:
        The result of request_func, or the error handler's result on failure.
    """
    attempts = tuple(timeouts) if timeouts else (None,) * max_retries
    last = len(attempts) - 1
    for attempt, timeout in enumerate(attempts):
        try:
            return request_func() if timeout is None else request_func(timeout)
        except requests.exceptions.Timeout as exc:
            if attempt == last:
                return error_handler("timeout", exc)
        except requests.exceptions.RequestException as exc:
            if attempt == last:
                return error_handler("request_failed", exc)
        except (ValueError, TypeError) as exc:
            if attempt == last:
                return error_handler("invalid_json", exc)
        except (KeyError, AttributeError) as exc:
            if attempt == last:
                return error_handler("parsing_error", exc)
        time.sleep(delay)
    return None
