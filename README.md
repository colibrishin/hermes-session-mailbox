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
- Session discovery defaults to compact `profile`, `session`, and `title` rows, newest activity first, with subagents excluded. Use `query` to search titles/addresses within the bounded recent candidates, `include_subagents=true` to include workers, or `detailed=true` for metadata and unverified activity hints. `include_recent=false` restricts discovery to registry entries.
- Messages remain `queued` until the recipient explicitly acknowledges them. A notice or wake is not an acknowledgment.
- Automatic wake requires the recipient's Hermes runtime to be running with the plugin enabled. Otherwise, the message waits in the inbox.

For example, ask the agent to call `session_mailbox_list_sessions(query="<topic>")`
to find an address. Use `session_search` only when you also need transcript/work context.

The tools are `session_mailbox_list_sessions`, `session_mailbox_send`, `session_mailbox_inbox`, `session_mailbox_ack`, and `session_mailbox_message_status`.

## Optional idle wake

Mailbox storage, inbox reads and explicit ACKs need no configuration. Canonical
Desktop/local-session idle wake is a separate opt-in in the recipient profile.
It uses Hermes's native plugin injection, not a
bridge or a second submit. Enable it in the recipient profile:

```text
hermes config set plugins.entries.session-mailbox-bundle.allow_gateway_injection true
```

For a named recipient profile, add `-p <profile>` after `hermes` in this command.
The receiver waits for its native host during startup. Messaging-platform lane
keys are not used by this path because they can rotate to another conversation.
Legacy TUI sessions retain their existing submit path; a wake uses only one path.
Native `queued` records scheduling acceptance, not confirmed admission or execution.

## Settings

Default mailbox operations need no configuration; Desktop idle wake requires the opt-in above. Profiles share `<hermes_root>/session-mailbox/mailbox.sqlite3`, where `hermes_root` is the global Hermes data directory.

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

## Responses

- `send` returns the message ID, routing, status and wake state, never the body. Read bodies with `inbox`.
- `inbox` pages hold whole messages and fit 32,768 UTF-8 bytes of JSON, so a page can be smaller than `limit`. Follow `next_cursor` while `has_more` is true. Bodies are never cut.
- `ack` returns each message's `id`, `status` and `acknowledged_at`. A repeated ACK keeps the first timestamp.
- Discovery warnings are grouped, with a `count` per group and the total in `warning_count`. Detailed rows carry one `liveness` hint, which never proves that a session is running or idle.
- The full send request, including routing, must fit 8,192 UTF-8 bytes (bytes, not characters).

## Running the tests locally

Standalone tests use synthetic temporary stores only. Set `TMPDIR` to your
authorized scratch directory, then from this checkout run:

```text
python -B tests/reload_regression.py
python -B tests/session_listing.py
python -B tests/public_responses.py
python -B tests/native_gateway.py
```

They need an installed Hermes interface; `native_gateway.py` requires the canonical
Desktop interface (CI pins `4d04303818b61bf3aa09278af48356315515965b`).
For the legacy installer/profile/real-submit suite, run `python -B tests/run.py`
with the compatible CI legacy pin (`d923a5eb487a17c0a68801bba9ef0f56c81040a8`).
Use a dedicated Hermes source checkout and Python 3.14 venv **outside all real
Hermes data roots**, with its own installed interface; keep `TMPDIR`/`TEMP`
and `SMB_CI_BASE` (if set) outside them too. Follow the editable installation
in `.github/actions/hermes-interface/action.yml`. The runner redirects fixture
homes and removes them only after child processes exit. Its refusal guard is
intentional: do not disable it or borrow a live runtime/package-manager store.
Native and legacy tests exercise different host APIs; one does not replace the other.

## License

MIT. See [LICENSE](LICENSE).
