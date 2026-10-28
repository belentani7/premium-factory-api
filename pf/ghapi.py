"""Direct GitHub REST + GraphQL client. Standard library only.

Two request counters exist on purpose and must never collide:
  - ``self.calls``       -> int, total requests issued (rate bookkeeping)
  - ``self.request_log`` -> list of per-request dicts (tests/fakes attach here)
"""

from __future__ import annotations

import base64
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

API_VERSION = "2022-11-28"
USER_AGENT = "premium-factory-api/2.0"


class APIError(Exception):
    def __init__(self, status, message, headers=None):
        super().__init__("HTTP %s: %s" % (status, message))
        self.status = status
        self.message = message
        self.headers = headers or {}


class NotFound(APIError):
    pass


class Forbidden(APIError):
    pass


class RateLimitExceeded(APIError):
    pass


def _urllib_transport(method, url, headers, body, timeout):
    """Return (status, headers-dict, body-bytes). HTTPError is a response."""
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx are responses, not crashes
        return exc.code, dict(exc.headers.items()), exc.read()


def _h(headers, name):
    """Case-insensitive header lookup."""
    lname = name.lower()
    for key, value in headers.items():
        if key.lower() == lname:
            return value
    return None


def _error_message(raw):
    try:
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict) and data.get("message"):
            return data["message"]
    except Exception:
        pass
    return (raw[:200].decode("utf-8", "replace")) if raw else "unknown error"


class GitHubAPI:
    def __init__(self, token, api_url="https://api.github.com", *, timeout=30,
                 retries=3, min_rate_remaining=100, max_rate_wait=900,
                 transport=None, sleep=None, now=None):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.min_rate_remaining = min_rate_remaining
        self.max_rate_wait = max_rate_wait
        self._transport = transport or _urllib_transport
        self._sleep = sleep or time.sleep
        self._now = now or time.time
        self.calls = 0
        self.request_log = []
        self.rate_remaining = None
        self.rate_reset = None

    # -- core ------------------------------------------------------------

    def request(self, method, path, *, body=None, accept=None):
        url = path if path.startswith("http") else self.api_url + path
        headers = {
            "Authorization": "Bearer %s" % self.token,
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            self._ensure_budget()
            self.calls += 1
            status, rheaders, rbody = self._transport(
                method, url, headers, data, self.timeout)
            self._absorb_rate_headers(rheaders)
            self.request_log.append(
                {"method": method, "path": path, "status": status})
            transient = status in (502, 503, 504) or (
                status == 403 and _h(rheaders, "retry-after")
                and self.rate_remaining != 0)
            if transient and attempt < self.retries:
                delay = float(_h(rheaders, "retry-after") or 0)
                if not delay:
                    delay = (1.5 ** attempt) + random.random() * 0.1
                attempt += 1
                self._sleep(delay)
                continue
            break

        if 200 <= status < 300:
            return self._decode(rbody)
        message = _error_message(rbody)
        if status == 404:
            raise NotFound(status, message, rheaders)
        if status in (403, 429):
            if self.rate_remaining == 0:
                raise RateLimitExceeded(status, message, rheaders)
            raise Forbidden(status, message, rheaders)
        raise APIError(status, message, rheaders)

    def _decode(self, raw):
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return raw

    def _absorb_rate_headers(self, headers):
        remaining = _h(headers, "x-ratelimit-remaining")
        reset = _h(headers, "x-ratelimit-reset")
        if remaining is not None:
            try:
                self.rate_remaining = int(remaining)
            except (TypeError, ValueError):
                pass
        if reset is not None:
            try:
                self.rate_reset = int(reset)
            except (TypeError, ValueError):
                pass

    def _ensure_budget(self):
        """Hard floor: below min_rate_remaining, wait for reset or stop."""
        if self.rate_remaining is None or self.rate_remaining >= self.min_rate_remaining:
            return
        wait = max(0.0, (self.rate_reset or 0) - self._now()) + 1.0
        if wait > self.max_rate_wait:
            raise RateLimitExceeded(
                403, "rate budget %s < floor %s; reset in %ds"
                % (self.rate_remaining, self.min_rate_remaining, int(wait)))
        self._sleep(wait)
        self.rate_remaining = None  # unknown until the next response

    # -- verb helpers ----------------------------------------------------

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body):
        return self.request("POST", path, body=body)

    def patch(self, path, body):
        return self.request("PATCH", path, body=body)

    def graphql(self, query, variables=None):
        return self.request("POST", "/graphql",
                            body={"query": query, "variables": variables or {}})

    def paginate(self, path, params=None, per_page=100, max_pages=100):
        params = dict(params or {})
        page = 1
        while True:
            merged = dict(params)
            merged["per_page"] = per_page
            merged["page"] = page
            query = urllib.parse.urlencode(merged)
            sep = "&" if "?" in path else "?"
            items = self.get("%s%s%s" % (path, sep, query))
            if not isinstance(items, list):
                raise APIError(0, "expected a JSON list from %s" % path)
            for item in items:
                yield item
            if len(items) < per_page or page >= max_pages:
                return
            page += 1

    # -- domain ----------------------------------------------------------

    def get_user(self):
        return self.get("/user")

    def list_repos(self):
        """All repos the token can see (owner+collaborator+org member)."""
        return list(self.paginate(
            "/user/repos",
            {"visibility": "all", "sort": "full_name"}))

    def get_content_text(self, full_name, path, ref):
        data = self.get("/repos/%s/contents/%s?ref=%s"
                        % (full_name, urllib.parse.quote(path),
                           urllib.parse.quote(ref)))
        if not isinstance(data, dict):
            raise APIError(0, "unexpected content payload for %s" % path)
        if data.get("encoding") == "base64":
            return base64.b64decode(data.get("content", "")).decode("utf-8")
        return data.get("content", "") or ""
