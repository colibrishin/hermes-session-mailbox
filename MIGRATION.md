# Migrating from 1.x to 2.0

Version 2.0.0 removes the separate MCP server. The plugin now provides the mailbox tools itself.

## What changes

| | 1.x | 2.0 |
|---|---|---|
| Tools | MCP server `session_mailbox` | Plugin toolset `session-mailbox` |
| Tool names | `mcp__session_mailbox__send`, … | `session_mailbox_send`, … |
| Install | `setup_bundle.py` + MCP SDK environment | `hermes plugins install` |
| Mailbox database | Unchanged format | Same file, same format |

Agents and prompts that call the old `mcp__session_mailbox__*` names must use the new `session_mailbox_*` names.

## Steps

Do this in each profile that had 1.x. Add `-p <profile>` for a named profile.

1. Remove the old MCP server entry. The command asks for confirmation.

   ```text
   hermes mcp remove session_mailbox
   ```

2. If the older `session-mailbox-notify` plugin is enabled, disable it.

   ```text
   hermes plugins disable session-mailbox-notify
   ```

3. Remove the old plugin copy, then install 2.0.

   ```text
   hermes plugins remove session-mailbox-bundle
   hermes plugins install colibrishin/hermes-session-mailbox --enable
   ```

4. Start a new session. Sessions that are already open keep the old hooks and tools.

## Mailbox database

Messages that are still queued stay readable after the upgrade. Both versions use the same database format.

- **Default path.** 2.0 uses `<hermes-root>/session-mailbox/mailbox.sqlite3`. This is the path the 1.x instructions used.
- **Existing setting.** The 1.x installer stored `db_path` and `hermes_root` in `plugins.entries.session-mailbox-bundle.settings`. 2.0 keeps using these values if they are still in the config. Check them with:

  ```text
  hermes config get plugins.entries.session-mailbox-bundle.settings
  ```

- **Custom path.** If you used a different `--db` in 1.x and the setting is gone, set it again:

  ```text
  hermes config set plugins.entries.session-mailbox-bundle.settings.db_path <absolute-path>
  ```

Every profile that should exchange messages must use the same database path.

## Mixed versions

A 1.x profile and a 2.0 profile can exchange messages while they share the same database. Each side uses its own tool names. Upgrade all profiles when you can, so the skill and notices refer to one set of names.
