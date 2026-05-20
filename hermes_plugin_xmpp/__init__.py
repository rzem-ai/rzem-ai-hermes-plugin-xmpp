"""Hermes XMPP gateway plugin."""

from .adapter import (
    PLATFORM,
    PLATFORM_HINT,
    XmppAdapter,
    adapter_factory,
    interactive_setup,
    is_configured,
    load_config,
    validate,
)

__all__ = [
    "PLATFORM",
    "PLATFORM_HINT",
    "XmppAdapter",
    "adapter_factory",
    "interactive_setup",
    "is_configured",
    "load_config",
    "validate",
]
__version__ = "0.2.0"
