"""HTTP for the provider clients: never follows a redirect, bounded size.

Requests carry credentials (Authorization headers, tokens, passwords), so a
redirect is refused rather than followed: with `NoRedirect` urllib raises
HTTPError with the 3xx status. A client that words the refusal itself passes
its own handler as `refuse`.
"""
import json
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class TooLarge(ValueError):
    pass


def open_url(request, timeout, direct=False, refuse=NoRedirect):
    """Open `request` without following redirects; `direct` also ignores proxy settings."""
    handlers = [urllib.request.ProxyHandler({})] if direct else []
    return urllib.request.build_opener(*handlers, refuse()).open(request, timeout=timeout)


def read(response, limit):
    """The whole body, or TooLarge when it exceeds `limit` bytes."""
    body = response.read(limit + 1)
    if len(body) > limit:
        raise TooLarge('The server returned an oversized response')
    return body


def fetch_json(request, timeout, limit, direct=False, refuse=NoRedirect):
    """Decoded JSON from `request`; raises HTTPError, OSError or ValueError (TooLarge included)."""
    with open_url(request, timeout, direct, refuse) as response:
        return json.loads(read(response, limit))
