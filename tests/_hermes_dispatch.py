"""Helper process: load plugins for HERMES_HOME natively, dispatch JSON tool calls from stdin."""
import json
import sys

from hermes_cli.plugins import discover_plugins, get_plugin_manager
from tools.registry import registry

PLUGIN_KEY = "session-mailbox-bundle"

discover_plugins()
loaded = get_plugin_manager()._plugins.get(PLUGIN_KEY)
state = {
    "loaded": loaded is not None,
    "enabled": getattr(loaded, "enabled", None),
    "error": str(getattr(loaded, "error", None) or "") or None,
    "results": [],
}
for name, args in json.load(sys.stdin):
    raw = registry.dispatch(name, args)
    state["results"].append(json.loads(raw) if isinstance(raw, str) else raw)
print("@@RESULT@@" + json.dumps(state, ensure_ascii=True), flush=True)
