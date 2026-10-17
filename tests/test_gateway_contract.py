"""Optional installed-Hermes contract test: no network, model or production state.

Run with HERMES_GATEWAY_SRC pointing at a trusted Hermes checkout, using that
installation's dependency environment. The gateway scheduler/PluginContext are
real; only its transport and API-success event are deterministic fixtures.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import resume

CORE = os.environ.get('HERMES_GATEWAY_SRC')


@unittest.skipUnless(CORE, 'trusted installed Hermes source/dependencies not selected')
class GatewayContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_queue_rejection_then_reconnection_and_receipt(self):
        sys.path.insert(0, CORE)
        from gateway.config import Platform
        from gateway.session import SessionSource
        from gateway.run_plugin_injection import GatewayPluginInjectionMixin
        from hermes_cli.plugins import PluginManager, PluginContext, PluginManifest
        key, sid = 'agent:main:telegram:dm:42', 'fixture-session'
        origin = SessionSource(platform=Platform.TELEGRAM, chat_id='42', chat_type='dm', user_id='42')
        entry = SimpleNamespace(origin=origin, session_id=sid)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'HERMES_HOME': temp}):
            home = Path(temp)
            (home / 'config.yaml').write_text(json.dumps({'plugins': {'entries': {'ratelimit-resume': {'allow_gateway_injection': True}}}}))
            class Host(GatewayPluginInjectionMixin):
                def __init__(self):
                    self._gateway_loop = asyncio.get_running_loop()
                    self._running, self._draining, self.available = True, False, False
                    self._background_tasks = set()
                    self.config = SimpleNamespace(multiplex_profiles=False)
                    self.async_session_store = SimpleNamespace(lookup_by_session_key=AsyncMock(return_value=entry))
                    self.adapter = SimpleNamespace(handle_message=self.receive)
                    self.events = []
                def _restored_source(self, value): return value.origin
                def _is_user_authorized_for_source(self, source, **kwargs): return True
                def _delivery_adapter_for(self, source): return self.adapter if self.available else None
                async def receive(self, event):
                    self.events.append(event)
                    self_outer.assertTrue(event.internal)
                    self_outer.assertFalse(event.allow_gateway_control)
                    self_outer.assertIsNone(plugin.on_gateway_dispatch(event=event))
                    plugin.on_turn_start(session_id=sid, user_message=event.text, turn_id='fixture-turn', task_id='fixture-task')
                    plugin.on_api_success(session_id=sid, turn_id='fixture-turn', task_id='fixture-task')
            self_outer = self
            host = Host()
            manager = PluginManager()
            manager.set_gateway_message_injector(host, host._schedule_plugin_message_injection)
            context = PluginContext(PluginManifest(name='ratelimit-resume', key='ratelimit-resume', source='user'), manager)
            notices = []
            store = resume.Store(home / 'state/pending.json')
            plugin = resume.ResumePlugin(context.inject_message, store, True, notify=lambda k, text: notices.append((k, text)))
            now = time.time()
            store.arm(key, sid, now - 1, 'rate_limit')
            try:
                plugin.tick(now=now)
                tasks = list(host._background_tasks)
                self.assertTrue(tasks, 'actual SDK scheduling must return a task')
                outcomes = await asyncio.gather(*tasks)
                await asyncio.sleep(0)
                self.assertEqual(outcomes, [False], 'the real host asynchronously rejects a missing adapter')
                self.assertEqual(notices, [])
                self.assertEqual(store.load()[key]['status'], 'submitted')
                host.available = True
                plugin.tick(now=now + resume.ACK_WAIT_SECONDS + 1)
                outcomes = await asyncio.gather(*list(host._background_tasks))
                await asyncio.sleep(0)
                self.assertEqual(outcomes, [True])
                self.assertEqual(len(host.events), 1)
                self.assertEqual(store.load(), {})
                self.assertEqual(notices, [(key, resume.RESUMED_NOTICE)])
            finally:
                manager.clear_gateway_message_injector(host)
