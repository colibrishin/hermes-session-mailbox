"""(a) Real `hermes plugins install file://<checkout> --enable` into a default and a named-profile home."""
from __future__ import annotations

import unittest

import _support as s


class CliInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.steps = s.ensure_installed()

    def test_install_exits_zero_without_prompting(self):
        # stdin is closed: a confirmation or dependency prompt would fail or hang, not pass.
        for name in ("install-default", "install-profile"):
            out = self.steps[name].stdout + self.steps[name].stderr
            self.assertIn(f"Plugin {s.PLUGIN_KEY} enabled", out, f"{name}: no enable confirmation\n{out}")

    def test_installed_tree_and_enabled_in_each_home(self):
        for home in (s.ROOT, s.PROFILE_HOME):
            with self.subTest(home=home.name):
                installed = home / "plugins" / s.PLUGIN_KEY
                for rel in ("plugin.yaml", "plugin.py", "__init__.py", "mailbox_store.py",
                            "wake_receiver.py", f"skills/{s.SKILL}/SKILL.md"):
                    self.assertTrue((installed / rel).is_file(), f"missing installed file {installed / rel}")
                plugins = s.read_yaml(home / "config.yaml").get("plugins") or {}
                self.assertIn(s.PLUGIN_KEY, plugins.get("enabled") or [], f"config.yaml plugins: {plugins}")
                self.assertNotIn(s.PLUGIN_KEY, plugins.get("disabled") or [])

    def test_no_python_dependency_declaration(self):
        # Hermes asks a dependency question only for a declaring plugin; this plugin must never be one.
        from pm.plugin_declarations import read_python_declaration
        declaration = read_python_declaration(s.ROOT / "plugins" / s.PLUGIN_KEY)
        self.assertIsNone(declaration.pyproject, "pm.plugin_declarations.read_python_declaration: pyproject")
        self.assertEqual(tuple(declaration.requirements), (), "read_python_declaration: requirements")
        self.assertEqual(tuple(declaration.install_requirements), ())
        self.assertFalse(declaration.is_member, "PythonDeclaration.is_member: plugin joined the dependency workspace")
        self.assertEqual(declaration.manifest.get("name"), s.PLUGIN_KEY)


if __name__ == "__main__":
    unittest.main()
