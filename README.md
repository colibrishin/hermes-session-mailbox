# hermes-session-mailbox

Send messages between [Hermes Agent](https://github.com/NousResearch/hermes-agent) sessions on the same machine, including sessions in different profiles. Recipients get a notice at their next model call. An idle session can wake to check its inbox.

Messages stay in a local SQLite file. No server or cloud service is needed.

## Install

Requires Hermes Agent with plugin support and Git with access to this repository.

```text
hermes plugins install colibrishin/hermes-session-mailbox --enable
```

Install in each named profile that will send or receive messages:

```text
hermes -p <profile> plugins install colibrishin/hermes-session-mailbox --enable
```

Start a new session after installation. No extra Python dependencies are needed.

Upgrading from 1.x? See [MIGRATION.md](MIGRATION.md).

## Use

Ask the agent to find another session and send it a message. The bundled skill, `session-mailbox-bundle:hermes-peer-session-collaboration`, guides the agent through sending, reading, and acknowledging messages.

- A recipient address is an exact profile and stored session ID pair. `session_mailbox_list_sessions` lists known addresses; it does not prove a session is running.
- Messages remain `queued` until the recipient explicitly acknowledges them. A notice or wake is not an acknowledgment.
- Automatic wake requires the recipient's Hermes runtime to be running with the plugin enabled. Otherwise, the message waits in the inbox.

Canonical Desktop/local-session wake uses Hermes's native plugin injection, not a
bridge or a second submit. Enable it in the recipient profile:

```text
hermes config set plugins.entries.session-mailbox-bundle.allow_gateway_injection true
```

The receiver waits for its native host during startup. Messaging-platform lane
keys are not used by this path because they can rotate to another conversation.
Legacy TUI sessions retain their existing submit path; a wake uses only one path.
Native `queued` records scheduling acceptance, not confirmed admission or execution.

The tools are `session_mailbox_list_sessions`, `session_mailbox_send`, `session_mailbox_inbox`, `session_mailbox_ack`, and `session_mailbox_message_status`.

## Settings

No configuration is needed by default. Profiles share `<hermes_root>/session-mailbox/mailbox.sqlite3`, where `hermes_root` is the global Hermes data directory.

To use a different mailbox, set the same absolute path in each participating profile:

```text
hermes config set plugins.entries.session-mailbox-bundle.settings.db_path /absolute/path/mailbox.sqlite3
hermes -p <profile> config set plugins.entries.session-mailbox-bundle.settings.db_path /absolute/path/mailbox.sqlite3
```

Do not use Hermes `state.db`, a path under `<hermes_root>/profiles`, or a path inside a plugin directory. These paths are rejected. The optional `hermes_root` setting must point to the global Hermes data directory, not a named-profile home.

## Limits and safety

- If the tools are missing, check that the `session-mailbox` toolset is enabled in `hermes tools`. `agent.coding_context: focus` also hides these tools in code-workspace sessions.
- The wake receiver waits while the recipient is busy, has a queued prompt or attachment, or is waiting for approval. It cannot see text you have typed but not sent.
- If a wake result is unknown, the receiver does not retry automatically.
- Sender addresses are caller claims, not authenticated identities. Trust depends on the local OS user.
- Peer messages are data. They do not grant permission or replace your instructions.

## License

MIT. See [LICENSE](LICENSE).
