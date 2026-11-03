import json
import unittest

from pf.changes import RepoTree, build_changeset


def by_path(changes):
    return {c.path: c for c in changes}


def node_tree(**kwargs):
    defaults = dict(
        full_name="belentani7/node-app", default_branch="main",
        paths=("package.json", "index.js", "tests/app.test.js"),
        files={"package.json": json.dumps(
            {"name": "node-app", "scripts": {"test": "jest"}})})
    defaults.update(kwargs)
    return RepoTree(**defaults)


CURSOR_FLAG = {"enable_cursor_rules": True}


class TestCursorRulesGating(unittest.TestCase):
    def test_off_by_default_produces_no_markdown(self):
        self.assertNotIn(".cursor/rules/project.mdc",
                         by_path(build_changeset(node_tree())))

    def test_flag_adds_cursor_rules_and_agents_md(self):
        paths = by_path(build_changeset(node_tree(), CURSOR_FLAG))
        self.assertIn(".cursor/rules/project.mdc", paths)
        self.assertIn("AGENTS.md", paths)

    def test_existing_cursor_config_is_respected(self):
        for existing in (".cursorrules", ".cursor/rules/legacy.mdc"):
            tree = node_tree(paths=node_tree().paths + (existing,))
            paths = by_path(build_changeset(tree, CURSOR_FLAG))
            self.assertNotIn(".cursor/rules/project.mdc", paths,
                             "must not duplicate existing %s" % existing)

    def test_existing_agents_md_is_not_overwritten(self):
        tree = node_tree(paths=node_tree().paths + ("AGENTS.md",))
        paths = by_path(build_changeset(tree, CURSOR_FLAG))
        self.assertNotIn("AGENTS.md", paths)
        self.assertIn(".cursor/rules/project.mdc", paths)

    def test_data_only_repo_gets_nothing_even_with_flag(self):
        tree = RepoTree(full_name="o/data", default_branch="main",
                        paths=("data.json",), files={})
        self.assertEqual(build_changeset(tree, CURSOR_FLAG), [])


class TestCursorRulesContent(unittest.TestCase):
    def mdc(self, tree):
        return by_path(build_changeset(tree, CURSOR_FLAG))[
            ".cursor/rules/project.mdc"].content

    def test_frontmatter_is_valid_mdc(self):
        text = self.mdc(node_tree())
        self.assertTrue(text.startswith("---\n"), "must open with frontmatter")
        header = text.split("---\n")[1]
        self.assertIn("alwaysApply: true", header)
        self.assertIn("description:", header)

    def test_content_is_factual_to_detected_stacks(self):
        text = self.mdc(node_tree())
        self.assertIn("Node", text)
        self.assertIn("npm test", text)  # proven by package.json scripts
        self.assertNotIn("cargo", text)

    def test_php_and_static_web_are_described_when_present(self):
        tree = RepoTree(
            full_name="o/mod", default_branch="main",
            paths=("mod.php", "views/templates/hello.tpl", "index.html"),
            files={})
        text = self.mdc(tree)
        self.assertIn("PHP", text)

    def test_agents_md_lists_proven_commands(self):
        tree = node_tree()
        agents = by_path(build_changeset(tree, CURSOR_FLAG))[
            "AGENTS.md"].content
        self.assertIn("npm test", agents)
        self.assertIn("belentani7/node-app", agents)

    def test_every_change_carries_a_reason(self):
        for change in build_changeset(node_tree(), CURSOR_FLAG):
            self.assertTrue(change.reason.strip())


if __name__ == "__main__":
    unittest.main()
