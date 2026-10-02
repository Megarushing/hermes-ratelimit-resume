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

Upstream has open PRs for this (gateway: NousResearch/hermes-agent#57480; TUI/Desktop: #103048),
none merged as of 2026-10. This plugin uses **only public plugin APIs** — no Hermes source patch —
so `hermes update` does not break it. Once an upstream feature lands, you can uninstall it.

## How it works

| Hook / API | Use |
|---|---|
| `transform_api_error_classification` | Observer only (returns `None`, never changes Hermes' classification). Stashes the reset time from the raw error body — a streamed Codex error drops it from the message text. |
| `api_request_error` | On a 429 / `rate_limit` in a gateway chat, reads the reset time (`resets_in_seconds`, `resets_at`, "resets in 2h 5m") and parks the chat. |
| `post_api_request` | A later successful call in that session cancels the pending resume. |
| `pre_gateway_dispatch` | Only to learn which chat typed `/ratelimit-test`. |
| `ctx.inject_message(..., session_key=…)` | The wake-up: a "continue" turn in the same chat, with full history. |
| `hermes send` | The visible ⏸️ / ▶️ notices (the injected text itself is agent input and never shows in the chat). |

- Pending resumes persist in `$HERMES_HOME/state/ratelimit-resume.json`, so a gateway restart keeps them.
- A small waker thread checks every 60s. It runs only in the `hermes gateway run` process.
- Resets shorter than 10 minutes are left to Hermes' own retry (it already waits up to 600s).
  Resets longer than `max_wait_hours` (default 6h — i.e. weekly windows) are left alone.

## Install

```sh
hermes plugins install https://github.com/Megarushing/hermes-ratelimit-resume
hermes plugins enable ratelimit-resume
hermes config set plugins.entries.ratelimit-resume.allow_gateway_injection true
systemctl --user restart hermes-gateway   # or however you run the gateway
```

Or clone it into `~/.hermes/plugins/ratelimit-resume` instead of `plugins install`.

`allow_gateway_injection` is required — Hermes denies plugin message injection by default.

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
- The notices use `hermes send`, which reads the bot token from the gateway's environment; it is
  launched from inside the gateway process.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## License

MIT
