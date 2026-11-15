# Hermes Rate-Limit Resume

Automatically continue a Hermes messaging-gateway conversation after a provider's usage limit resets.

A subscription limit can interrupt a task for hours. Without automatic continuation, you have to watch the clock and return to say “continue”. This plugin remembers the affected conversation, waits for the reported reset, and submits a continuation to that same session. No shell polling loop, separate daemon, or Hermes source patch is required.

## What it looks like

Illustrative conversation with the default settings:

```text
You: Finish the report.

Bot: ⏸️ Rate limited — I'll auto-continue at 14:01.

… after the provider's reset time and safety margin …

Bot: ▶️ Rate limit reset — auto-continuing now.
Bot: ▶️ auto-continued
     Picking up where I left off…
```

The plugin posts the pause/resume notices; the agent generates the continuation reply. Their exact ordering can vary. The resumed notice confirms a **matching provider request succeeded**, not that the whole task finished.

**This starts a real agent turn.** It can consume model credits and continue tool use under the gateway's existing permissions. Enable it only where unattended continuation of an interrupted task is appropriate.

## Features

- Resumes the original gateway session with its existing conversation history.
- Reads explicit provider reset times instead of guessing when a quota will recover.
- Configurable platform scope, minimum/maximum wait, safety margin, notices and continuation text.
- Persists pending work across gateway restarts, with separate state for each profile.
- Keeps unconfirmed submissions pending rather than treating queue acceptance as success.
- Provides status and end-to-end test commands inside the gateway chat.
- Uses native hooks and message injection; does not patch Hermes, alter provider error classification, or install an OS cron job.

Short rate limits are normally left to Hermes's own retry handling. This plugin fills the longer-wait gap; it does not bypass provider quotas or increase your allowance.

## Requirements and compatibility

1. A working [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) messaging gateway, with the intended platform already connected and its provider configured.
2. Native plugin hooks `transform_api_error_classification`, `api_request_error`, `pre_gateway_dispatch`, `pre_llm_call`, and `post_api_request`; command registration; and `ctx.inject_message(..., session_key=...)` support. The gateway must supply the session/turn correlation needed to confirm the injected turn.
3. Explicit gateway-injection permission for this plugin, shown in the installation commands below. Enabling the plugin alone is not enough.
4. A provider error that includes a supported reset time: `resets_in_seconds`, Unix-epoch `resets_at`, or English relative text such as `resets in 2h 5m`. A bare 429 or an absolute wall-clock phrase alone is insufficient.
5. For visible notices, a working `hermes send` command in the gateway environment. It uses the existing bot configuration; this plugin needs no separate token.

The runtime uses Python's standard library; no plugin-specific Python packages are required. Installed-host checks use Hermes's own dependency environment. Compatibility has been checked against Hermes source revision **`3b42157858`** with Python **3.11.15**; this is not a minimum release guarantee or a claim of universal platform support. Native API changes can require a plugin update.

See the [Hermes plugin documentation](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/) for the host installation and plugin lifecycle.

## Install

### Default profile

Run these commands in your terminal:

```sh
hermes plugins install https://github.com/Megarushing/hermes-ratelimit-resume --no-enable --yes-deps
hermes config set plugins.entries.ratelimit-resume.allow_gateway_injection true
hermes plugins enable ratelimit-resume
```

`--no-enable` separates installation from activation. `--yes-deps` explicitly accepts dependency preparation by the Hermes installer; review any plugin before trusting it. Although this plugin has no extra packages, a fresh Hermes installation may still need to download its managed Python/runtime dependencies. If that fails, check `hermes pm doctor` before investigating the plugin.

**The permission is `plugins.entries.ratelimit-resume.allow_gateway_injection`, not a key inside `settings`.** It authorises the plugin to submit agent turns through the gateway. It does not bypass the host's profile ownership or user-authorisation checks.

Optionally limit new automatic resumes to one platform before restarting:

```sh
hermes config set plugins.entries.ratelimit-resume.settings.platforms '["discord"]'
```

The default empty list allows all supported messaging-gateway platforms; it does **not** disable the plugin.

Then restart the gateway **from an external terminal**, not through a tool running inside that gateway's active agent turn:

```sh
hermes gateway restart
```

For a foreground or container deployment, restart the existing gateway through its normal supervisor instead. Do not launch a second gateway against the same profile. Source edits and hot-reload receipts alone do not prove that the in-memory waker has been replaced.

### Named profiles

Use the same profile for every step. For a profile named `work`:

```sh
hermes --profile work plugins install https://github.com/Megarushing/hermes-ratelimit-resume --no-enable --yes-deps
hermes --profile work config set plugins.entries.ratelimit-resume.allow_gateway_injection true
hermes --profile work config set plugins.entries.ratelimit-resume.settings.platforms '["discord"]'
hermes --profile work plugins enable ratelimit-resume
hermes --profile work gateway restart
```

Each profile has its own permission, settings and pending-state file. Installing or configuring the default profile does not configure another one.

### Verify the installation

```sh
hermes plugins list
hermes plugins doctor ratelimit-resume --ci
```

For a named profile, prefix both commands with `hermes --profile work` instead of `hermes`.

Doctor validates discovery, the manifest, import and hook registration. **It does not prove that a live gateway will admit and run a continuation.** To check that path:

1. Start with a harmless conversation in a test chat; the next step uses your configured continuation text and may resume prior work.
2. Send `/ratelimit-test` **in the gateway chat**, not the terminal. This schedules a real continuation without needing a provider rate limit.
3. Allow roughly one to two minutes: the test delay is 60 seconds and the waker checks every 60 seconds. A busy gateway or provider can take longer.
4. Check `/ratelimit-status`, the actual agent response, and the matched success notice if notices are enabled. “Test armed” or “submission queued” alone is not success.

The test uses a separate pending slot so it does not replace a real rate-limit resume. It still makes a real model request and may use tools or incur charges. Do not use it casually in a production conversation with unfinished sensitive work.

## Configuration

Plugin settings live under `plugins.entries.ratelimit-resume.settings` in the **active profile's** `config.yaml`. Use `hermes config set` rather than replacing the whole file. The defaults below mirror [`plugin.yaml`](plugin.yaml); restart the owning gateway after changes.

| Setting | Default | Meaning |
| --- | --- | --- |
| `platforms` | `[]` | All messaging-gateway platforms. Use platform names such as `["discord", "telegram"]` to restrict **new** rate-limit captures. These are not channel IDs. |
| `max_wait_hours` | `6` | Maximum provider-reported wait to accept, before the safety margin. Longer waits are not parked. |
| `min_wait_seconds` | `600` | New resets at or below this threshold are left to Hermes's normal retry. An already-pending resume can re-arm for a shorter reset. |
| `margin_seconds` | `60` | Additional delay after the provider's reset time. The waker's polling interval can add further delay. |
| `notices` | `true` | Send pause, confirmed-resume and blocked notices through `hermes send`. `false` suppresses all three, not the agent's own reply. |
| `resume_message` | `Rate limit reset — continue where you stopped. Start your reply with "▶️ *auto-continued*".` | Agent-facing continuation instruction. An internal correlation marker is appended automatically; do not add one yourself. |

`allow_gateway_injection` is a separate **host permission**, not one of these settings. Its required value is boolean `true` at `plugins.entries.ratelimit-resume.allow_gateway_injection`.

### Choose the platforms

```sh
hermes config set plugins.entries.ratelimit-resume.settings.platforms '["discord", "telegram"]'
```

Use the platform identifiers recognised by your gateway. Empty `[]` means all messaging platforms; CLI, TUI, Desktop, dashboard, API, ACP, batch and cron contexts remain excluded. There is no per-channel or per-user capture allowlist in this plugin.

Changing `platforms` does not remove already-pending entries, and `/ratelimit-test` is an explicit test rather than a filtered provider-error capture. To stop all activity, disable and restart the plugin's gateway as described below.

### Adjust the waiting window

For new reset waits above 15 minutes and up to 8 hours, with a two-minute safety margin:

```sh
hermes config set plugins.entries.ratelimit-resume.settings.min_wait_seconds 900
hermes config set plugins.entries.ratelimit-resume.settings.max_wait_hours 8
hermes config set plugins.entries.ratelimit-resume.settings.margin_seconds 120
```

Use non-negative integer seconds and a positive, finite maximum greater than the minimum window. A larger maximum opts into resuming older interrupted work. Changing the settings does not recalculate wake times already saved in the queue.

### Reduce chat notifications

```sh
hermes config set plugins.entries.ratelimit-resume.settings.notices false
```

Use a boolean, not a quoted string containing `false`. This also hides the blocked warning; use `/ratelimit-status` when investigating a quiet queue. To remove the agent's `auto-continued` prefix as well, change `resume_message` separately.

### Customise the continuation instruction

```sh
hermes config set plugins.entries.ratelimit-resume.settings.resume_message 'The usage limit has reset. Continue the interrupted task and briefly state what you resumed.'
```

Keep this instruction appropriate for unattended execution. It is submitted as agent input, not posted as a user-visible chat message by the plugin. The agent's response may quote or act on it. An empty value falls back to the default instruction.

## Chat commands

| Command | Scope and effect |
| --- | --- |
| `/ratelimit-test` | Arms a real continuation for the current gateway chat after 60 seconds, in a separate test slot. |
| `/ratelimit-status` | Lists pending entries across the active profile, including their routing keys, wake times, reasons and states. |
| `/ratelimit-status clear` | Removes **all pending entries in that profile**, including test slots—not just entries from the chat where it was sent. |

**Clear is not an undo button for a running agent turn.** Removing local tracking cannot retract work already admitted or started by the gateway. These commands have no additional plugin-specific administrator check; access follows the host's command policy. Keep that profile-wide visibility and control in mind on shared gateways.

Times in notices and status output use the **gateway process's local timezone**, not necessarily the viewer's timezone.

## How it works

| Component | Responsibility |
| --- | --- |
| `transform_api_error_classification` | Observes raw reset information without replacing Hermes's error classification. |
| `api_request_error` | Parks an eligible rate-limited gateway session when a reset time and routing key are available. |
| Persistent state | Stores the session reference, wake time, attempt marker and confirmation state across restarts. |
| Background waker | Checks every 60 seconds inside the gateway process and submits due continuations through `ctx.inject_message`. |
| `pre_gateway_dispatch` / `pre_llm_call` | Observe available ingress metadata and bind the marked continuation to its actual turn/task IDs. Internal events can skip the first hook, so turn-start correlation matters. |
| `post_api_request` | Confirms matching provider progress before announcing success and clearing the bound continuation. |
| `hermes send` | Posts visible notices through the existing platform configuration; the injected instruction itself is not a chat post. |

Submission acceptance is not execution. Unacknowledged queued submissions can retry after 120 seconds; immediate rejections are eligible for another attempt after 60 seconds. Once marked dispatched/running, an entry is not repeatedly injected.

After 15 minutes without confirmation, an entry becomes `blocked` and is retained with one warning if notices are enabled. When an ingress receipt is recorded, that starts a fresh confirmation window. A late matching provider success can still resolve a blocked entry. A sufficiently overdue entry may therefore become blocked after a long gateway outage rather than immediately resuming stale work.

Fresh unmarked work in the same session supersedes its pending automatic resume; the explicit test slot is separate. A new provider limit can re-arm pending work. Neither behaviour constitutes an exactly-once delivery guarantee.

## Privacy, storage and unattended execution

- Pending state lives at `<HERMES_HOME>/state/ratelimit-resume.json`, outside the plugin checkout. Keep it out of Git, support tickets and public logs.
- The file stores routing/session identifiers, timestamps, attempt IDs and status—not a copy of the conversation or provider credentials. The resumed agent still has access to the original conversation history through Hermes.
- State is not encrypted and the plugin does not impose its own restrictive file mode; protect the profile directory through your OS permissions. Completed/cancelled entries are removed, while blocked entries remain until resolved or cleared. There is no age-based purge setting.
- The plugin does not bypass host profile ownership or user authorisation. The resumed turn still has the gateway's normal tool access; this is not a sandbox or a new approval boundary.
- Reset detection and waiting make no extra LLM calls. **The continuation itself is an ordinary model-backed turn**, and the test command starts one too.
- Notices can expose that a conversation is waiting or has resumed. `/ratelimit-status` can expose other pending conversations' routing keys within the same profile.
- Diagnostic logs include profile filesystem paths and routing identifiers. Redact them before sharing. The rejection diagnostic does not log prompts or credential values; that is not a guarantee about every host log or CLI error.

## Troubleshooting and limits

**Nothing was scheduled:** check that this is a real messaging-gateway session, the platform is allowed, the error is a recognised rate limit, and a supported reset time is present within your configured window. Missing reset information is deliberately not guessed. The waker starts only in a process whose arguments identify `hermes gateway run`; an embedded/custom host may need separate compatibility work.

**Scheduled, but no agent response:** check `allow_gateway_injection` in the correct profile, gateway connectivity, `/ratelimit-status`, and the gateway logs. A `submission queued` log is not proof of dispatch or provider success.

**“Automatic continuation could not be confirmed”:** no matching confirmation arrived before the deadline. The entry remains blocked; the message does not by itself diagnose a bad reset-time parser. Inspect admission and correlation logs before blaming the provider.

**Profile-ownership rejection:** a host message such as `belongs to profile None, not the plugin's profile None` occurs before the agent starts. Version 1.0.3 logs `registration_home`, `routing_home` and `state_home` when submission is rejected. These diagnostics do not change profiles or bypass authorisation and are **not a claimed fix** for an intermittent host mismatch.

**Agent continued, but no visible notice:** `notices` may be disabled, `hermes send` may have failed, or the gateway may not have supplied the expected matching correlation fields. Check the record and logs rather than inferring delivery from one missing banner.

**Update or disable appears ineffective:** the plugin keeps a background thread and its settings in memory. Restart the owning gateway externally; do not rely on hot reload to replace or stop an existing waker.

Additional limits:

- No exactly-once guarantee across process crashes, asynchronous queueing and provider execution. Retained state may require manual recovery.
- Run one gateway process per profile/state file. The file lock is in-process, not a cross-process coordination mechanism.
- Missing/corrupt/unreadable state is treated as an empty queue by the loader. Back up the profile if you need recoverability; do not hand-edit live state.
- Notice sending is best-effort and can lag or fail independently of the resumed turn.
- Not every provider exposes a supported reset time, and not every platform/host version has been live-tested. Rerun the installed-host contract test after Hermes upgrades.

Gateway logs normally live at `<HERMES_HOME>/logs/gateway.log`. Search for `ratelimit-resume`, then correlate the timestamp with the host's admission/dispatch messages. Never publish an unredacted log or state file.

## Update, disable and recovery

For an installer-managed copy:

```sh
hermes plugins update ratelimit-resume
hermes gateway restart
```

Use `hermes --profile NAME` for the owning named profile. A manually linked checkout is updated with Git instead; do not mix installation methods or overwrite an existing link with a second clone.

To disable:

```sh
hermes plugins disable ratelimit-resume
hermes gateway restart
```

Run the restart externally. Disabling and restarting stops this plugin's automatic wake-ups; it does not delete pending state or revoke work already started by the host. Re-enabling later can revisit saved pending entries. If you intentionally want to discard the queue, review `/ratelimit-status` and use `/ratelimit-status clear` before disabling, understanding its profile-wide scope.

For a blocked task, inspect its status and decide whether to continue manually in that conversation. Do not clear unrelated chats merely to remove one warning. Avoid rolling back to version 1.0.0, which treated queued submissions as successful execution and could silently lose pending work.

## Development and tests

The ordinary fixture suite uses only Python's standard library; the installed-host contract test is skipped unless explicitly selected:

```sh
git clone https://github.com/Megarushing/hermes-ratelimit-resume
cd hermes-ratelimit-resume
python3 -m unittest discover -s tests -v
```

For the real Hermes API contract, use a trusted Hermes checkout and that installation's dependency environment:

```sh
HERMES_GATEWAY_SRC=/path/to/hermes-agent hermes --run-module unittest discover -s tests -v
hermes plugins doctor . --ci
```

Tests cover reset parsing, platform/settings behaviour, pending-state persistence, retry and confirmation, fresh-turn cancellation, separate test slots, and fail-safe rejection diagnostics. The optional contract test uses the actual host's `PluginContext`, profile guard and asynchronous gateway dispatch with fixture transport/provider events: it covers a rejected profile, an unavailable adapter, recovery and a matched success receipt.

No test above sends a production message or makes a model request. Passing them does not replace the opt-in live `/ratelimit-test`, and does not prove that every real provider error or gateway version behaves identically.

## Licence

MIT — see [LICENSE](LICENSE).
