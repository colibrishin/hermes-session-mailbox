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

Upgrading from 1.x? See [MIGRATION.md](MIGRATION.md).

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
- **Focused coding sessions hide the tools.** With `agent.coding_context: focus`, Hermes limits a code-workspace session to the coding toolset, so the `session-mailbox` tools are not offered. The default (`auto`) is not affected.
- **Disabled in `hermes tools`.** If you turn off the `session-mailbox` toolset in `hermes tools`, the tools stay off until you turn it back on.
- **Do not use Hermes `state.db` as the mailbox.** The plugin rejects it.

## CI

GitHub Actions (`.github/workflows/ci.yml`) installs real upstream [Hermes Agent](https://github.com/NousResearch/hermes-agent) and checks this plugin's interface with it. Each job makes a plain Python 3.14 venv and runs `pip install -e` on an upstream checkout. Then it runs `tests/`.

| Test file | What it checks |
|---|---|
| `test_a_install.py` | `hermes plugins install file://<checkout> --enable` works in a fresh default home and in a named profile. It asks no question, enables the plugin, and finds no Python dependency declaration. |
| `test_b_native_load.py` | The native plugin manager loads the five tools with their schemas. The toolset resolves for the `cli` platform. The three hooks fire at their native call sites. The skill opens through `skill_view`. Unload removes everything, including the wake monitor. |
| `test_c_cross_profile.py` | Send, inbox, ack, and status work across profiles through the tool registry. A retry is deduplicated, key reuse with a different payload fails, and an ACK from the wrong recipient is rejected. |
| `test_d_wake_receiver.py` | The wake receiver uses the real `prompt.submit` of `tui_gateway.server`. Only the model run is stubbed. It checks the stored recipient turn, waiting while busy or waiting for approval, no retry after an unknown result, and clean start and stop. |

The jobs:

- **pinned** runs Hermes `v0.22.2` (commit `d923a5e`), the version this plugin was developed against. It runs on Ubuntu and Windows for every push and pull request, and it must pass.
- **latest** runs the upstream `main` branch on Ubuntu, for every push and once a week. It may fail without blocking. A failure there is an early warning that upstream changed an interface this plugin uses.

The tests use only temporary directories. They also redirect `HOME`, `USERPROFILE`, `LOCALAPPDATA` and `APPDATA`, because Hermes writes launchers and runtimes under the user's default data directory. The install test copies the last commit of this checkout, so commit your plugin changes before you run it.

Redirecting the environment is not enough on its own. The Hermes package manager decides which data root owns a Hermes checkout from the checkout's location and install stamp, not from `HOME` or `LOCALAPPDATA`. A checkout inside your Hermes data directory (for example the one Hermes runs from, `~/.hermes/hermes-agent` or `%LOCALAPPDATA%\hermes\hermes-agent`) can therefore make a test install write into your real `bin/`, `installs/` and `tools/`. So `tests/_support.py` checks, before it writes anything, that the Hermes checkout, the venv, the temp directory and the package manager's owner and store all stay outside your Hermes data directory (`~/.hermes`, `%LOCALAPPDATA%\hermes`, or `$HERMES_HOME`). If one does not, every test module fails at import with `REFUSING TO RUN`.

### Running the tests locally

Use a dedicated Hermes clone, venv and temp directory, all OUTSIDE your Hermes data directory. Never `pip install -e` the checkout Hermes itself runs from. Do not run the tests from a shell that Hermes started either: such a shell sets `TMPDIR`, which may point inside the data directory. Start a new terminal instead. Upstream Hermes needs Python 3.14.

POSIX (`~/src` is an example; any directory outside `~/.hermes` works):

```bash
mkdir -p ~/src/hsm-ci && cd ~/src/hsm-ci
git clone https://github.com/NousResearch/hermes-agent.git
git -C hermes-agent checkout d923a5eb487a17c0a68801bba9ef0f56c81040a8   # v0.22.2, the pinned CI ref
python3.14 -m venv venv
venv/bin/python -m pip install -e ./hermes-agent
git clone <this repository> hermes-session-mailbox
mkdir -p tmp && TMPDIR="$PWD/tmp" venv/bin/python hermes-session-mailbox/tests/run.py
```

Windows (`C:\hsm-ci` is an example; any directory outside `%LOCALAPPDATA%\hermes` works):

```text
mkdir C:\hsm-ci && cd /d C:\hsm-ci
git clone https://github.com/NousResearch/hermes-agent.git
git -C hermes-agent checkout d923a5eb487a17c0a68801bba9ef0f56c81040a8
py -3.14 -m venv venv
venv\Scripts\python -m pip install -e .\hermes-agent
git clone <this repository> hermes-session-mailbox
mkdir tmp && set "TEMP=C:\hsm-ci\tmp" && set "TMP=C:\hsm-ci\tmp"
venv\Scripts\python hermes-session-mailbox\tests\run.py
```

`tests/run.py` exits non-zero when any test fails, errors or is skipped, and when it cannot remove the temporary fixture tree.

## License

MIT. See [LICENSE](LICENSE).
