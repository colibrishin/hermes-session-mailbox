"""Single-profile native installer. Import-safe; preview unless --apply.
Run with Hermes' Python (hermes_yaml), supply a separate MCP SDK 2 interpreter.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

PLUGIN = "session-mailbox-bundle"
SERVER = "session_mailbox"
FILES = ("__init__.py", "plugin.py", "plugin.yaml", "server.py", "mailbox_store.py",
         "wake_store.py", "wake_receiver.py", "setup_bundle.py", "requirements-server.txt", "README.md",
         "skills/hermes-peer-session-collaboration/SKILL.md")


def absolute(value):
    p = Path(value)
    if not p.is_absolute():
        raise ValueError(f"absolute path required: {value}")
    return p.resolve()


def read_config(home):
    import hermes_yaml
    result = hermes_yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8-sig")) or {}
    if not isinstance(result, dict):
        raise ValueError("config must be a mapping")
    return result


def unrelated(config):
    config = deepcopy(config)
    # Native config writers stamp their schema version on old/minimal files.
    # This is migration metadata, not an unrelated operator setting.
    config.pop("_config_version", None)
    servers = config.get("mcp_servers", {})
    servers.pop(SERVER, None)
    if not servers:
        config.pop("mcp_servers", None)
    plugins = config.get("plugins", {})
    entries = plugins.get("entries", {})
    entries.pop(PLUGIN, None)
    if not entries:
        plugins.pop("entries", None)
    for field in ("enabled", "disabled"):
        if field in plugins:
            plugins[field] = [p for p in plugins[field] if p != PLUGIN]
            if not plugins[field]:
                plugins.pop(field)
    if not plugins:
        config.pop("plugins", None)
    return config


def plan(args):
    root, home, db, interpreter = map(absolute, (args.hermes_root, args.profile_home, args.db, args.server_python))
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.profile):
        raise ValueError("invalid profile identifier")
    expected = root if args.profile == "default" else root / "profiles" / args.profile
    if home != expected or root.parent.name == "profiles":
        raise ValueError("profile/home/global-root mismatch")
    if not (home / "config.yaml").is_file() or not interpreter.is_file():
        raise ValueError("existing profile config and server interpreter required")
    if db.name.lower() == "state.db" or db.is_relative_to(root / "profiles"):
        raise ValueError("mailbox must not be state.db or profile-local")
    source = Path(__file__).resolve().parent
    if db.is_relative_to(source) or db.is_relative_to(home / "plugins"):
        raise ValueError("mailbox data must be outside plugin source/install trees")
    destination = home / "plugins" / PLUGIN
    settings = {"hermes_root": str(root), "db_path": str(db)}
    server_args = ["-B", str(destination / "server.py"), "--db", str(db), "--hermes-root", str(root)]
    before = read_config(home)
    plugins = before.get("plugins", {})
    if "session-mailbox-notify" in plugins.get("enabled", []):
        raise ValueError("legacy notification hook is enabled: decide migration and disable it explicitly first")
    entry = before.get("mcp_servers", {}).get(SERVER)
    if entry is not None and (entry.get("command") != str(interpreter) or entry.get("args") != server_args
                              or entry.get("enabled") is not True):
        raise ValueError("conflicting session_mailbox config; no overwrite: migrate explicitly after backup")
    old = plugins.get("entries", {}).get(PLUGIN)
    if old is not None and old.get("settings") != settings:
        raise ValueError("conflicting bundle settings")
    if destination.exists():
        for name in FILES:
            if not (destination / name).is_file() or (destination / name).read_bytes() != (source / name).read_bytes():
                raise ValueError(f"installed bundle drift: {name}; no overwrite")
    command = json.loads(args.hermes_command)
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise ValueError("--hermes-command must be a JSON argv array")
    return dict(root=root, home=home, db=db, interpreter=interpreter, source=source,
                destination=destination, settings=settings, server_args=server_args,
                before=before, command=command, existing_server=entry is not None)


def run(argv, env, cwd=None, input=None):
    result = subprocess.run(argv, env=env, cwd=cwd, input=input, text=True,
                            encoding="utf-8", errors="replace", capture_output=True, timeout=180)
    print(json.dumps({"command": argv, "exit": result.returncode,
                      "stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=True), flush=True)
    if result.returncode:
        raise RuntimeError("native command failed; partial installation may remain, inspect before retry")
    return result


def install(args):
    p = plan(args)
    print(json.dumps({"apply": args.apply, "profile": args.profile, "home": str(p["home"]),
                      "plugin": PLUGIN, "destination": str(p["destination"]),
                      "server": SERVER, "command": str(p["interpreter"]), "args": p["server_args"],
                      "settings": p["settings"], "hooks_activation": "separate explicit native enable"}, indent=2))
    if not args.apply:
        return
    env = dict(os.environ, HERMES_HOME=str(p["home"]), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    # Prevent an inherited active-profile selector from redirecting native CLI.
    env.pop("HERMES_PROFILE", None)
    env.pop("HERMES_PROFILE_NAME", None)
    run([str(p["interpreter"]), "-B", "-c",
         "from importlib.metadata import version; assert version('mcp') == '2.0.0'; from mcp.server import MCPServer"], env)
    if not p["destination"].exists():
        scratch = absolute(args.scratch)
        scratch.mkdir(parents=True, exist_ok=True)
        # Native install accepts Git sources, not arbitrary directories. Stage
        # only reviewed code: no .venv, DB, receipts, secrets or old package.
        with tempfile.TemporaryDirectory(prefix="mailbox-install-", dir=scratch) as tmp:
            stage = Path(tmp)
            for name in FILES:
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p["source"] / name, target)
            run(["git", "init", "--quiet", str(stage)], env)
            run(["git", "-C", str(stage), "config", "core.autocrlf", "false"], env)
            run(["git", "-C", str(stage), "add", "--", *FILES], env)
            run(["git", "-C", str(stage), "-c", "user.name=Mailbox Bundle", "-c",
                 "user.email=mailbox@localhost", "commit", "--quiet", "-m", "Local bundle snapshot"], env)
            # Bundled Git for Windows accepts file://C:/..., not /C:/...
            local_url = "file://" + stage.as_posix() if os.name == "nt" else stage.as_uri()
            install_env = dict(env, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.autocrlf", GIT_CONFIG_VALUE_0="false")
            run(p["command"] + ["plugins", "install", local_url, "--no-enable", "--no-deps"], install_env)
        # Read back exact copied targets, not just command exit status.
        for name in FILES:
            target = p["destination"] / name
            if not target.is_file() or target.read_bytes() != (p["source"] / name).read_bytes():
                raise RuntimeError(f"native plugin install readback mismatch: {name}")
    current = read_config(p["home"])
    if unrelated(current) != unrelated(p["before"]):
        raise RuntimeError("unrelated configuration changed; stop without restoring over concurrent edits")
    run(p["command"] + ["config", "set", f"plugins.entries.{PLUGIN}.settings", json.dumps(p["settings"])], env)
    if not p["existing_server"]:
        run(p["command"] + ["mcp", "add", SERVER, "--command", str(p["interpreter"]),
                            "--connect-timeout", "20", "--args", *p["server_args"]], env, input="y\n")
    final = read_config(p["home"])
    entry = final.get("mcp_servers", {}).get(SERVER, {})
    if not (entry.get("enabled") is True and entry.get("command") == str(p["interpreter"])
            and entry.get("args") == p["server_args"]):
        raise RuntimeError("MCP registration did not read back enabled; inspect partial state")
    if final["plugins"]["entries"][PLUGIN]["settings"] != p["settings"]:
        raise RuntimeError("plugin settings readback mismatch")
    if unrelated(final) != unrelated(p["before"]):
        raise RuntimeError("unrelated configuration changed; no destructive rollback attempted")
    print(json.dumps({"verified": True, "mcp_registered": True, "plugin_files_verified": True,
                      "hooks_enabled_by_installer": False, "running_sessions_reloaded": False,
                      "unrelated_config_sha256": hashlib.sha256(json.dumps(unrelated(final), sort_keys=True).encode()).hexdigest()}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("profile", "profile-home", "hermes-root", "db", "server-python"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--hermes-command", default='["hermes"]', help="JSON argv prefix, not a shell command")
    parser.add_argument("--scratch", default=tempfile.gettempdir())
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        install(args)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"STOP: {exc}\n")


if __name__ == "__main__":
    main()
