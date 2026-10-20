# hermes-session-mailbox

Let separate [Hermes Agent](https://github.com/NousResearch/hermes-agent) sessions send messages to each other on the same machine.

One session can hand off a result, ask a question, or announce a milestone. The recipient gets a notice at its next model call. An idle recipient can wake up and check its inbox.

The bundle contains:

| Part | What it does |
|---|---|
| MCP server (`server.py`) | Five tools: `list_sessions`, `send`, `inbox`, `ack`, `message_status`. Messages are stored in a local SQLite file. |
| Notification hooks (`plugin.py`) | Add a short "you have mail" notice before the next model call or after a tool result. |
| Idle-wake receiver (`wake_receiver.py`) | Submits a fixed prompt to an idle recipient session, which then reads its inbox. |
| Skill (`skills/hermes-peer-session-collaboration`) | Teaches the agent how to address, read, acknowledge, and reply to messages safely. |
| Installer (`setup_bundle.py`) | Registers the plugin and the MCP server in one Hermes profile. |

There is no network listener, daemon, or cloud service. All sessions share one SQLite file.

## How it works

```text
session A                          session B
   |  mcp__session_mailbox__send      |
   |------------> mailbox.sqlite3 ----|  hook: notice on next model call
   |                                  |  receiver: wake B if it is idle
   |                                  |  mcp__session_mailbox__inbox
   |  mcp__session_mailbox__message_status  <-- mcp__session_mailbox__ack
```

- **Address.** A session is addressed by its exact `(profile, stored session ID)` pair.
- **Message lifecycle.** A message is first `queued`. It becomes `acknowledged` only when the recipient explicitly calls `ack`. A notice or a wake does not mark a message as read.
- **Wake.** The receiver submits only a fixed notice, never the message body. It waits when the session is busy, has a queued prompt or attachment, or is waiting for approval.

## Requirements

- Hermes Agent with plugin and MCP support.
- Git.
- Python 3.11 or later in a separate environment for the MCP server, with `mcp==2.0.0`. Do not install it into Hermes' own environment.

## Install

The examples use Windows paths. Replace them with your own absolute paths.

### 1. Get the code and prepare the server environment

```text
git clone https://github.com/colibrishin/hermes-session-mailbox C:/src/hermes-session-mailbox
uv venv C:/mailbox-runtime
uv pip install --python C:/mailbox-runtime/Scripts/python.exe -r C:/src/hermes-session-mailbox/requirements-server.txt
```

### 2. Preview the installation

Run the installer with the Python interpreter that runs Hermes. That interpreter must be able to import `hermes_yaml`.

```text
<hermes-python> -B C:/src/hermes-session-mailbox/setup_bundle.py ^
  --profile default ^
  --profile-home C:/Users/me/.hermes ^
  --hermes-root C:/Users/me/.hermes ^
  --db C:/Users/me/.hermes/session-mailbox/mailbox.sqlite3 ^
  --server-python C:/mailbox-runtime/Scripts/python.exe
```

| Option | Meaning |
|---|---|
| `--profile` | Profile to install into: `default` or a named profile. |
| `--profile-home` | That profile's home. For a named profile, this is `<hermes-root>/profiles/<name>`. |
| `--hermes-root` | The global Hermes data directory. |
| `--db` | Shared mailbox file. Every profile that should talk to each other must use the same path. It must be outside `<hermes-root>/profiles` and must not be `state.db`. |
| `--server-python` | Interpreter from step 1. |
| `--hermes-command` | Optional. A JSON argv prefix for the Hermes CLI, for example `["C:/path/python.exe","-m","hermes_cli.main"]`. The default is `["hermes"]`. |
| `--scratch` | Optional. Temporary directory for staging. |

Without `--apply`, the installer only prints the plan. It changes nothing.

### 3. Apply and enable

Add `--apply` to the same command. The installer:

1. installs the plugin with `hermes plugins install` but leaves it disabled;
2. stores the mailbox settings with `hermes config set`;
3. registers the server with `hermes mcp add session_mailbox`, which also connects to it and discovers its tools.

Then enable and check the plugin:

```text
hermes --profile default plugins enable session-mailbox-bundle --no-allow-tool-override
hermes --profile default mcp test session_mailbox
```

Repeat steps 2 and 3 for each profile that should use the mailbox. Use the same `--db` each time.

Sessions that are already open do not load new hooks. Start a new session after you enable the plugin. `/reload-mcp` refreshes the MCP tools only, not the hooks.

## Usage

The agent normally uses the bundled skill: `session-mailbox-bundle:hermes-peer-session-collaboration`. The basic flow is:

1. **Find your own address.** In the session's terminal, read `HERMES_SESSION_ID` and `HERMES_SESSION_PROFILE`.
2. **Send.** Call `send` with the sender and recipient pairs, the `body`, and a `key`. If you retry the same message with the same key, the mailbox stores it only once. Reusing a key with a different payload fails.
3. **Read.** The recipient calls `inbox` with its own address. When `has_more` is true, pass the last message ID as `after` to get the next page.
4. **Acknowledge.** The recipient calls `ack` only for messages it has actually read.
5. **Check.** The sender calls `message_status` to see whether the message is `queued` or `acknowledged`, and the wake state.

`list_sessions` lists known session addresses. Its `active_hint` field does not prove that a session is alive or idle.

## Safety model

- **Not authentication.** Sender addresses are claims by the caller. The trust boundary is the local OS user.
- **Messages are data, not instructions.** A peer message never grants permission or replaces the user's request. The hooks and the skill tell the agent this explicitly.
- **No automatic resend.** If a wake submission result is unknown, the receiver records it as `unknown` and does not try again.
- **Unsent text is not protected.** Text typed into a frontend but not yet sent is not visible to the runtime. The receiver cannot see it.

## Limitations

- **Two registrations.** Hermes loads plugin hooks and MCP servers separately, so this bundle registers both. Disabling the plugin does not remove the MCP server. Disabling the MCP server does not stop hooks that are already loaded. Manage each one with its own `hermes plugins` or `hermes mcp` command.
- **Wake needs a running owner.** Automatic wake works only when the recipient session runs in a Hermes runtime that has the plugin enabled. Otherwise the message waits in the inbox.
- **No `plugins update`.** The installer installs from a temporary local copy. To upgrade, reinstall from a newer checkout.
- **The installer stops instead of overwriting.** It stops if `session_mailbox` already points to a different server, if installed bundle files differ, or if the older `session-mailbox-notify` plugin is enabled. Resolve the conflict, then run it again.
- **Do not use Hermes `state.db` as the mailbox.** The installer rejects it.

## License

MIT. See [LICENSE](LICENSE).
