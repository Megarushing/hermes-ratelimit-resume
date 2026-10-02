"""Hermes plugin entry point: wires resume.ResumePlugin into public plugin hooks."""
from __future__ import annotations

try:
    from .resume import ResumePlugin, Settings, Store, is_gateway_process, state_path
except ImportError:  # plain unittest import
    from resume import ResumePlugin, Settings, Store, is_gateway_process, state_path

_PLUGIN = None


def register(ctx) -> None:
    global _PLUGIN
    _PLUGIN = ResumePlugin(ctx.inject_message, Store(state_path()), is_gateway_process(),
                           settings=Settings(ctx.get_config))
    ctx.register_hook("transform_api_error_classification", _PLUGIN.on_classify)
    ctx.register_hook("api_request_error", _PLUGIN.on_api_error)
    ctx.register_hook("post_api_request", _PLUGIN.on_api_success)
    ctx.register_hook("pre_gateway_dispatch", _PLUGIN.on_gateway_dispatch)
    ctx.register_command("ratelimit-test", _PLUGIN.cmd_test,
                         description="Fake a usage-limit reset in 60s to test auto-continue")
    ctx.register_command("ratelimit-status", _PLUGIN.cmd_status,
                         description="List pending rate-limit resumes ('clear' drops them)",
                         args_hint="[clear]")
    _PLUGIN.start()
