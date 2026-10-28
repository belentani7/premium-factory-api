import json
import unittest

from pf import ghapi
from tests.fakes import FakeGitHub, make_client


class Transport:
    """Scriptable low-level transport capturing everything it sees."""

    def __init__(self, responses):
        # responses: list of (status, headers, body-obj); last one repeats
        self.responses = list(responses)
        self.requests = []
        self.sleeps = []

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append({
            "method": method, "url": url,
            "headers": dict(headers),
            "body": json.loads(body.decode("utf-8")) if body else None,
        })
        status, rheaders, obj = self.responses.pop(0) if len(
            self.responses) > 1 else self.responses[0]
        encoded = json.dumps(obj).encode("utf-8") if obj is not None else b""
        return status, rheaders, encoded


def base_headers(**extra):
    headers = {"x-ratelimit-remaining": "4999",
               "x-ratelimit-reset": "0"}
    headers.update(extra)
    return headers


class TestGhapi(unittest.TestCase):
    def test_auth_headers_sent(self):
        t = Transport([(200, base_headers(), {"login": "o"})])
        client = ghapi.GitHubAPI("tok-123", "https://api.test",
                                 transport=t, sleep=lambda s: None)
        client.get("/user")
        sent = t.requests[0]["headers"]
        self.assertEqual(sent["Authorization"], "Bearer tok-123")
        self.assertEqual(sent["X-GitHub-Api-Version"], ghapi.API_VERSION)
        self.assertIn("premium-factory", sent["User-Agent"])

    def test_pagination_uses_100_per_page(self):
        t = Transport([
            (200, base_headers(), list(range(100))),
            (200, base_headers(), list(range(100, 150))),
        ])
        client = ghapi.GitHubAPI("t", "https://api.test", transport=t,
                                 sleep=lambda s: None)
        items = list(client.paginate("/user/repos"))
        self.assertEqual(items, list(range(150)))
        self.assertEqual(client.calls, 2)  # int counter
        self.assertEqual(len(client.request_log), 2)  # separate list
        self.assertIn("page=2", t.requests[1]["url"])

    def test_rate_headers_tracked(self):
        t = Transport([(200, {"x-ratelimit-remaining": "4321",
                              "x-ratelimit-reset": "1700000000"}, {})])
        client = ghapi.GitHubAPI("t", "https://api.test", transport=t,
                                 sleep=lambda s: None)
        client.get("/x")
        self.assertEqual(client.rate_remaining, 4321)
        self.assertEqual(client.rate_reset, 1700000000)

    def test_retry_on_503_then_success(self):
        t = Transport([
            (503, base_headers(), {"message": "down"}),
            (503, base_headers(), {"message": "down"}),
            (200, base_headers(), {"ok": True}),
        ])
        client = ghapi.GitHubAPI("t", "https://api.test", transport=t,
                                 sleep=t.sleeps.append)
        data = client.get("/x")
        self.assertEqual(data, {"ok": True})
        self.assertEqual(client.calls, 3)
        self.assertEqual(len(t.sleeps), 2)

    def test_secondary_limit_uses_retry_after(self):
        t = Transport([
            (403, dict(base_headers(), **{"retry-after": "2"}), {}),
            (200, base_headers(), {"ok": 1}),
        ])
        client = ghapi.GitHubAPI("t", "https://api.test", transport=t,
                                 sleep=t.sleeps.append)
        client.get("/x")
        self.assertEqual(t.sleeps, [2.0])

    def test_hard_budget_stop_raises(self):
        t = Transport([(200, base_headers(), {})])
        client = ghapi.GitHubAPI("t", "https://api.test", transport=t,
                                 sleep=lambda s: None, min_rate_remaining=50,
                                 max_rate_wait=900, now=lambda: 1000)
        client.rate_remaining = 5
        client.rate_reset = 99999  # far future -> wait > max -> clean stop
        with self.assertRaises(ghapi.RateLimitExceeded):
            client.get("/x")

    def test_graphql_posts_to_graphql_endpoint(self):
        t = Transport([(200, base_headers(), {"data": {"x": 1}})])
        client = ghapi.GitHubAPI("t", "https://api.test", transport=t,
                                 sleep=lambda s: None)
        data = client.graphql("query { viewer { login } }", {"a": 1})
        self.assertEqual(data["data"], {"x": 1})
        self.assertTrue(t.requests[0]["url"].endswith("/graphql"))
        self.assertEqual(t.requests[0]["body"],
                         {"query": "query { viewer { login } }",
                          "variables": {"a": 1}})

    def test_error_classification(self):
        cases = [(404, ghapi.NotFound), (403, ghapi.Forbidden)]
        for status, expected in cases:
            t = Transport([(status, base_headers(), {"message": "nope"})])
            client = ghapi.GitHubAPI("t", "https://api.test", transport=t,
                                     sleep=lambda s: None)
            with self.assertRaises(expected):
                client.get("/x")

    def test_fake_client_wiring(self):
        github = FakeGitHub(login="owner")
        github.add_repo("o/r1", files={"a.txt": "x"})
        client = make_client(github)
        user = client.get_user()
        self.assertEqual(user["login"], "owner")
        repos = list(client.paginate("/user/repos"))
        self.assertEqual([r["full_name"] for r in repos], ["o/r1"])
        self.assertIsInstance(client.calls, int)
        self.assertIsInstance(client.request_log, list)


if __name__ == "__main__":
    unittest.main()
