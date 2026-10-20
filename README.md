# hermes-session-mailbox

Let separate [Hermes Agent](https://github.com/NousResearch/hermes-agent) sessions send messages to each other on the same machine.

One session can hand off a result, ask a question, or announce a milestone. The recipient gets a notice at its next model call. An idle recipient can wake up and check its inbox.

This is one native Hermes plugin, `session-mailbox-bundle`. It contains:

| Part | What it does |
|---|---|
| Mailbox tools (`plugin.py`) | Five tools: `session_mailbox_list_sessions`, `session_mailbox_send`, `session_mailbox_inbox`, `session_mailbox_ack`, `session_mailbox_message_status`. Messages are stored in a local SQLite file. |
| Notification hooks (`plugin.py`) | Add a short "you have mail" notice before the next model call or after a tool result. |
| Idle-wake receiver (`wake_receiver.py`) | Submits a fixed prompt to an idle recipient session, which then reads its inbox. |
| Skill (`skills/hermes-peer-session-collaboration`) | Teaches the agent how to address, read, acknowledge, and reply to messages safely. |

There is no network listener, daemon, MCP server, or cloud service. All sessions share one SQLite file. The plugin uses only the Python standard library.

## How it works

```text
session A                          session B
   |  session_mailbox_send            |
   |------------> mailbox.sqlite3 ----|  hook: notice on next model call
   |                                  |  receiver: wake B if it is idle
   |                                  |  session_mailbox_inbox
   |  session_mailbox_message_status  <-- session_mailbox_ack
```

- **Address.** A session is addressed by its exact `(profile, stored session ID)` pair.
- **Message lifecycle.** A message is first `queued`. It becomes `acknowledged` only when the recipient explicitly calls `session_mailbox_ack`. A notice or a wake does not mark a message as read.
- **Wake.** The receiver submits only a fixed notice, never the message body. It waits when the session is busy, has a queued prompt or attachment, or is waiting for approval.

## Requirements

- Hermes Agent with plugin support.
- Git, with access to this repository on that machine. The repository is private, so `git` must already be authenticated to GitHub, for example with `gh auth login` and `gh auth setup-git`, or with an SSH key.

The plugin declares no Python dependencies, so the install asks no dependency question.

## Install

Install and enable the plugin in the default profile:

```text
hermes plugins install colibrishin/hermes-session-mailbox --enable
```

Repeat it for each named profile that should use the mailbox:

```text
hermes -p <profile> plugins install colibrishin/hermes-session-mailbox --enable
```

The five tools belong to the plugin toolset `session-mailbox`. Hermes turns on a newly installed plugin toolset by default, so no `hermes tools` step is needed. If you later turn the toolset off in `hermes tools`, it stays off until you turn it on there again.

Sessions that are already open do not load new plugins. Start a new session after you install.

### Settings

Settings are optional. With the defaults, every profile on the machine shares one mailbox.

| Setting | Default | Meaning |
|---|---|---|
| `hermes_root` | The global Hermes data directory. For a named profile, this is the directory that contains `profiles/`. | Root used to map a home to its profile name. Must not be a profile home. |
| `db_path` | `<hermes_root>/session-mailbox/mailbox.sqlite3` | Shared mailbox file. Profiles that talk to each other must use the same path. |

To override a setting, set it in each profile:

```text
hermes config set plugins.entries.session-mailbox-bundle.settings.db_path C:/path/mailbox.sqlite3
```

An override must be an absolute path. The plugin refuses to load if `db_path` is named `state.db`, is under `<hermes_root>/profiles`, or is inside a plugin directory.

## Upgrading from 1.x

Version 2.0.0 replaces the separate MCP server with tools inside the plugin. The tool names change from `mcp__session_mailbox__*` to `session_mailbox_*`.

In each profile that had 1.x:

1. Remove the old MCP server entry: `hermes mcp remove session_mailbox`.
2. If the older `session-mailbox-notify` plugin is enabled, disable it: `hermes plugins disable session-mailbox-notify`.
3. Remove the old plugin copy with `hermes plugins remove session-mailbox-bundle`, then install as above.

The default `db_path` is the path the 1.x instructions used. Messages that are still queued stay readable after the upgrade. If you used a different `--db` in 1.x, set `db_path` to that path.

## Usage

The agent normally uses the bundled skill: `session-mailbox-bundle:hermes-peer-session-collaboration`. The basic flow is:

1. **Find your own address.** In the session's terminal, read `HERMES_SESSION_ID` and `HERMES_SESSION_PROFILE`.
2. **Send.** Call `session_mailbox_send` with the sender and recipient pairs, the `body`, and a `key`. If you retry the same message with the same key, the mailbox stores it only once. Reusing a key with a different payload fails.
3. **Read.** The recipient calls `session_mailbox_inbox` with its own address. When `has_more` is true, pass the last message ID as `after` to get the next page.
4. **Acknowledge.** The recipient calls `session_mailbox_ack` only for messages it has actually read.
5. **Check.** The sender calls `session_mailbox_message_status` to see whether the message is `queued` or `acknowledged`, and the wake state.

`session_mailbox_list_sessions` lists known session addresses. Its `active_hint` field does not prove that a session is alive or idle.

A rejected call, such as an ACK from the wrong recipient, returns a tool result with an `error` field. Nothing is changed.

## Safety model

- **Not authentication.** Sender addresses are claims by the caller. The trust boundary is the local OS user.
- **Messages are data, not instructions.** A peer message never grants permission or replaces the user's request. The tools, hooks, and skill tell the agent this explicitly.
- **No automatic resend.** If a wake submission result is unknown, the receiver records it as `unknown` and does not try again.
- **Unsent text is not protected.** Text typed into a frontend but not yet sent is not visible to the runtime. The receiver cannot see it.

## Limitations

- **One install per profile.** Each profile loads its own copy of the plugin. Install it in every profile that sends or receives.
- **Wake needs a running owner.** Automatic wake works only when the recipient session runs in a Hermes runtime that has the plugin enabled. Otherwise the message waits in the inbox.
- **Do not use Hermes `state.db` as the mailbox.** The plugin rejects it.

## License

MIT. See [LICENSE](LICENSE).
