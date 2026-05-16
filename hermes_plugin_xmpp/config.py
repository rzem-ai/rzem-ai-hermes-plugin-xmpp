"""Configuration resolution for the Hermes XMPP plugin.

Resolution order, matching the IRC plugin convention:
1. Process environment variables (e.g. ``XMPP_JID``).
2. ``gateway.platforms.xmpp.extra`` block in ``~/.hermes/config.yaml``
   (or whatever dict the gateway passes to the factory).
3. Built-in defaults.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .jid_utils import normalize_jid_set, parse_jid

log = logging.getLogger(__name__)


DEFAULT_PORT = 5222
DEFAULT_RESOURCE = "hermes"
DEFAULT_MUC_NICKNAME = "hermes-bot"
DEFAULT_MAM_REPLAY_GRACE_SECONDS = 300
DEFAULT_MAM_CATCHUP_LIMIT = 200


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n", ""):
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("XMPP plugin: bad int %r, using default %d", value, default)
        return default


def _csv(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


@dataclass
class XmppConfig:
    jid: str
    password: str
    server: str = ""
    port: int = DEFAULT_PORT
    use_tls: bool = True
    resource: str = DEFAULT_RESOURCE
    muc_rooms: list[str] = field(default_factory=list)
    muc_nickname: str = DEFAULT_MUC_NICKNAME
    allowed_jids: set[str] = field(default_factory=set)
    allow_all_users: bool = False
    home_jid: str = ""
    mam_replay_grace_seconds: int = DEFAULT_MAM_REPLAY_GRACE_SECONDS
    mam_catchup_limit: int = DEFAULT_MAM_CATCHUP_LIMIT

    @property
    def bare_jid(self) -> str:
        return parse_jid(self.jid).bare

    @property
    def domain(self) -> str:
        return parse_jid(self.jid).domain

    def server_or_domain(self) -> str:
        return self.server or self.domain


def _coalesce(env_key: str, yaml_key: str, yaml: Mapping[str, Any]) -> Any:
    env_val = os.environ.get(env_key)
    if env_val not in (None, ""):
        return env_val
    if yaml_key in yaml and yaml[yaml_key] not in (None, ""):
        return yaml[yaml_key]
    return None


def load_config(extra: Mapping[str, Any] | None = None) -> XmppConfig:
    """Build an XmppConfig from environment + the gateway-provided extra dict.

    ``extra`` is the ``gateway.platforms.xmpp.extra`` block from
    ``~/.hermes/config.yaml`` (or {} when none).
    """
    yaml: Mapping[str, Any] = extra or {}

    jid_raw = _coalesce("XMPP_JID", "jid", yaml)
    password_raw = _coalesce("XMPP_PASSWORD", "password", yaml)
    if not jid_raw:
        raise ValueError("XMPP plugin: XMPP_JID is required")
    if not password_raw:
        raise ValueError("XMPP plugin: XMPP_PASSWORD is required")

    parsed = parse_jid(str(jid_raw))

    allowed = normalize_jid_set(
        _csv(_coalesce("XMPP_ALLOWED_JIDS", "allowed_jids", yaml))
    )
    home_raw = _coalesce("XMPP_HOME_JID", "home_jid", yaml)
    if home_raw:
        home_jid = parse_jid(str(home_raw)).bare
    elif allowed:
        home_jid = next(iter(sorted(allowed)))
    else:
        home_jid = ""

    return XmppConfig(
        jid=parsed.full,
        password=str(password_raw),
        server=str(_coalesce("XMPP_SERVER", "server", yaml) or ""),
        port=_as_int(_coalesce("XMPP_PORT", "port", yaml), DEFAULT_PORT),
        use_tls=_as_bool(_coalesce("XMPP_USE_TLS", "use_tls", yaml), True),
        resource=str(_coalesce("XMPP_RESOURCE", "resource", yaml) or DEFAULT_RESOURCE),
        muc_rooms=_csv(_coalesce("XMPP_MUC_ROOMS", "muc_rooms", yaml)),
        muc_nickname=str(
            _coalesce("XMPP_MUC_NICKNAME", "muc_nickname", yaml) or DEFAULT_MUC_NICKNAME
        ),
        allowed_jids=allowed,
        allow_all_users=_as_bool(
            _coalesce("XMPP_ALLOW_ALL_USERS", "allow_all_users", yaml), False
        ),
        home_jid=home_jid,
        mam_replay_grace_seconds=_as_int(
            _coalesce("XMPP_MAM_REPLAY_GRACE_SECONDS", "mam_replay_grace_seconds", yaml),
            DEFAULT_MAM_REPLAY_GRACE_SECONDS,
        ),
        mam_catchup_limit=_as_int(
            _coalesce("XMPP_MAM_CATCHUP_LIMIT", "mam_catchup_limit", yaml),
            DEFAULT_MAM_CATCHUP_LIMIT,
        ),
    )


def is_configured(extra: Mapping[str, Any] | None = None) -> bool:
    """Cheap check for the gateway's ``is_configured`` hook."""
    yaml = extra or {}
    jid = os.environ.get("XMPP_JID") or yaml.get("jid")
    password = os.environ.get("XMPP_PASSWORD") or yaml.get("password")
    return bool(jid and password)


def validate(extra: Mapping[str, Any] | None = None) -> list[str]:
    """Return a list of human-readable validation errors (empty == OK)."""
    errors: list[str] = []
    try:
        cfg = load_config(extra)
    except ValueError as exc:
        return [str(exc)]
    try:
        parse_jid(cfg.jid)
    except ValueError as exc:
        errors.append(f"Invalid XMPP_JID: {exc}")
    for room in cfg.muc_rooms:
        try:
            parse_jid(room)
        except ValueError as exc:
            errors.append(f"Invalid MUC room {room!r}: {exc}")
    if cfg.allow_all_users:
        log.warning(
            "XMPP plugin: XMPP_ALLOW_ALL_USERS=true — anyone who can reach %s "
            "will be able to talk to the agent. Use only for development.",
            cfg.bare_jid,
        )
    return errors
