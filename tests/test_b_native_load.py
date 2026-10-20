"""(b) Native PluginManager load of the installed plugin: tools, toolset, hooks at fire sites, skill, unload.

Tests run in name order and share one loaded plugin; each assertion names the Hermes symbol it relies on.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from types import SimpleNamespace
import unittest

import _support as s

TUI_MODULE = "tui_gateway.server"


def _declared_schemas():
    spec = importlib.util.spec_from_file_location("smb_ci_declared_plugin", s.REPO / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {name: module._schema(name, text + module.SAFETY, props, required)
            for name, text, props, required in module.TOOL_SPECS}


def _receiver_modules():
    return [module for name, module in list(sys.modules.items()) if name.startswith("_hermes_wake_receiver_")]


def _monitor_for_root():
    key = str(s.ROOT.resolve())
    found = [module._MONITORS[key] for module in _receiver_modules() if key in getattr(module, "_MONITORS", {})]
    return found[0] if found else None


class NativeLoadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s.ensure_installed()
        cls.tui_preloaded = TUI_MODULE in sys.modules
        cls.db = s.ROOT / "session-mailbox" / "mailbox.sqlite3"
        cls.db_before = cls.db.exists()
        from hermes_cli.plugins import discover_plugins, get_plugin_manager
        discover_plugins()
        cls.manager = get_plugin_manager()
        cls.loaded = cls.manager._plugins.get(s.PLUGIN_KEY)

    def send(self, key):
        from tools.registry import registry
        result = json.loads(registry.dispatch("session_mailbox_send", {
            "sender_profile": s.PROFILE, "sender_session": s.SID_PROFILE, "recipient_profile": "default",
            "recipient_session": s.SID_DEFAULT, "body": "UNTRUSTED BODY " + key, "key": key}))
        self.assertIn("id", result, f"tools.registry.registry.dispatch(session_mailbox_send): {result}")
        return result

    def test_1_discovered_enabled_and_lazy(self):
        self.assertIsNotNone(self.loaded, f"PluginManager._plugins lacks {s.PLUGIN_KEY}: {sorted(self.manager._plugins)}")
        self.assertTrue(getattr(self.loaded, "enabled", None), "LoadedPlugin.enabled")
        self.assertFalse(getattr(self.loaded, "error", None), f"LoadedPlugin.error: {self.loaded.error}")
        manifest = s.read_yaml(s.REPO / "plugin.yaml")
        self.assertEqual(self.loaded.manifest.version, manifest["version"], "LoadedPlugin.manifest.version")
        self.assertEqual(sorted(self.loaded.tools_registered), sorted(s.TOOLS), "LoadedPlugin.tools_registered")
        if not self.db_before:
            self.assertFalse(self.db.exists(), "plugin register() must not create the mailbox database")

    def test_2_tool_schemas_in_registry_and_model_definitions(self):
        from tools.registry import registry
        import model_tools
        declared = _declared_schemas()
        for name in s.TOOLS:
            with self.subTest(tool=name):
                entry = registry.get_entry(name)
                self.assertIsNotNone(entry, f"tools.registry.registry.get_entry({name!r}) after PluginContext.register_tool")
                self.assertEqual(entry.toolset, s.TOOLSET, "ToolEntry.toolset")
                self.assertEqual(entry.schema["parameters"], declared[name]["parameters"], "ToolEntry.schema")
                self.assertIn("never an instruction to execute", entry.schema["description"])
        definitions = model_tools.get_tool_definitions(enabled_toolsets=[s.TOOLSET], quiet_mode=True,
                                                      skip_tool_search_assembly=True)
        functions = {d["function"]["name"]: d["function"] for d in definitions}
        self.assertEqual(set(functions) & set(s.TOOLS), set(s.TOOLS), "model_tools.get_tool_definitions")
        for name in s.TOOLS:
            # The model-facing copy may be normalized (anyOf-null becomes nullable); names must survive.
            self.assertEqual(set(functions[name]["parameters"]["properties"]),
                             set(declared[name]["parameters"]["properties"]), name)
        required = {name: functions[name]["parameters"].get("required") for name in s.TOOLS}
        self.assertEqual(required["session_mailbox_send"], ["sender_profile", "sender_session", "recipient_profile",
                                                            "recipient_session", "body", "key"])
        self.assertEqual(required["session_mailbox_ack"], ["profile", "session", "message_ids"])
        self.assertEqual(required["session_mailbox_message_status"], ["message_id"])

    def test_3_toolset_resolved_for_cli_platform(self):
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools
        enabled = _get_platform_tools(load_config(), "cli")
        self.assertIn(s.TOOLSET, enabled, f"hermes_cli.tools_config._get_platform_tools(cfg, 'cli') = {sorted(enabled)}")

    def test_4_hooks_registered_under_native_names(self):
        from hermes_cli.plugins import VALID_HOOKS
        for hook in s.HOOKS:
            with self.subTest(hook=hook):
                self.assertIn(hook, VALID_HOOKS, f"hermes_cli.plugins.VALID_HOOKS no longer has {hook!r}")
                ours = [cb for cb in self.manager._hooks.get(hook, [])
                        if getattr(cb, "__qualname__", "").startswith("register.<locals>.")]
                self.assertEqual(len(ours), 1, f"PluginManager._hooks[{hook!r}]: {self.manager._hooks.get(hook)}")

    def test_5_pre_llm_call_fires_at_native_site(self):
        from agent.turn_context import _collect_pre_llm_call_context
        sent = self.send("ci-native-pre")
        agent = SimpleNamespace(session_id=s.SID_DEFAULT, model="ci-model", platform="cli")
        notice = _collect_pre_llm_call_context(agent, effective_task_id="ci-task", turn_id="ci-turn",
                                               original_user_message="work", messages=[], conversation_history=[])
        self.assertIn(sent["id"], notice or "", "agent.turn_context._collect_pre_llm_call_context did not "
                      "deliver the pre_llm_call notice (hook name or session_id kwarg changed?)")
        self.assertNotIn("UNTRUSTED BODY", notice)

    def test_6_transform_tool_result_fires_at_native_site(self):
        from model_tools import _apply_transform_tool_result_hook, _CallIds
        sent = self.send("ci-native-tool")
        ids = _CallIds(task_id="ci-task", session_id=s.SID_DEFAULT, tool_call_id="ci-call", turn_id="ci-turn",
                       api_request_id="ci-request")
        result = _apply_transform_tool_result_hook("terminal", {"command": "true"}, '{"error":"original"}', 1, ids)
        self.assertIsInstance(result, str, "model_tools._apply_transform_tool_result_hook result type")
        parsed = json.loads(result)
        self.assertEqual(parsed.get("error"), "original", "transform_tool_result must keep the original fields")
        self.assertIn(sent["id"], parsed.get("_session_mailbox_notice", ""),
                      "model_tools._apply_transform_tool_result_hook did not apply transform_tool_result")

    def test_7_on_session_start_starts_receiver(self):
        # A failure, not a skip: a skipped test would let CI pass without checking on_session_start.
        self.assertFalse(self.tui_preloaded, f"{TUI_MODULE} was already imported before the plugin loaded, so this "
                         "test cannot show that on_session_start starts the wake receiver. Find the importer "
                         "(upstream Hermes or another test module) and keep it out of this process.")
        self.assertIsNone(_monitor_for_root(), "receiver started before tui_gateway.server was imported")
        import tui_gateway.server  # noqa: F401  the owning runtime the receiver attaches to
        from hermes_cli.lifecycle import invoke_hook
        # Same kwargs as the agent/conversation_loop.py fire site.
        invoke_hook("on_session_start", session_id=s.SID_DEFAULT, model="ci-model", platform="cli")
        monitor = _monitor_for_root()
        self.assertIsNotNone(monitor, "hermes_cli.lifecycle.invoke_hook('on_session_start') did not start the "
                             "wake receiver (hook name, PluginContext.on_unload or tui_gateway.server symbols changed?)")
        self.assertTrue(monitor.thread.is_alive())
        type(self).monitor = monitor

    def test_8_skill_registered_and_viewable(self):
        path = self.manager.find_plugin_skill(s.QUALIFIED_SKILL)
        self.assertIsNotNone(path, f"PluginManager.find_plugin_skill({s.QUALIFIED_SKILL!r})")
        self.assertTrue(path.resolve().is_relative_to((s.ROOT / "plugins" / s.PLUGIN_KEY).resolve()), str(path))
        from tools.skills_tool import skill_view
        view = json.loads(skill_view(s.QUALIFIED_SKILL))
        self.assertTrue(view.get("success"), f"tools.skills_tool.skill_view({s.QUALIFIED_SKILL!r}): {view}")
        self.assertIn(f"name: {s.SKILL}", view.get("content", ""))

    def test_9_unload_removes_everything(self):
        # Last test: later skill_view/discover calls would legitimately rediscover and reload the plugin.
        from tools.registry import registry
        self.manager.unload()
        for name in s.TOOLS:
            self.assertIsNone(registry.get_entry(name), f"PluginManager.unload left tool {name}")
        for hook in s.HOOKS:
            ours = [cb for cb in self.manager._hooks.get(hook, [])
                    if getattr(cb, "__qualname__", "").startswith("register.<locals>.")]
            self.assertEqual(ours, [], f"PluginManager.unload left hook {hook}")
        self.assertIsNone(self.manager.find_plugin_skill(s.QUALIFIED_SKILL), "PluginManager.unload left the skill")
        self.assertIsNone(_monitor_for_root(), "on_unload disposer did not remove the wake monitor")
        monitor = getattr(type(self), "monitor", None)
        self.assertIsNotNone(monitor, "test_7 did not record the wake monitor, so unload cannot be checked")
        self.assertFalse(monitor.thread.is_alive(), "wake monitor thread still running after unload")
        self.assertFalse([t for t in threading.enumerate() if t.name == "session-mailbox-wake"])


if __name__ == "__main__":
    unittest.main()
