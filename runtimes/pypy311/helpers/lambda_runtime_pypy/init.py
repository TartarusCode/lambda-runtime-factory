"""Init hook support for Provisioned Concurrency and warm-up flows."""

import importlib
import inspect
import os
from typing import Any, Callable, List

Hook = Callable[..., Any]
REGISTERED_INIT_HOOKS: List[Hook] = []


def register_init_hook(function: Hook) -> Hook:
    """Decorator for registering runtime init hooks in user code."""
    REGISTERED_INIT_HOOKS.append(function)
    return function


def _load_hook(reference: str) -> Hook:
    module_name, function_name = reference.rsplit(".", 1)
    module = importlib.import_module(module_name)
    hook = getattr(module, function_name)
    if not callable(hook):
        raise TypeError(f"Configured init hook '{reference}' is not callable")
    return hook


def _call_hook(hook: Hook, handler: Hook) -> None:
    signature = inspect.signature(hook)
    kwargs = {}

    if "handler" in signature.parameters:
        kwargs["handler"] = handler
    if "environment" in signature.parameters:
        kwargs["environment"] = dict(os.environ)

    hook(**kwargs)


def run_configured_init_hooks(handler: Hook) -> None:
    """Run hooks registered in code and via PYPY_RUNTIME_INIT_HOOKS."""
    hook_references = [
        reference.strip()
        for reference in os.getenv("PYPY_RUNTIME_INIT_HOOKS", "").split(",")
        if reference.strip()
    ]

    hooks = list(REGISTERED_INIT_HOOKS)
    hooks.extend(_load_hook(reference) for reference in hook_references)

    for hook in hooks:
        _call_hook(hook, handler)
