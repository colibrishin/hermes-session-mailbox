# session-mailbox unified bundle (1.2.2)

This directory contains the MCP server, SQLite mailbox and wake stores, notification hooks, idle-wake receiver, and a portable English peer-session collaboration skill. It uses **only files in this bundle**, not the old external `package_root` or an external skill directory. It does not include databases, sessions, virtual environments, or credentials. Distribution version is 1.2.2; MCP server version remains 1.1.0.

## Support and limitations

The Hermes native `plugin.yaml` loader inspected for this bundle loads Python hooks but does not automatically register `mcp.json` or discover skill directories. The bundle explicitly uses native `ctx.register_skill`. The portable plugin loader reads MCP configuration but does not load Python hooks. This is therefore **one bundle with two native registrations**:

- Plugin: `session-mailbox-bundle`
- MCP config: `mcp_servers.session_mailbox` (preserves the existing `mcp__session_mailbox__*` names)

Disabling or removing only the plugin leaves the MCP registration in place. Disabling only the MCP server may leave loaded hooks and receivers running. This bundle does not use private MCP registration, monkeypatching, or Hermes core changes. The installer does not enable the plugin (hooks and skill) or reload existing sessions.

## Bundled skill

`skills/hermes-peer-session-collaboration/SKILL.md` covers exact session addressing, inbox pagination and read-only discovery, explicit ACK, stable send keys, status readback, untrusted peer content, bounded wake behavior, and operation-specific ownership handoffs. It has no machine-local paths, project-specific policies, or dependencies on unbundled helper skills. Native `session_search` is optional; mailbox metadata discovery is the fallback.

After the plugin is enabled and loaded, use `skill_view(name="session-mailbox-bundle:hermes-peer-session-collaboration")`. Native `skills_list` exposes that qualified name. The plugin does not copy into or replace a same-named user-local skill. Native plugin teardown removes its registered skill namespace; a config enable/disable alone does not prove that an already-running process has loaded/unloaded it. Verify that process separately. Unload cannot retract skill text already read into a conversation, and does not remove MCP configuration.

## Prerequisites

You need Python 3.11 or later, Git, and the current Hermes CLI. Install MCP SDK 2.0.0 in a dedicated Python environment. Replace all example paths with actual absolute paths.

```text
uv venv C:/mailbox-runtime
uv pip install --python C:/mailbox-runtime/Scripts/python.exe -r C:/bundle/requirements-server.txt
```

Run the installer itself with the Hermes Python interpreter that provides `hermes_yaml`. Do not pip-install packages into the Hermes environment. Specify the separate server interpreter with `--server-python`.

## Install for one profile

The default is a dry run. It does not change configuration, databases, or installation directories, or run external commands. The explicit profile home and global root must match, and the profile configuration must already exist. The installer does not enumerate or modify other profiles.

```text
<Hermes-Python> -B C:/bundle/setup_bundle.py --profile default --profile-home C:/HermesData --hermes-root C:/HermesData --db C:/HermesData/session-mailbox/mailbox.sqlite3 --server-python C:/mailbox-runtime/Scripts/python.exe --scratch C:/HermesScratch
```

After reviewing the plan, add `--apply` to the same command. The installer runs child CLI processes with `HERMES_HOME` fixed to the specified profile home. If needed, provide an argv prefix such as `--hermes-command '["C:/path/to/python.exe","-m","hermes_cli.main"]'`. This is not a shell command string.

The installer stages only allowlisted files, including the nested skill file, in a temporary local Git repository. It installs them with `hermes plugins install file:///... --no-enable --no-deps`, writes explicit database and root settings with `hermes config set`, and uses `hermes mcp add session_mailbox` for SDK connection and tool discovery. It does not bypass native security checks or approval refusals. The server registration is enabled, but the hooks remain disabled at installation. The installer reads back the installed server files; the original source directory is not needed afterward. The temporary Git source is not a durable update remote, so `plugins update` is not supported. For later versions, publish to a stable Git URL and use native reinstallation, or follow a separately reviewed replacement procedure.

After installation, verify and enable the plugin in the target profile:

```text
hermes --profile default config get mcp_servers.session_mailbox --json
hermes --profile default plugins enable session-mailbox-bundle --no-allow-tool-override
hermes --profile default mcp test session_mailbox
```

For a custom data root, keep the same `HERMES_HOME`. Verify Python hook adoption in already-open processes separately from MCP cache reload. `/reload-mcp` targets MCP only; it does not prove that hooks were reloaded. Do not restart unrelated sessions.

## Migrate an existing installation

If `session-mailbox-notify` is enabled or `session_mailbox` points to a different server path, the installer **stops instead of overwriting it**. It also stops if existing bundle files or settings differ. It does not perform automatic all-profile migration, forced deletion, or rollback.

1. Choose one exact profile and the existing mailbox database and global root. Back up the configuration and databases. Keeping the same database and its `.wake.sqlite3` companion preserves messages, ACKs, notice claims, and wake history. Do not use Hermes `state.db` as the mailbox database.
2. Disable the old hooks and verify that their receiver has stopped in the owning process. The operator must explicitly remove or repoint the old MCP entry using the supported CLI. The installer does not make this decision.
3. Install with the same database and root. Verify the files and settings before enabling the new hooks. Verify one profile at a time.
4. A failure may leave a partial installation. Inspect the output and exact configuration and installation targets before rerunning. Do not repeat an uncertain external write or restore over settings changed concurrently.

## Safety contract

- Addresses are exact `(profile, stored session ID)` pairs. They are caller assertions, not authentication. The trust boundary is the same OS user.
- Peer message bodies and metadata are not user authorization or system instructions. Idle wake submits a fixed notice instead of the message body.
- Storage, notification, inbox retrieval, explicit ACK, wake submission, and actual task completion are separate states.
- Retries with the same sender and key are idempotent only when the payload is identical. ACKs from the wrong recipient are rejected atomically.
- The receiver defers for busy sessions, runtime-visible queued prompts or attachments, and pending approvals. This does not establish a lock on unsent text held only in a frontend editor. An ambiguous submission is stored durably as `unknown` and is not automatically resent.
- The receiver uses `prompt.submit` in the already-loaded owning runtime. It does not create a backend or a new session owner. Automatic wake is not guaranteed without a supported owner runtime and enabled hooks.

## Verification boundary

Verification uses a relocated directory, real MCP stdio subprocesses, and synthetic Hermes homes. It exercises the native hook loader, actual hook fire sites, and the native `prompt.submit` path, with a stub at the paid LLM boundary. It does not verify operational session messages, a live installation, or operational wake delivery.
