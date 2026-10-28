"""The factual changeset engine.

Only generates what the repo tree itself proves, and nothing else:
  - quality.yml      only when NO CI exists; steps derived from files present
  - codeql.yml       only when analyzable source exists and repo is public
                     (CodeQL is not free on private repos; configurable)
  - dependabot.yml   only when a supported manifest exists
  - pyproject.toml   only when requirements.txt exists and pyproject does not
  - package.json     engines + test script ONLY when tests are proven

Order matters (deliberately): packaging patches are built FIRST so the
Node quality workflow is derived from the already-patched package.json.
No cosmetic edits. No markdown files, ever.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import TOOL_TAG

WORKFLOWS_DIR = ".github/workflows/"


@dataclass
class RepoTree:
    """What the executor observed about one repo (paths are blobs only)."""
    full_name: str
    default_branch: str
    private: bool = False
    paths: tuple = ()
    files: dict = field(default_factory=dict)  # path -> text content


@dataclass
class Change:
    path: str
    content: str
    reason: str
    action: str = "create"  # create | update


# -- tree predicates ---------------------------------------------------------

def _has(tree, path):
    return path in tree.paths


def has_ci(tree):
    return any(p.startswith(WORKFLOWS_DIR) and p.endswith((".yml", ".yaml"))
               for p in tree.paths)


def has_codeql(tree):
    return any(p.startswith(WORKFLOWS_DIR) and "codeql" in p.rsplit("/", 1)[-1]
               for p in tree.paths)


def has_dependabot(tree):
    return any(p in (".github/dependabot.yml", ".github/dependabot.yaml")
               for p in tree.paths)


def python_tested(tree):
    return any(p == "tests" or p.startswith("tests/")
               or p.split("/")[-1].startswith("test_")
               or p.endswith("/conftest.py")
               for p in tree.paths)


def node_tested(tree):
    return any(p.startswith(("tests/", "test/"))
               or p.endswith((".test.js", ".test.ts", ".test.jsx",
                              ".test.mjs", ".spec.js", ".spec.ts"))
               for p in tree.paths)


def rust_tested(tree):
    return any(p == "tests" or p.startswith("tests/") for p in tree.paths)


def go_tested(tree):
    return any(p.endswith("_test.go") for p in tree.paths)


def detect_stacks(tree):
    stacks = []
    if (any(p.endswith(".py") for p in tree.paths)
            or _has(tree, "requirements.txt") or _has(tree, "pyproject.toml")):
        stacks.append("python")
    if _has(tree, "package.json"):
        stacks.append("node")
    if _has(tree, "Cargo.toml"):
        stacks.append("rust")
    if _has(tree, "go.mod"):
        stacks.append("go")
    return stacks


CODEQL_LANG = {"python": "python", "node": "javascript", "rust": "rust",
               "go": "go"}


def codeql_languages(tree):
    langs = []
    if any(p.endswith(".py") for p in tree.paths):
        langs.append("python")
    if (_has(tree, "package.json")
            or any(p.endswith((".js", ".jsx", ".ts", ".tsx")) for p in tree.paths)):
        langs.append("javascript")
    if _has(tree, "Cargo.toml") or any(p.endswith(".rs") for p in tree.paths):
        langs.append("rust")
    if _has(tree, "go.mod") or any(p.endswith(".go") for p in tree.paths):
        langs.append("go")
    if any(p.endswith(".java") for p in tree.paths):
        langs.append("java")
    return langs


DETECTORS = (  # (ecosystem, manifest path)
    ("pip", "requirements.txt"),
    ("pip", "pyproject.toml"),
    ("npm", "package.json"),
    ("cargo", "Cargo.toml"),
    ("gomod", "go.mod"),
    ("maven", "pom.xml"),
    ("gradle", "build.gradle"),
    ("gradle", "build.gradle.kts"),
    ("bundler", "Gemfile"),
    ("composer", "composer.json"),
)


def detect_ecosystems(tree):
    found = []
    for eco, manifest in DETECTORS:
        if _has(tree, manifest) and eco not in found:
            found.append(eco)
    return found


# -- YAML/TOML generators (hand-rolled templates: `on:` stays literal) -------

def _wf_header(name, branch):
    return (
        "name: %s\n"
        "on:\n"
        "  push:\n"
        "    branches: [\"%s\"]\n"
        "  pull_request:\n"
        "\n"
        "permissions:\n"
        "  contents: read\n"
        "\n"
        "jobs:\n"
        "  quality:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n" % (name, branch)
    )


def _py_deps_install(tree):
    steps = []
    if _has(tree, "requirements.txt"):
        steps.append("      - run: pip install -r requirements.txt")
    if _has(tree, "pyproject.toml"):
        steps.append("      - run: pip install .")
    return steps


def _node_version(config):
    return str(config.get("node_ci_version", "18"))


def quality_python(tree, config):
    steps = [
        "      - uses: actions/checkout@v4",
        "      - uses: actions/setup-python@v5",
        "        with:",
        "          python-version: \"3.x\"",
    ] + _py_deps_install(tree)
    if python_tested(tree):
        steps.append("      - run: python -m unittest discover")
        test_reason = "tests detected -> unittest discover"
    else:
        steps.append("      - run: python -m compileall -q .")
        test_reason = "no tests detected -> compileall smoke"
    body = "\n".join(steps) + "\n"
    reason = ("no CI found; steps derived from: %s"
              % ", ".join(filter(None, [
                  "requirements.txt" if _has(tree, "requirements.txt") else "",
                  "pyproject.toml" if _has(tree, "pyproject.toml") else "",
                  test_reason])))
    return _wf_header("quality", tree.default_branch) + body, reason


def quality_node(tree, pkg, config):
    """pkg is the PATCHED package.json dict (or the original if unpatched)."""
    pkg = pkg or {}
    scripts = pkg.get("scripts") or {}
    engines_node = (pkg.get("engines") or {}).get("node")
    version = _node_version(config)
    if engines_node:
        match = re.search(r"\d+", str(engines_node))
        if match:
            version = match.group(0)
    steps = [
        "      - uses: actions/checkout@v4",
        "      - uses: actions/setup-node@v4",
        "        with:",
        "          node-version: \"%s\"" % version,
        "      - run: %s" % ("npm ci" if _has(tree, "package-lock.json")
                             else "npm install"),
    ]
    provable = []
    if "test" in scripts:
        steps.append("      - run: npm test")
        provable.append("scripts.test present -> npm test")
    if "build" in scripts:
        steps.append("      - run: npm run build")
        provable.append("scripts.build present -> npm run build")
    if not provable:
        return None, ""  # nothing provable to run: refuse to be cosmetic
    body = "\n".join(steps) + "\n"
    reason = "no CI found; steps derived from package.json: %s" % (
        ", ".join(provable))
    return _wf_header("quality", tree.default_branch) + body, reason


def quality_rust(tree, config):
    steps = [
        "      - uses: actions/checkout@v4",
        "      - run: rustup update stable",
    ]
    notes = ["Cargo.toml present"]
    if _has(tree, "Cargo.lock"):
        steps.append("      - run: cargo build --locked")
        notes.append("Cargo.lock present -> --locked")
    else:
        steps.append("      - run: cargo build")
    if rust_tested(tree):
        steps.append("      - run: cargo test")
        notes.append("tests/ present -> cargo test")
    else:
        steps.append("      - run: cargo check")
        notes.append("no tests/ -> cargo check")
    body = "\n".join(steps) + "\n"
    reason = "no CI found; steps derived from: %s" % ", ".join(notes)
    return _wf_header("quality", tree.default_branch) + body, reason


def quality_go(tree, config):
    steps = [
        "      - uses: actions/checkout@v4",
        "      - uses: actions/setup-go@v5",
        "        with:",
        "          go-version: \"stable\"",
        "      - run: go build ./...",
    ]
    if go_tested(tree):
        steps.append("      - run: go test ./...")
        notes = "go.mod present; *_test.go present -> go test"
    else:
        steps.append("      - run: go vet ./...")
        notes = "go.mod present; no tests -> go vet"
    body = "\n".join(steps) + "\n"
    reason = "no CI found; steps derived from: %s" % notes
    return _wf_header("quality", tree.default_branch) + body, reason


def codeql_workflow(tree, langs):
    return _wf_header("codeql", tree.default_branch) + (
        "  analyze:\n"
        "    runs-on: ubuntu-latest\n"
        "    permissions:\n"
        "      security-events: write\n"
        "      contents: read\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: github/codeql-action/init@v3\n"
        "        with:\n"
        "          languages: %s\n"
        "      - uses: github/codeql-action/analyze@v3\n" % " ".join(langs))


def dependabot_yaml(ecosystems):
    blocks = []
    for eco in ecosystems:
        blocks.append(
            "  - package-ecosystem: \"%s\"\n"
            "    directory: \"/\"\n"
            "    schedule:\n"
            "      interval: \"weekly\"\n" % eco)
    return "version: 2\nupdates:\n" + "".join(blocks)


def pyproject_toml(repo_name, requirement_lines):
    name = re.sub(r"[^A-Za-z0-9._-]", "-", repo_name).strip("-.") or "project"
    deps = "".join("    %s,\n" % json.dumps(line, ensure_ascii=False)
                   for line in requirement_lines)
    return (
        "[project]\n"
        "name = %s\n"
        "version = \"0.1.0\"\n"
        "description = \"Factual packaging baseline%s\"\n"
        "dependencies = [\n%s]\n" % (json.dumps(name), TOOL_TAG, deps or "")
    )


# -- the changeset -----------------------------------------------------------

def _py_requirements(tree):
    """Non-empty, non-flag requirement lines, verbatim (inline # dropped)."""
    text = tree.files.get("requirements.txt")
    if text is None:
        return []
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        cut = re.split(r"\s#", line, maxsplit=1)[0].strip()
        if cut:
            lines.append(cut)
    return lines


def build_changeset(tree, config=None):
    """Config keys used: node_min, node_ci_version, enable_codeql_private."""
    config = dict(config or {})
    changes = []

    # 1) packaging first — later steps must see the patched state
    req_lines = None
    if (_has(tree, "requirements.txt")
            and not _has(tree, "pyproject.toml")):
        req_lines = _py_requirements(tree)
        changes.append(Change(
            "pyproject.toml", pyproject_toml(tree.full_name, req_lines),
            "dependencies verbatim from requirements.txt (%d lines)"
            % len(req_lines)))

    pkg_state = None
    if _has(tree, "package.json"):
        try:
            pkg = json.loads(tree.files.get("package.json") or "")
        except ValueError:
            pkg = None  # malformed JSON: never touched, never invented
        if isinstance(pkg, dict):
            node_min = config.get("node_min", ">=18")
            patched = []
            if node_tested(tree):
                scripts = pkg.setdefault("scripts", {})
                if "test" not in scripts:
                    scripts["test"] = "node --test"
                    patched.append("scripts.test=node --test")
                engines = pkg.setdefault("engines", {})
                if "node" not in engines:
                    engines["node"] = node_min
                    patched.append("engines.node=%s" % node_min)
            if patched:
                changes.append(Change(
                    "package.json",
                    json.dumps(pkg, indent=2, ensure_ascii=False) + "\n",
                    "tests present -> %s" % ", ".join(patched),
                    action="update"))
            pkg_state = pkg

    # 2) quality workflow — only when the repo has NO CI at all
    if not has_ci(tree):
        stacks = detect_stacks(tree)
        if stacks:
            primary = stacks[0]
            if primary == "python":
                content, reason = quality_python(tree, config)
            elif primary == "node":
                content, reason = quality_node(tree, pkg_state, config)
            elif primary == "rust":
                content, reason = quality_rust(tree, config)
            else:
                content, reason = quality_go(tree, config)
            if content:
                changes.append(Change(
                    ".github/workflows/quality.yml", content, reason))

    # 3) dependabot — only when a manifest exists
    if not has_dependabot(tree):
        ecosystems = detect_ecosystems(tree)
        if ecosystems:
            changes.append(Change(
                ".github/dependabot.yml", dependabot_yaml(ecosystems),
                "manifests present: %s" % ", ".join(ecosystems)))

    # 4) codeql — analyzable source, public repo (or explicitly enabled)
    if not has_codeql(tree):
        langs = codeql_languages(tree)
        if langs and (not tree.private
                      or config.get("enable_codeql_private")):
            changes.append(Change(
                ".github/workflows/codeql.yml", codeql_workflow(tree, langs),
                "analyzable source (%s)%s" % (
                    " ".join(langs),
                    "" if not tree.private else ", private enabled by config")))

    changes.sort(key=lambda c: c.path)
    seen = set()
    unique = []
    for change in changes:
        if change.path not in seen:
            seen.add(change.path)
            unique.append(change)
    return unique


def commit_message(changes):
    lines = ["chore(quality): factual baseline %s" % TOOL_TAG, ""]
    for change in changes:
        lines.append("- %s: %s" % (change.path, change.reason))
    return "\n".join(lines) + "\n"
