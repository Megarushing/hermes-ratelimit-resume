# hermes-ratelimit-resume

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that **auto-continues a
gateway chat when your provider's usage limit resets** — the Hermes equivalent of Claude Code's
"continue automatically at usage limit".

You hit a subscription window (e.g. the Codex 5-hour limit) in Discord, Telegram, Slack, … and
Hermes stops with *"Limit resets at 14:00"*. Without this plugin you have to come back and say
"continue" yourself (or babysit a `/loop`). With it:

```
⏸️ Rate limited — I'll auto-continue at 14:01.
… two hours later …
▶️ Rate limit reset — auto-continuing now.
▶️ auto-continued — picking up where I left off: …
```

## Why a plugin

This plugin uses **only public plugin APIs** — no Hermes source patch. Public API contract changes
can still require an update: version 1.0.1 distinguishes asynchronous queue acceptance from an
actual dispatched turn and successful provider request. Revalidate the optional installed-Hermes
contract test after gateway updates; do not assume a `True` submission result proves delivery.

## How it works

| Hook / API | Use |
|---|---|
| `transform_api_error_classification` | Observer only (returns `None`, never changes Hermes' classification). Stashes the reset time from the raw error body — a streamed Codex error drops it from the message text. |
| `api_request_error` | On a 429 / `rate_limit` in a gateway chat, reads the reset time (`resets_in_seconds`, `resets_at`, "resets in 2h 5m") and parks the chat. |
| `pre_gateway_dispatch` | Receives the private attempt marker on our own internal event; records ingress without claiming the agent ran. Also learns which chat typed `/ratelimit-test`. |
| `pre_llm_call` | Binds the marked continuation to its actual turn/task IDs, or cancels superseded automatic work when a fresh human turn starts. |
| `post_api_request` | Confirms progress only for the bound continuation's turn/task IDs before posting the resumed notice and removing the record. A prior busy turn cannot acknowledge it. |
| `ctx.inject_message(..., session_key=…)` | The wake-up: a "continue" turn in the same chat, with full history. |
| `hermes send` | The visible ⏸️ / ▶️ notices (the injected text itself is agent input and never shows in the chat). |

- Pending resumes persist in `$HERMES_HOME/state/ratelimit-resume.json`, so a gateway restart keeps them.
- A small waker thread checks every 60s. It runs only in the `hermes gateway run` process.
- A queued submission stays pending. Without an ingress receipt it can retry after 120s, including
  when a disconnected adapter reconnects. Once received/running, it is not repeatedly injected.
- After 15 minutes without confirmation (a new 15-minute window starts at ingress), automatic
  attempts stop and the record remains `blocked`, with one failure notice. It is never silently
  deleted as though work resumed. A late matching provider success can still resolve it.
- The resumed notice means a matching provider request succeeded, **not that the whole task finished**.
- Resets shorter than 10 minutes normally stay with Hermes' own retry. An already-pending resume
  can re-arm for a short new reset rather than getting stuck after a dispatched attempt.
  Resets longer than `max_wait_hours` (default 6h — i.e. weekly windows) are left alone.

## Install

```sh
hermes plugins install https://github.com/Megarushing/hermes-ratelimit-resume
hermes plugins enable ratelimit-resume
hermes config set plugins.entries.ratelimit-resume.allow_gateway_injection true
# From a separate operator shell, or use /restart in the gateway chat:
systemctl --user restart hermes-gateway
```

Or clone it into `~/.hermes/plugins/ratelimit-resume` instead of `plugins install`.

`allow_gateway_injection` is required — Hermes denies plugin message injection by default.
An installed gateway imports this plugin and keeps its waker in memory: source edits alone do not
activate the fix. Do not restart the gateway from a tool running inside its own active turn. Apply
with the supported chat `/restart` or a genuinely separate operator shell, then use `/ratelimit-test`.
Disable with `hermes plugins disable ratelimit-resume` and restart the owning gateway. No new
systemd unit, OS cron or separate daemon is installed.

## Settings

Under `plugins.entries.ratelimit-resume.settings` in `config.yaml` (also shown in the Desktop
Plugins tab):

| Key | Default | Meaning |
|---|---|---|
| `platforms` | `[]` (all) | Gateway platforms to auto-continue, e.g. `[discord, telegram]`. |
| `max_wait_hours` | `6` | Longest reset to wait for. |
| `min_wait_seconds` | `600` | Shorter resets are left to Hermes' own retry. |
| `margin_seconds` | `60` | Continue this long after the stated reset. |
| `notices` | `true` | Post the ⏸️ / ▶️ notices. |
| `resume_message` | `Rate limit reset — continue where you stopped. Start your reply with "▶️ *auto-continued*".` | Text injected into the agent at wake-up. |

## Commands

- `/ratelimit-test` — arms a fake resume for this chat in 60s (end-to-end check). Uses its own slot, so it never replaces a real pending resume.
- `/ratelimit-status` — lists pending resumes; `/ratelimit-status clear` drops them.

Logs: `grep ratelimit-resume ~/.hermes/logs/gateway.log`.

## Limits

- Messaging gateway only. CLI, TUI and Desktop sessions are ignored.
- Needs the provider to state its reset time (Codex and most subscription providers do). With no
  reset time, nothing is parked and Hermes' normal error shows.
- Requires gateway ingress metadata plus `pre_llm_call` and `post_api_request` correlation fields.
  Missing acknowledgement support retains/blocks the request instead of announcing success.
- This is not an exactly-once distributed delivery guarantee. A process crash between ingress and
  agent execution can leave a blocked record requiring manual continuation. Stale marked attempts
  are dropped once the record is cancelled or re-armed; failed automatic work remains inspectable.
- The notices use `hermes send`, which reads the bot token from the gateway's environment; it is
  launched from inside the gateway process.

## Tests

```sh
python3 -m unittest discover -s tests -v
# Optional: run under an installed Hermes dependency environment, with a trusted source checkout:
HERMES_GATEWAY_SRC=/path/to/hermes-agent hermes --run-module unittest discover -s tests -v
```

The optional test uses the actual installed `PluginContext` and gateway scheduler/dispatch code.
It reproduces queue acceptance followed by an asynchronously unroutable adapter, then reconnection,
and verifies retained state, turn correlation and one confirmed notice. Its adapter/provider-success
events are explicit fixtures: it makes no network/model calls and is not a live Discord reset test.

## License

MIT
