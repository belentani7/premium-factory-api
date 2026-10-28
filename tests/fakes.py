"""Shared offline fakes.

FakeGitHub simulates just enough GitHub to exercise the executor for real:
refs, trees, contents, blobs/trees/commits, ref PATCH (force is rejected
with 422, protected branches with 403). Every mutation is recorded in
``.writes`` so dry-run zero-writes is provable, not assumed.

Note for tests: the client exposes ``calls`` (int) and ``request_log``
(list) as distinct attributes on purpose.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse

import pf.ghapi


def _sha(seed):
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


class _Repo:
    def __init__(self, files=None, *, private=False, archived=False,
                 fork=False, default_branch="main", protected=False,
                 deny_write=False, language="Python"):
        self.files = dict(files or {})
        self.private = private
        self.archived = archived
        self.fork = fork
        self.default_branch = default_branch
        self.protected = protected
        self.deny_write = deny_write
        self.language = language
        self.counter = 0
        self.commits = []
        self.pending_commit = None
        self.blobs = {}
        self.tree_entries = None
        self.head = None
        self.ref_patches = []
        if self.files:
            self.commit_files(self.files, message="initial", _initial=True)

    def commit_files(self, files, message, _initial=False):
        self.counter += 1
        sha = _sha("%s-%d" % (sorted(self.files.items()), self.counter))
        self.files = dict(self.files)
        self.files.update(files)
        self.commits.append({"sha": sha, "message": message,
                             "parents": [] if _initial else
                             ([self.head] if self.head else [])})
        self.head = sha
        return sha


class FakeGitHub:
    def __init__(self, login="owner"):
        self.login = login
        self.repos = {}
        self.writes = []  # every mutating request (method, path, body)

    def add_repo(self, full_name, files=None, **kwargs):
        repo = _Repo(files, **kwargs)
        self.repos[full_name] = repo
        return repo

    # -- transport ---------------------------------------------------------

    def transport(self, method, url, headers, body, timeout):
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        payload = json.loads(body.decode("utf-8")) if body else None
        try:
            status, obj = self.route(method, path, query, payload)
        except Exception as exc:  # pragma: no cover - surfaces fake bugs
            status, obj = 500, {"message": "fake bug: %r" % exc}
        encoded = json.dumps(obj).encode("utf-8") if obj is not None else b""
        headers_out = {"x-ratelimit-remaining": "4990",
                       "x-ratelimit-reset": "0",
                       "content-type": "application/json"}
        return status, headers_out, encoded

    def _mutating(self, method, path, payload):
        if method in ("POST", "PATCH", "PUT", "DELETE"):
            self.writes.append({"method": method, "path": path,
                                "body": payload})

    def route(self, method, path, query, payload):
        if method == "GET" and path == "/user":
            return 200, {"login": self.login}

        if method == "GET" and path == "/user/repos":
            names = sorted(self.repos)
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per_page", ["30"])[0])
            chunk = names[(page - 1) * per_page: page * per_page]
            return 200, [self._repo_payload(name) for name in chunk]

        match = re.match(r"^/repos/([^/]+/[^/]+)(/.*)?$", path)
        if match:
            full_name, rest = match.group(1), match.group(2) or ""
            if full_name not in self.repos:
                return 404, {"message": "Not Found"}
            repo = self.repos[full_name]
            if rest == "" and method == "GET":
                return 200, self._repo_payload(full_name)
            if rest.startswith("/git/ref/heads/"):
                branch = rest.rsplit("/", 1)[-1]
                if branch != repo.default_branch or repo.head is None:
                    return 404, {"message": "ref not found"}
                return 200, {"object": {"sha": repo.head}}
            if rest.startswith("/git/trees/"):
                return 200, {
                    "truncated": False,
                    "tree": [{"path": p, "type": "blob"}
                             for p in sorted(repo.files)]}
            commit_match = re.match(r"^/git/commits/[0-9a-f]+$", rest)
            if commit_match and method == "GET":
                return 200, {"sha": rest.rsplit("/", 1)[-1],
                             "tree": {"sha": _sha("tree-" + str(repo.head))},
                             "parents": []}
            if rest.startswith("/contents/"):
                rel = urllib.parse.unquote(
                    rest[len("/contents/"):]).split("?")[0]
                if rel not in repo.files:
                    return 404, {"message": "No commit history for %s" % rel}
                content = base64.b64encode(
                    repo.files[rel].encode("utf-8")).decode("ascii")
                return 200, {"encoding": "base64", "content": content}
            if rest == "/git/blobs" and method == "POST":
                self._mutating(method, path, payload)
                repo.counter += 1
                sha = _sha("blob-%d" % repo.counter)
                repo.blobs[sha] = payload.get("content", "")
                return 201, {"sha": sha}
            if rest == "/git/trees" and method == "POST":
                self._mutating(method, path, payload)
                repo.counter += 1
                repo.tree_entries = payload.get("tree", [])
                return 201, {"sha": _sha("tree-%d" % repo.counter)}
            if rest == "/git/commits" and method == "POST":
                self._mutating(method, path, payload)
                obj = self._commit_object(repo, payload)
                repo.pending_commit = obj
                return 201, obj
            if rest.startswith("/git/refs/heads/") and method == "PATCH":
                self._mutating(method, path, payload)
                if payload.get("force"):
                    return 422, {"message": "cannot force update a ref"}
                if repo.protected:
                    return 403, {"message": "protected branch hook declined"}
                if repo.deny_write:
                    return 403, {"message":
                                 "resource not accessible by integration"}
                repo.ref_patches.append(payload)
                repo.head = payload["sha"]
                repo.commits.append(repo.pending_commit
                                    or {"sha": payload["sha"],
                                        "message": "patched", "parents": []})
                repo.pending_commit = None
                for entry in repo.tree_entries or []:
                    if entry.get("sha") in repo.blobs:
                        repo.files[entry["path"]] = repo.blobs[entry["sha"]]
                repo.tree_entries = None
                return 200, {"object": {"sha": payload["sha"]}}
            match_branch = re.match(r"^/branches/([^/]+)/protection$", rest)
            if match_branch and method == "GET":
                if repo.protected:
                    return 200, {"enabled": True}
                return 404, {"message": "Branch not protected"}
            match_collab = re.match(r"^/collaborators$", rest)
            if match_collab:
                return 200, []
        if method == "POST" and path == "/graphql":
            self._mutating(method, path, payload)
            return 200, {"data": {"viewer": {"login": self.login}}}
        return 404, {"message": "fake has no route for %s %s"
                     % (method, path)}

    def _commit_object(self, repo, payload):
        repo.counter += 1
        return {"sha": _sha("commit-%d" % repo.counter),
                "tree": payload.get("tree"),
                "message": payload.get("message", ""),
                "parents": payload.get("parents", [])}

    def _repo_payload(self, full_name):
        repo = self.repos[full_name]
        return {
            "full_name": full_name,
            "name": full_name.split("/")[-1],
            "default_branch": repo.default_branch,
            "private": repo.private,
            "archived": repo.archived,
            "fork": repo.fork,
            "language": repo.language,
            "clone_url": "https://github.com/%s.git" % full_name,
            "permissions": {"push": not repo.deny_write},
        }

    # -- assertions helpers ------------------------------------------------

    @property
    def write_count(self):
        return len(self.writes)

    def new_commits_on(self, full_name):
        return len(self.repos[full_name].commits)


def make_client(github, **kwargs):
    """A GitHubAPI wired to the fake, with no network at all."""
    kwargs.setdefault("transport", github.transport)
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("min_rate_remaining", 10)
    return pf.ghapi.GitHubAPI("test-token", "https://api.github.test", **kwargs)
