import json
import unittest

from pf.changes import (RepoTree, build_changeset, commit_message,
                        dependabot_yaml, pyproject_toml)

PY_REQS = "flask==2.3.2\nrequests>=2.0  # pinned by hand\n-r other.txt\n\n"


def py_tree(**kwargs):
    defaults = dict(
        full_name="o/py-legacy", default_branch="main", private=False,
        paths=("requirements.txt", "main.py", "tests/test_main.py",
               "tests/__init__.py"),
        files={"requirements.txt": PY_REQS})
    defaults.update(kwargs)
    return RepoTree(**defaults)


def by_path(changes):
    return {c.path: c for c in changes}


class TestChangesPython(unittest.TestCase):
    def test_full_baseline_for_legacy_python_repo(self):
        changes = build_changeset(py_tree())
        paths = by_path(changes)
        self.assertEqual(sorted(paths), [
            ".github/dependabot.yml",
            ".github/workflows/codeql.yml",
            ".github/workflows/quality.yml",
            "pyproject.toml",
        ])
        quality = paths[".github/workflows/quality.yml"].content
        self.assertIn("pip install -r requirements.txt", quality)
        self.assertIn("python -m unittest discover", quality)
        self.assertIn('branches: ["main"]', quality)
        pyproject = paths["pyproject.toml"].content
        self.assertIn('"flask==2.3.2"', pyproject)   # verbatim
        self.assertIn('"requests>=2.0"', pyproject)  # inline comment dropped
        self.assertNotIn("-r other.txt", pyproject)  # flags are not deps
        dependabot = paths[".github/dependabot.yml"].content
        self.assertIn('package-ecosystem: "pip"', dependabot)
        codeql = paths[".github/workflows/codeql.yml"].content
        self.assertIn("languages: python", codeql)
        for change in changes:
            self.assertTrue(change.reason)

    def test_existing_ci_means_no_quality_workflow(self):
        tree = py_tree(paths=("requirements.txt", "main.py",
                              ".github/workflows/ci.yml"),
                       files={"requirements.txt": PY_REQS,
                              ".github/workflows/ci.yml": "name: ci\n"})
        paths = by_path(build_changeset(tree))
        self.assertNotIn(".github/workflows/quality.yml", paths)
        self.assertIn("pyproject.toml", paths)

    def test_no_requirements_no_pyproject(self):
        tree = py_tree(paths=("main.py", "tests/test_main.py"), files={})
        paths = by_path(build_changeset(tree))
        self.assertNotIn("pyproject.toml", paths)
        self.assertIn(".github/workflows/quality.yml", paths)


class TestChangesNode(unittest.TestCase):
    def node_tree(self, pkg, extra=(), private=False):
        paths = ["package.json"] + list(extra)
        return RepoTree(full_name="o/node-app", default_branch="main",
                        private=private, paths=tuple(paths),
                        files={"package.json": json.dumps(pkg)})

    def test_patch_first_then_workflow_derives_from_patched_json(self):
        # no test script, no engines, tests present, no lockfile, no CI
        pkg = {"name": "node-app", "version": "1.0.0"}
        tree = self.node_tree(pkg, extra=("tests/app.test.js", "index.js"))
        changes = build_changeset(tree)
        paths = by_path(changes)
        patched = json.loads(paths["package.json"].content)
        self.assertEqual(patched["scripts"]["test"], "node --test")
        self.assertEqual(patched["engines"]["node"], ">=18")
        self.assertEqual(paths["package.json"].action, "update")
        quality = paths[".github/workflows/quality.yml"].content
        # the workflow must reflect the PATCHED package.json (npm test exists)
        self.assertIn("npm test", quality)
        self.assertIn("npm install", quality)
        self.assertIn('node-version: "18"', quality)
        self.assertIn(".github/workflows/codeql.yml", paths)

    def test_node_without_tests_is_never_patched(self):
        pkg = {"name": "lib", "scripts": {"build": "webpack"}}
        tree = self.node_tree(pkg, extra=("index.js",))
        paths = by_path(build_changeset(tree))
        self.assertNotIn("package.json", paths)
        quality = paths[".github/workflows/quality.yml"].content
        self.assertNotIn("npm test", quality)
        self.assertIn("npm run build", quality)

    def test_node_nothing_provable_means_no_quality(self):
        pkg = {"name": "lib", "scripts": {"lint": "eslint ."}}
        tree = self.node_tree(pkg, extra=("index.js",))
        paths = by_path(build_changeset(tree))
        self.assertNotIn(".github/workflows/quality.yml", paths)
        self.assertIn(".github/dependabot.yml", paths)  # manifest proven

    def test_lockfile_uses_npm_ci(self):
        pkg = {"name": "lib", "scripts": {"test": "jest"}}
        tree = self.node_tree(pkg, extra=("package-lock.json", "index.js"))
        quality = by_path(build_changeset(
            tree))[".github/workflows/quality.yml"].content
        self.assertIn("npm ci", quality)


class TestChangesRust(unittest.TestCase):
    def test_rust_repo_builds_cargo_steps(self):
        tree = RepoTree(
            full_name="o/rust-cli", default_branch="main",
            paths=("Cargo.toml", "Cargo.lock", "src/main.rs", "tests/cli.rs"),
            files={})
        paths = by_path(build_changeset(tree))
        quality = paths[".github/workflows/quality.yml"].content
        self.assertIn("rustup update stable", quality)
        self.assertIn("cargo build --locked", quality)
        self.assertIn("cargo test", quality)
        self.assertNotIn("cargo check", quality)
        self.assertIn('package-ecosystem: "cargo"',
                      paths[".github/dependabot.yml"].content)
        self.assertIn("rust", paths[".github/workflows/codeql.yml"].content)

    def test_rust_without_lock_or_tests_uses_check(self):
        tree = RepoTree(full_name="o/rust-lib", default_branch="main",
                        paths=("Cargo.toml", "src/lib.rs"), files={})
        quality = by_path(build_changeset(
            tree))[".github/workflows/quality.yml"].content
        self.assertIn("- run: cargo build\n", quality)
        self.assertIn("cargo check", quality)
        self.assertNotIn("cargo test", quality)


class TestChangesGates(unittest.TestCase):
    def test_codeql_gated_on_private_repos(self):
        tree = py_tree(private=True)
        paths = by_path(build_changeset(tree))
        self.assertNotIn(".github/workflows/codeql.yml", paths)
        paths = by_path(build_changeset(tree, {"enable_codeql_private": True}))
        self.assertIn(".github/workflows/codeql.yml", paths)

    def test_codeql_not_duplicated(self):
        tree = py_tree(
            paths=py_tree().paths + (".github/workflows/codeql.yml",))
        paths = by_path(build_changeset(tree))
        self.assertNotIn(".github/workflows/codeql.yml", paths)

    def test_dependabot_all_ecosystems_once(self):
        tree = py_tree(paths=("requirements.txt", "package.json",
                              "Cargo.toml", "main.py"),
                       files={"requirements.txt": "flask\n",
                              "package.json": "{}"})
        dependabot = by_path(build_changeset(
            tree))[".github/dependabot.yml"].content
        self.assertIn('"pip"', dependabot)
        self.assertIn('"npm"', dependabot)
        self.assertIn('"cargo"', dependabot)

    def test_nothing_provable_is_an_empty_changeset(self):
        tree = RepoTree(full_name="o/data-only", default_branch="main",
                        paths=("data.json", "notes.csv"), files={})
        self.assertEqual(build_changeset(tree), [])

    def test_changeset_sorted_and_deduplicated(self):
        changes = build_changeset(py_tree())
        paths = [c.path for c in changes]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(paths)))


class TestGeneratedFormats(unittest.TestCase):
    def test_every_yaml_parses_and_on_stays_literal(self):
        yaml = self.import_yaml()  # skips when PyYAML is not installed
        tree = py_tree()
        for change in build_changeset(tree):
            if not change.path.endswith((".yml", ".yaml")):
                continue
            data = yaml.load(change.content, Loader=yaml.BaseLoader)
            self.assertIsInstance(data, dict, change.path)
            if change.path.startswith(".github/workflows/"):
                # PyYAML 1.1 would coerce `on` to True; BaseLoader keeps it
                self.assertIn("on", data, change.path)

    def test_pyproject_parses_as_toml(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib needs python 3.11+")
        text = pyproject_toml("o/some repo", ["flask==2.3.2", 'we"ird<1'])
        data = tomllib.loads(text)
        self.assertEqual(data["project"]["dependencies"],
                         ["flask==2.3.2", 'we"ird<1'])

    def test_package_json_roundtrip(self):
        tree = RepoTree(full_name="o/n", default_branch="main",
                        paths=("package.json", "tests/a.test.js"),
                        files={"package.json":
                               '{"name":"n","scripts":{"build":"x"}}'})
        patched = by_path(build_changeset(
            tree))["package.json"].content
        data = json.loads(patched)
        self.assertEqual(data["scripts"]["test"], "node --test")

    def test_commit_message_lists_files_and_reasons(self):
        message = commit_message(build_changeset(py_tree()))
        self.assertIn("chore(quality)", message)
        self.assertIn("pyproject.toml:", message)
        self.assertIn("requirements.txt", message)


def _import_yaml(self):
    try:
        import yaml
    except ImportError:
        self.skipTest("PyYAML not installed (validation-only dependency)")
    return yaml


unittest.TestCase.import_yaml = _import_yaml

if __name__ == "__main__":
    unittest.main()
