"""Auto-continue a gateway chat after a provider usage limit resets.

Public Hermes plugin APIs only: the ``api_request_error``,
``transform_api_error_classification``, ``post_api_request`` and
``pre_gateway_dispatch`` hooks, ``ctx.register_command`` and
``ctx.inject_message``. No Hermes source is patched, so ``hermes update``
leaves this plugin alone.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.plugins.ratelimit_resume")

# Defaults for the user settings under plugins.entries.ratelimit-resume.settings
# (see plugin.yaml config_schema).
# Shortest reset worth parking. Hermes itself sleeps and retries waits up to
# 600s (compute_error_backoff clamp), so shorter limits need no help.
MIN_WAIT_SECONDS = 600
# Longest reset worth parking. Multi-day (weekly) windows are left alone.
MAX_WAIT_SECONDS = 6 * 3600
# Send the continue this long after the provider's stated reset.
MARGIN_SECONDS = 60
# Gateway platforms to park; empty = every messaging platform.
PLATFORMS: tuple = ()
# Never parked: these are not messaging-gateway chats.
NON_GATEWAY_PLATFORMS = {"", "cli", "cron", "tui", "desktop", "dashboard", "api", "acp", "batch"}
# Post visible ⏸️/▶️ notices in the chat via `hermes send`.
NOTICES = True
# How often the waker thread checks for due resumes.
TICK_SECONDS = 60
# A due resume whose injection keeps failing is dropped after this long.
GIVE_UP_AFTER_SECONDS = 900
# How old a reset stashed by the classification hook may be when the error hook reads it.
STASH_MAX_AGE_SECONDS = 30
# Delay used by /ratelimit-test.
TEST_DELAY_SECONDS = 60
# Injected into the agent as user input; the chat never shows it, hence the reply tag.
RESUME_MESSAGE = ("Rate limit reset — continue where you stopped. "
                  "Start your reply with \"▶️ *auto-continued*\".")
# Visible chat notices, posted as the bot via the public `hermes send` CLI.
PARKED_NOTICE = "⏸️ Rate limited — I'll auto-continue at {when}."
RESUMED_NOTICE = "▶️ Rate limit reset — auto-continuing now."
# A re-arm moving the wake-up by less than this does not post a new parked notice.
RENOTIFY_SHIFT_SECONDS = 300
SEND_TIMEOUT_SECONDS = 60
TEST_SUFFIX = "#test"

_RESETS_IN_SECONDS_FIELD = re.compile(r"resets_in_seconds\W{1,4}(\d+(?:\.\d+)?)", re.I)
_RESETS_AT_FIELD = re.compile(r"resets_at\W{1,4}(\d{9,11}(?:\.\d+)?)", re.I)
_RESETS_IN_TEXT = re.compile(
    r"resets?\s+in\s+"
    r"(?:(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)\b\s*)?"
    r"(?:(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b)?",
    re.I,
)


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def state_path() -> Path:
    return hermes_home() / "state" / "ratelimit-resume.json"


def is_gateway_process(argv: Optional[list] = None) -> bool:
    """Only `hermes gateway run` may arm or fire resumes; a CLI process would
    swallow the injection into its own REPL instead of the gateway chat."""
    args = sys.argv if argv is None else argv
    return "gateway" in args and "run" in args


def reset_seconds_from_body(body: Any, now: float) -> Optional[float]:
    if not isinstance(body, dict):
        return None
    inner = body.get("error") if isinstance(body.get("error"), dict) else body
    value = inner.get("resets_in_seconds")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    value = inner.get("resets_at")
    if isinstance(value, (int, float)) and value > now:
        return float(value) - now
    return None


def reset_seconds_from_text(text: str, now: float) -> Optional[float]:
    if not text:
        return None
    m = _RESETS_IN_SECONDS_FIELD.search(text)
    if m and float(m.group(1)) > 0:
        return float(m.group(1))
    m = _RESETS_AT_FIELD.search(text)
    if m and float(m.group(1)) > now:
        return float(m.group(1)) - now
    for m in _RESETS_IN_TEXT.finditer(text):
        if any(m.groups()):
            h, mi, s = (float(g or 0) for g in m.groups())
            total = h * 3600 + mi * 60 + s
            if total > 0:
                return total
    return None


def lookup_session_key(session_id: str) -> Optional[str]:
    """Gateway routing key for a session id, from the gateway's session store."""
    try:
        data = json.loads((hermes_home() / "sessions" / "sessions.json").read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key, entry in data.items():
        if isinstance(entry, dict) and entry.get("session_id") == session_id:
            return entry.get("session_key") or key
    return None


def lookup_target(session_key: str) -> Optional[str]:
    """`hermes send` target (platform:chat_id[:thread_id]) for a session_key."""
    try:
        data = json.loads((hermes_home() / "sessions" / "sessions.json").read_text())
        origin = data[session_key].get("origin") or {}
    except Exception:
        return None
    platform, chat = origin.get("platform"), origin.get("chat_id")
    if not platform or not chat:
        return None
    thread = origin.get("thread_id")
    return f"{platform}:{chat}:{thread}" if thread else f"{platform}:{chat}"


def hermes_bin() -> Optional[str]:
    launcher = hermes_home() / "hermes-agent" / ".hermes" / "bin" / "hermes"
    return str(launcher) if launcher.exists() else shutil.which("hermes")


def send_notice(session_key: str, text: str) -> None:
    """Fire-and-forget visible notice; never blocks or raises into a hook."""
    target, exe = lookup_target(session_key), hermes_bin()
    if not target or not exe:
        logger.warning("ratelimit-resume: cannot notify %s (target=%s bin=%s)", session_key, target, exe)
        return

    def run() -> None:
        try:
            r = subprocess.run([exe, "send", "-q", "-t", target, text], capture_output=True,
                               text=True, timeout=SEND_TIMEOUT_SECONDS)
            if r.returncode:
                logger.warning("ratelimit-resume: hermes send failed (%s): %s", r.returncode,
                               (r.stderr or r.stdout)[-300:])
        except Exception:
            logger.warning("ratelimit-resume: hermes send crashed", exc_info=True)

    threading.Thread(target=run, name="ratelimit-resume-send", daemon=True).start()


class Store:
    """Pending resumes keyed by session_key, persisted so a restart keeps them."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def arm(self, session_key: str, session_id: str, wake_at: float, reason: str) -> Optional[float]:
        """Returns the previous wake_at for this key (None when newly parked)."""
        with self.lock:
            data = self.load()
            previous = (data.get(session_key) or {}).get("wake_at")
            data[session_key] = {
                "session_id": session_id,
                "wake_at": wake_at,
                "armed_at": time.time(),
                "reason": reason,
            }
            self.save(data)
            return previous

    def cancel_session_id(self, session_id: str) -> list:
        with self.lock:
            data = self.load()
            gone = [k for k, v in data.items() if v.get("session_id") == session_id]
            for k in gone:
                del data[k]
            if gone:
                self.save(data)
            return gone

    def clear(self) -> int:
        with self.lock:
            n = len(self.load())
            self.save({})
            return n


class Settings:
    """User settings; ``get`` is ``ctx.get_config`` (or a stub in tests)."""

    def __init__(self, get=lambda key, default=None: default):
        def num(key, default):
            try:
                return float(get(key, default))
            except (TypeError, ValueError):
                logger.warning("ratelimit-resume: setting %s is not a number; using %s", key, default)
                return float(default)

        self.min_wait = num("min_wait_seconds", MIN_WAIT_SECONDS)
        self.max_wait = num("max_wait_hours", MAX_WAIT_SECONDS / 3600) * 3600
        self.margin = num("margin_seconds", MARGIN_SECONDS)
        raw = get("platforms", list(PLATFORMS)) or []
        if isinstance(raw, str):
            raw = [p for p in re.split(r"[,\s]+", raw) if p]
        self.platforms = {str(p).strip().lower() for p in raw}
        self.notices = bool(get("notices", NOTICES))
        self.resume_message = str(get("resume_message", RESUME_MESSAGE) or RESUME_MESSAGE)

    def platform_enabled(self, platform: str) -> bool:
        platform = (platform or "").lower()
        if platform in NON_GATEWAY_PLATFORMS:
            return False
        return not self.platforms or platform in self.platforms


class ResumePlugin:
    def __init__(self, inject, store: Store, gateway: bool, notify=send_notice,
                 settings: Optional[Settings] = None):
        self.s = settings or Settings()
        self.inject = inject
        self.notify = notify if self.s.notices else (lambda key, text: None)
        self.store = store
        self.gateway = gateway
        self._stash_lock = threading.Lock()
        self._stash: dict = {}
        self._test_key: Optional[str] = None
        self._thread: Optional[threading.Thread] = None

    # --- hooks -----------------------------------------------------------
    def on_classify(self, *, provider: str = "", status_code: Any = None,
                    error_body: Any = None, error_message: str = "", **_: Any) -> None:
        """Observer only (returns None, so Hermes' classification is unchanged).
        Runs on a different thread from api_request_error and sees the raw body,
        which a streamed Codex error drops from its message text."""
        now = time.time()
        secs = reset_seconds_from_body(error_body, now) or reset_seconds_from_text(error_message, now)
        if secs:
            with self._stash_lock:
                self._stash[provider or ""] = (now, now + secs)
        return None

    def on_api_error(self, *, session_id: str = "", platform: str = "", provider: str = "",
                     status_code: Any = None, reason: str = "", error: Any = None,
                     **_: Any) -> None:
        if not self.gateway or not self.s.platform_enabled(platform) or not session_id:
            return
        if status_code != 429 and reason != "rate_limit":
            return
        now = time.time()
        message = error.get("message", "") if isinstance(error, dict) else str(error or "")
        secs = reset_seconds_from_text(message, now)
        reset_at = now + secs if secs else None
        if reset_at is None:
            with self._stash_lock:
                seen_at, stashed = self._stash.get(provider or "", (0.0, None))
            if stashed and now - seen_at <= STASH_MAX_AGE_SECONDS:
                reset_at = stashed
        if reset_at is None:
            logger.info("ratelimit-resume: %s rate-limited with no reset time; not parking", session_id)
            return
        wait = reset_at - now
        if wait <= self.s.min_wait:
            return
        if wait > self.s.max_wait:
            logger.info("ratelimit-resume: %s reset in %.1fh exceeds cap; not parking",
                        session_id, wait / 3600)
            return
        key = lookup_session_key(session_id)
        if not key:
            logger.warning("ratelimit-resume: no session_key for %s; not parking", session_id)
            return
        wake_at = reset_at + self.s.margin
        previous = self.store.arm(key, session_id, wake_at, "rate_limit")
        if previous is None or abs(previous - wake_at) > RENOTIFY_SHIFT_SECONDS:
            self.notify(key, PARKED_NOTICE.format(when=time.strftime("%H:%M", time.localtime(wake_at))))
        logger.info("ratelimit-resume: parked %s until %s", key,
                    time.strftime("%H:%M:%S", time.localtime(wake_at)))

    def on_api_success(self, *, session_id: str = "", **_: Any) -> None:
        if not self.gateway or not session_id:
            return
        for key in self.store.cancel_session_id(session_id):
            logger.info("ratelimit-resume: %s answered again; dropped pending resume", key)

    def on_gateway_dispatch(self, *, event: Any = None, session_store: Any = None, **_: Any) -> None:
        """Remember which chat typed /ratelimit-test (command handlers get no session)."""
        text = str(getattr(event, "text", "") or "").strip()
        if not text.startswith("/ratelimit-test"):
            return None
        source = getattr(event, "source", None)
        try:
            gen = getattr(session_store, "_generate_session_key", None)
            if gen is not None:
                self._test_key = gen(source)
            else:
                from gateway.session import build_session_key
                self._test_key = build_session_key(source)
        except Exception:
            logger.warning("ratelimit-resume: could not resolve test session key", exc_info=True)
            self._test_key = None
        return None

    # --- commands --------------------------------------------------------
    def cmd_test(self, raw_args: str = "") -> str:
        if not self.gateway:
            return "ratelimit-resume only works in the gateway."
        key = self._test_key
        if not key:
            return "Could not find this chat's session key; test not armed."
        # Own slot, so a test never replaces a real pending resume for the same chat.
        self.store.arm(key + TEST_SUFFIX, "test", time.time() + TEST_DELAY_SECONDS, "test")
        return f"Test armed: this chat gets \"{self.s.resume_message}\" in {TEST_DELAY_SECONDS}s."

    def cmd_status(self, raw_args: str = "") -> str:
        if raw_args.strip() == "clear":
            return f"Cleared {self.store.clear()} pending resume(s)."
        data = self.store.load()
        if not data:
            return "No pending rate-limit resumes."
        lines = []
        for key, v in sorted(data.items(), key=lambda kv: kv[1].get("wake_at", 0)):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(v.get("wake_at", 0)))
            lines.append(f"- {key} → {when} ({v.get('reason')})")
        return "Pending rate-limit resumes:\n" + "\n".join(lines)

    # --- waker -----------------------------------------------------------
    def tick(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self.store.lock:
            data = self.store.load()
            changed = False
            for key in list(data):
                entry = data[key]
                if entry.get("wake_at", 0) > now:
                    continue
                ok = False
                try:
                    target = key[: -len(TEST_SUFFIX)] if key.endswith(TEST_SUFFIX) else key
                    ok = bool(self.inject(self.s.resume_message, role="user", session_key=target))
                except Exception:
                    logger.warning("ratelimit-resume: inject failed for %s", key, exc_info=True)
                if ok:
                    logger.info("ratelimit-resume: resumed %s", key)
                    self.notify(target, RESUMED_NOTICE)
                    del data[key]
                    changed = True
                elif now - entry["wake_at"] > GIVE_UP_AFTER_SECONDS:
                    logger.warning("ratelimit-resume: giving up on %s after repeated inject failures", key)
                    del data[key]
                    changed = True
            if changed:
                self.store.save(data)

    def start(self) -> None:
        if not self.gateway or self._thread is not None:
            return

        def loop() -> None:
            while True:
                try:
                    self.tick()
                except Exception:
                    logger.warning("ratelimit-resume: tick failed", exc_info=True)
                time.sleep(TICK_SECONDS)

        self._thread = threading.Thread(target=loop, name="ratelimit-resume", daemon=True)
        self._thread.start()
