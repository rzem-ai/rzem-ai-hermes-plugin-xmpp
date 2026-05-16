"""Hermes XMPP gateway plugin."""

from .adapter import XmppAdapter, register

__all__ = ["XmppAdapter", "register"]
__version__ = "0.1.0"
