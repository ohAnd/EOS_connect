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


def retry_request(request_func, error_handler, max_retries=3, delay=1):
    """
    Centralized retry logic for API requests.

    Args:
        request_func (callable): Function that performs the request and returns
            the result.
        error_handler (callable): Called as ``error_handler(kind, exception)``
            on final failure; its return value is returned.
        max_retries (int): Attempts before the error handler is called.
        delay (int): Delay in seconds between attempts.

    Returns:
        The result of request_func, or the error handler's result on failure.
    """
    for attempt in range(max_retries):
        try:
            return request_func()
        except requests.exceptions.Timeout as exc:
            if attempt == max_retries - 1:
                return error_handler("timeout", exc)
        except requests.exceptions.RequestException as exc:
            if attempt == max_retries - 1:
                return error_handler("request_failed", exc)
        except (ValueError, TypeError) as exc:
            if attempt == max_retries - 1:
                return error_handler("invalid_json", exc)
        except (KeyError, AttributeError) as exc:
            if attempt == max_retries - 1:
                return error_handler("parsing_error", exc)
        time.sleep(delay)
    return None
