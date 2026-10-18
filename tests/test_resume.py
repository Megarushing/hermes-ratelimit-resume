import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import resume  # noqa: E402

CODEX_TEXT = ("Error code: 429 - {'error': {'type': 'usage_limit_reached', "
              "'message': 'The usage limit has been reached', 'resets_in_seconds': 7380}}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "sessions").mkdir()
        (self.home / "sessions" / "sessions.json").write_text(json.dumps({
            "_README": "x",
            "agent:main:discord:group:1:2": {"session_key": "agent:main:discord:group:1:2",
                                             "session_id": "S1",
                                             "origin": {"platform": "discord", "chat_id": "1",
                                                        "thread_id": None}},
            "agent:main:discord:thread:7": {"session_id": "S7",
                                            "origin": {"platform": "discord", "chat_id": "5",
                                                       "thread_id": "7"}},
        }))
        self.env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        self.env.start()
        self.injected = []
        self.inject_ok = True

        def inject(content, role="user", *, session_key=None):
            self.injected.append((content, session_key))
            return self.inject_ok

        self.store = resume.Store(resume.state_path())
        self.notices = []
        self.p = resume.ResumePlugin(inject, self.store, gateway=True,
                                     notify=lambda key, text: self.notices.append((key, text)))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def error(self, **kw):
        base = dict(session_id="S1", platform="discord", provider="openai-codex",
                    status_code=429, reason="rate_limit",
                    error={"type": "RateLimitError", "message": CODEX_TEXT})
        base.update(kw)
        self.p.on_api_error(**base)


class RegistrationTests(unittest.TestCase):
    def test_turn_receipt_hook_is_registered_without_starting_a_waker_in_cli(self):
        import importlib.util
        path = Path(resume.__file__).with_name('__init__.py')
        spec = importlib.util.spec_from_file_location('resume_entry_fixture', path)
        entry = importlib.util.module_from_spec(spec); spec.loader.exec_module(entry)
        hooks, commands = {}, {}
        context = SimpleNamespace(inject_message=lambda *a, **kw: True,
            get_config=lambda key, default=None: default,
            register_hook=lambda name, callback: hooks.update({name: callback}),
            register_command=lambda name, callback, **kwargs: commands.update({name: callback}))
        with mock.patch.object(entry, 'is_gateway_process', return_value=False):
            entry.register(context)
        self.assertIn('pre_llm_call', hooks)
        self.assertEqual(hooks['pre_llm_call'], entry._PLUGIN.on_turn_start)
        self.assertEqual(set(commands), {'ratelimit-test', 'ratelimit-status'})
        self.assertIsNone(entry._PLUGIN._thread)


class ParseTests(unittest.TestCase):
    def test_text_field(self):
        self.assertEqual(resume.reset_seconds_from_text(CODEX_TEXT, 0), 7380)

    def test_text_human(self):
        self.assertEqual(resume.reset_seconds_from_text("usage limit, resets in 2h 5m", 0), 7500)

    def test_text_none(self):
        self.assertIsNone(resume.reset_seconds_from_text("HTTP 429: The usage limit has been reached", 0))

    def test_body_nested_and_flat(self):
        self.assertEqual(resume.reset_seconds_from_body({"error": {"resets_in_seconds": 30}}, 0), 30)
        self.assertEqual(resume.reset_seconds_from_body({"resets_at": 1100}, 1000), 100)

    def test_gateway_argv(self):
        self.assertTrue(resume.is_gateway_process(["-c", "gateway", "run"]))
        self.assertFalse(resume.is_gateway_process(["-c", "-z", "hi"]))


class ArmTests(Base):
    def test_arms_from_text(self):
        self.error()
        data = self.store.load()
        self.assertIn("agent:main:discord:group:1:2", data)
        wake = data["agent:main:discord:group:1:2"]["wake_at"]
        self.assertAlmostEqual(wake, time.time() + 7380 + resume.MARGIN_SECONDS, delta=5)

    def test_stream_error_uses_classifier_stash(self):
        self.p.on_classify(provider="openai-codex", status_code=429,
                           error_body={"type": "usage_limit_reached", "resets_in_seconds": 3000})
        self.error(error={"type": "ProviderStreamError",
                          "message": "Provider stream returned an error event - HTTP 429"})
        self.assertEqual(len(self.store.load()), 1)

    def test_stale_stash_ignored(self):
        self.p._stash["openai-codex"] = (time.time() - 999, time.time() + 3000)
        self.error(error={"message": "HTTP 429"})
        self.assertEqual(self.store.load(), {})

    def test_over_cap_not_parked(self):
        self.error(error={"message": "'resets_in_seconds': 164160"})
        self.assertEqual(self.store.load(), {})

    def test_short_reset_left_to_hermes(self):
        self.error(error={"message": "'resets_in_seconds': 171"})
        self.assertEqual(self.store.load(), {})

    def test_other_platform_ignored(self):
        self.error(platform="cli")
        self.error(platform="cron")
        self.assertEqual(self.store.load(), {})

    def test_any_messaging_platform_by_default(self):
        self.error(platform="telegram")
        self.assertEqual(len(self.store.load()), 1)

    def test_platforms_setting_restricts(self):
        cfg = {"platforms": ["telegram"]}
        self.p.s = resume.Settings(lambda k, d=None: cfg.get(k, d))
        self.error(platform="discord")
        self.assertEqual(self.store.load(), {})
        self.error(platform="telegram")
        self.assertEqual(len(self.store.load()), 1)

    def test_settings_parse_and_fallback(self):
        cfg = {"max_wait_hours": "2", "margin_seconds": "oops", "platforms": "discord, slack",
               "notices": False}
        st = resume.Settings(lambda k, d=None: cfg.get(k, d))
        self.assertEqual(st.max_wait, 7200)
        self.assertEqual(st.margin, resume.MARGIN_SECONDS)
        self.assertEqual(st.platforms, {"discord", "slack"})
        self.assertFalse(st.notices)

    def test_max_wait_setting(self):
        cfg = {"max_wait_hours": 1}
        self.p.s = resume.Settings(lambda k, d=None: cfg.get(k, d))
        self.error()  # 7380s > 1h
        self.assertEqual(self.store.load(), {})

    def test_notices_off(self):
        p = resume.ResumePlugin(lambda *a, **k: True, self.store, True,
                                notify=lambda k, t: self.notices.append(t),
                                settings=resume.Settings(lambda k, d=None: False if k == "notices" else d))
        p.on_api_error(session_id="S1", platform="discord", provider="x", status_code=429,
                       reason="rate_limit", error={"message": CODEX_TEXT})
        self.assertEqual(self.notices, [])
        self.assertEqual(len(self.store.load()), 1)

    def test_non_gateway_process_ignored(self):
        self.p.gateway = False
        self.error()
        self.assertEqual(self.store.load(), {})

    def test_non_rate_limit_ignored(self):
        self.error(status_code=500, reason="server_error")
        self.assertEqual(self.store.load(), {})

    def test_unknown_session_not_parked(self):
        self.error(session_id="NOPE")
        self.assertEqual(self.store.load(), {})

    def test_parked_notice_once_per_reset(self):
        self.error()
        self.error()
        self.assertEqual(len(self.notices), 1)
        self.assertTrue(self.notices[0][1].startswith("⏸️"))

    def test_parked_notice_again_when_reset_moves(self):
        self.error()
        self.error(error={"message": "'resets_in_seconds': 9000"})
        self.assertEqual(len(self.notices), 2)

    def test_lookup_target(self):
        self.assertEqual(resume.lookup_target("agent:main:discord:group:1:2"), "discord:1")
        self.assertEqual(resume.lookup_target("agent:main:discord:thread:7"), "discord:5:7")
        self.assertIsNone(resume.lookup_target("missing"))

    def test_success_cancels(self):
        self.error()
        self.p.on_api_success(session_id="S1")
        self.assertEqual(self.store.load(), {})

    def test_classify_hook_returns_none(self):
        self.assertIsNone(self.p.on_classify(provider="x", error_body={"resets_in_seconds": 5}))


class TickTests(Base):
    def test_due_entry_injected_once(self):
        self.store.arm("K", "S1", time.time() - 1, "test")
        self.p.tick()
        self.p.tick()
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.injected[0][1], "K")
        self.assertTrue(self.injected[0][0].startswith(resume.RESUME_MESSAGE))
        self.assertEqual(self.notices, [])
        self.assertEqual(self.store.load()['K']['status'], 'submitted')

    def test_future_entry_waits(self):
        self.store.arm("K", "S1", time.time() + 100, "test")
        self.p.tick()
        self.assertEqual(self.injected, [])

    def test_failed_inject_retries_then_retains_visible_failure(self):
        self.inject_ok = False
        wake = time.time() - 1
        self.store.arm("K", "S1", wake, "test")
        self.p.tick()
        self.assertIn("K", self.store.load())
        self.p.tick(now=wake + resume.GIVE_UP_AFTER_SECONDS + 1)
        self.assertEqual(self.store.load()['K']['status'], 'blocked')
        self.assertEqual(len(self.notices), 1)
        count = len(self.injected)
        self.p.tick(now=wake + resume.GIVE_UP_AFTER_SECONDS + 100)
        self.assertEqual(len(self.injected), count)
        self.assertEqual(len(self.notices), 1)

    def receipt(self, key='K', session_id='S1', content=None, **changes):
        event = SimpleNamespace(text=content or self.injected[-1][0], internal=True, source=None,
            metadata={'hermes_plugin_id': 'ratelimit-resume', 'hermes_plugin_injection': True,
                      'gateway_session_key': key, 'gateway_session_id': session_id})
        for name, value in changes.items(): setattr(event, name, value)
        return self.p.on_gateway_dispatch(event=event)

    def test_gateway_queued_then_unroutable_retries_after_restart_without_success_notice(self):
        now = time.time()
        self.store.arm('K', 'S1', now - 1, 'rate_limit')
        self.p.tick(now=now)  # True = accepted by host, then async routing fails.
        self.assertIn('K', self.store.load())
        self.assertEqual(self.notices, [])
        again = resume.ResumePlugin(self.p.inject, resume.Store(self.store.path), True,
                                    notify=lambda k, t: self.notices.append((k, t)))
        again.tick(now=now + 1)
        self.assertEqual(len(self.injected), 1)
        again.tick(now=now + resume.ACK_WAIT_SECONDS + 1)
        self.assertEqual(len(self.injected), 2)
        self.assertEqual(self.notices, [])
        self.assertIn('K', self.store.load())

    def test_actual_dispatch_then_api_success_confirms_and_notifies_once(self):
        now = time.time()
        self.store.arm('K', 'S1', now - 1, 'rate_limit')
        self.p.tick(now=now)
        self.assertIsNone(self.receipt())
        self.assertEqual(self.store.load()['K']['status'], 'dispatched')
        self.assertEqual(self.notices, [])
        self.p.tick(now=now + resume.ACK_WAIT_SECONDS + 1)
        self.assertEqual(len(self.injected), 1, 'do not inject again while a received turn runs')
        self.p.on_turn_start(session_id='S1', user_message=self.injected[-1][0], turn_id='R1', task_id='T1')
        self.p.on_api_success(session_id='S1', turn_id='R1', task_id='T1')
        self.p.on_api_success(session_id='S1', turn_id='R1', task_id='T1')
        self.assertEqual(self.store.load(), {})
        self.assertEqual(self.notices, [('K', resume.RESUMED_NOTICE)])

    def test_turn_start_is_the_receipt_when_the_host_skips_dispatch_hooks_for_internal_events(self):
        # Real gateway (run_inbound._hm_admit_event): internal events return before pre_gateway_dispatch.
        now = time.time()
        self.store.arm('K', 'S1', now - 1, 'rate_limit')
        self.p.tick(now=now)
        self.assertEqual(self.store.load()['K']['status'], 'submitted')
        self.p.on_turn_start(session_id='S1', user_message=self.injected[-1][0], turn_id='R1', task_id='T1')
        self.assertEqual(self.store.load()['K']['status'], 'running')
        self.p.tick(now=now + resume.ACK_WAIT_SECONDS + 1)
        self.assertEqual(len(self.injected), 1, 'a running resumed turn must not get a duplicate continue')
        self.p.on_api_success(session_id='S1', turn_id='R1', task_id='T1')
        self.assertEqual(self.store.load(), {})
        self.assertEqual(self.notices, [('K', resume.RESUMED_NOTICE)])

    def test_ingress_receipt_does_not_misattribute_another_active_turns_api_response(self):
        self.store.arm('K', 'S1', time.time() - 1, 'rate_limit')
        self.p.tick()
        self.receipt()
        self.p.on_api_success(session_id='S1', turn_id='OLD', task_id='OLD_TASK')
        self.assertIn('K', self.store.load())
        self.assertEqual(self.notices, [])
        self.p.on_turn_start(session_id='S1', user_message=self.injected[-1][0], turn_id='NEW', task_id='NEW_TASK')
        self.p.on_api_success(session_id='S1', turn_id='OLD', task_id='OLD_TASK')
        self.assertIn('K', self.store.load())
        self.p.on_api_success(session_id='S1', turn_id='NEW', task_id='NEW_TASK')
        self.assertEqual(self.notices, [('K', resume.RESUMED_NOTICE)])

    def test_test_slot_survives_prior_turns_api_success_until_its_own_turn_runs(self):
        self.p._test_key = 'agent:main:discord:group:1:2'
        self.p.cmd_test()
        self.p.on_api_success(session_id='S1', turn_id='OLD')
        self.assertIn(self.p._test_key + resume.TEST_SUFFIX, self.store.load())

    def test_human_success_cancels_pending_without_claiming_auto_resume(self):
        self.store.arm('K', 'S1', time.time() - 1, 'rate_limit')
        self.p.tick()
        old = self.injected[-1][0]
        self.p.on_api_success(session_id='S1')
        self.assertEqual(self.store.load(), {})
        self.assertEqual(self.notices, [])
        self.assertEqual(self.receipt(content=old)['action'], 'skip')

    def test_rearmed_record_rejects_stale_queued_continuation(self):
        self.store.arm('K', 'S1', time.time() - 1, 'rate_limit')
        self.p.tick()
        old = self.injected[-1][0]
        self.store.arm('K', 'S1', time.time() + 300, 'rate_limit')
        self.assertEqual(self.receipt(content=old)['action'], 'skip')
        self.assertNotEqual(self.store.load()['K'].get('status'), 'dispatched')

    def test_receipt_must_be_our_internal_event_with_matching_session(self):
        self.store.arm('K', 'S1', time.time() - 1, 'rate_limit')
        self.p.tick()
        self.receipt(internal=False)
        self.assertEqual(self.store.load()['K']['status'], 'submitted')
        self.receipt(session_id='OTHER')
        self.assertEqual(self.store.load()['K']['status'], 'submitted')
        self.p.on_api_success(session_id='OTHER')
        self.assertIn('K', self.store.load())

    def test_inject_may_synchronously_acknowledge_without_lock_deadlock(self):
        import threading
        self.store.arm('K', 'S1', time.time() - 1, 'rate_limit')
        def inject(content, **kwargs):
            self.receipt(content=content)
            self.p.on_turn_start(session_id='S1', user_message=content, turn_id='R1', task_id='T1')
            self.p.on_api_success(session_id='S1', turn_id='R1', task_id='T1')
            return True
        self.p.inject = inject
        worker = threading.Thread(target=self.p.tick, daemon=True)
        worker.start(); worker.join(timeout=1)
        self.assertFalse(worker.is_alive(), 'never hold the state lock across host callbacks')
        self.assertEqual(self.store.load(), {})

    def test_short_limit_after_dispatch_rearms_existing_pending_request(self):
        key = 'agent:main:discord:group:1:2'
        self.store.arm(key, 'S1', time.time() - 1, 'rate_limit')
        self.p.tick()
        self.receipt(key=key)
        before = self.store.load()[key]['attempt_id']
        self.error(error={'message': "'resets_in_seconds': 30"})
        value = self.store.load()[key]
        self.assertEqual(value['status'], 'waiting')
        self.assertNotEqual(value['attempt_id'], before)
        self.assertGreater(value['wake_at'], time.time())

    def test_stalled_dispatch_is_retained_as_blocked_not_reinjected_forever(self):
        now = time.time()
        self.store.arm('K', 'S1', now - 1, 'rate_limit')
        self.p.tick(now=now)
        self.receipt()
        self.p.tick(now=now + resume.GIVE_UP_AFTER_SECONDS + 1)
        self.assertEqual(self.store.load()['K']['status'], 'blocked')
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.notices, [('K', resume.BLOCKED_NOTICE)])

    def test_late_reconnection_gets_a_full_confirmation_window_after_receipt(self):
        now = time.time()
        self.store.arm('K', 'S1', now - resume.GIVE_UP_AFTER_SECONDS + 1, 'rate_limit')
        self.p.tick(now=now)
        self.receipt()
        self.p.tick(now=now + resume.ACK_WAIT_SECONDS + 1)
        self.assertEqual(self.store.load()['K']['status'], 'dispatched')
        self.assertEqual(self.notices, [])

    def test_state_survives_new_instance(self):
        self.error()
        again = resume.ResumePlugin(lambda *a, **k: True, resume.Store(resume.state_path()), True)
        self.assertEqual(len(again.store.load()), 1)


class CommandTests(Base):
    def test_test_command_uses_dispatch_key(self):
        class Src: pass
        class Ev:
            text = "/ratelimit-test"
            source = Src()
        class SS:
            def _generate_session_key(self, source):
                return "agent:main:discord:group:1:2"
        self.assertIsNone(self.p.on_gateway_dispatch(event=Ev(), session_store=SS()))
        msg = self.p.cmd_test("")
        self.assertIn("Test armed", msg)
        self.assertEqual(self.store.load()['agent:main:discord:group:1:2#test']['session_id'], 'S1')

    def test_test_does_not_replace_real_resume(self):
        self.error()
        self.p._test_key = "agent:main:discord:group:1:2"
        self.p.cmd_test("")
        data = self.store.load()
        self.assertEqual(data["agent:main:discord:group:1:2"]["reason"], "rate_limit")
        data["agent:main:discord:group:1:2#test"]["wake_at"] = time.time() - 1
        self.store.save(data)
        self.p.tick()
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.injected[0][1], "agent:main:discord:group:1:2")
        self.assertIn("agent:main:discord:group:1:2", self.store.load())

    def test_status_and_clear(self):
        self.assertIn("No pending", self.p.cmd_status(""))
        self.error()
        self.assertIn("agent:main:discord:group:1:2", self.p.cmd_status(""))
        self.assertIn("Cleared 1", self.p.cmd_status("clear"))


if __name__ == "__main__":
    unittest.main()
