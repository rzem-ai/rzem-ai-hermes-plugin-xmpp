# hermes-plugin-xmpp

XMPP gateway plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Hermes already speaks Telegram, Discord, Slack, IRC, and friends through
its built-in gateway adapters. This plugin gives Hermes a first-class XMPP
adapter so you can talk to your agent from any XMPP client
(Conversations, Gajim, Dino, Snikket, Movim, ...) over both 1:1 chats and
MUC group rooms.

## Features

- **1:1 chats** — DM the bot's JID from any XMPP client.
- **MUC group rooms (XEP-0045)** — join named rooms; the bot only responds
  when addressed (`hermes-bot: ...`).
- **Message Carbons (XEP-0280)** — the bot stays in sync with the user's
  other clients; outgoing carbon-sent stanzas are suppressed so the bot
  never echoes its own replies back as user input.
- **Message Archive Management (XEP-0313)** — on startup the adapter
  pulls the server-side archive since its last-seen cursor so messages
  sent while the gateway was offline are picked up. Stanzas older than
  `XMPP_MAM_REPLAY_GRACE_SECONDS` (default 300s) are ingested silently;
  fresher ones get a live reply.
- **HTTP File Upload (XEP-0363)** for image / file delivery.
- **Chat States (XEP-0085)** for typing indicators.
- **Stanza dedup** keyed by `<stanza-id>` (XEP-0359), `<origin-id>`, or a
  `(from, id, timestamp)` tuple, so carbons + MAM never double-deliver.
- **Reconnect with exponential backoff** (2/4/8/16/32s).
- **Standalone cron sender** so Hermes can deliver scheduled
  notifications even when the long-running gateway is not the sending
  process.

## Install

### As a drop-in plugin (recommended)

```bash
hermes plugins install https://github.com/rzem-ai/hermes-plugin-xmpp.git --no-enable
```

That clones into `$HERMES_HOME/plugins/xmpp-platform/` and prompts for the
required env vars. Equivalent manual install:

```bash
git clone https://github.com/rzem-ai/hermes-plugin-xmpp.git \
    "$HERMES_HOME/plugins/xmpp-platform"
```

Then install the package + its native deps (`slixmpp`, `aiohttp`) into the
Hermes venv so the inner `hermes_plugin_xmpp` module is importable:

```bash
uv pip install --python <hermes-venv>/bin/python -e \
  "$HERMES_HOME/plugins/xmpp-platform"
```

For a default `scripts/install.sh` install the venv is
`~/.hermes/bin/venv`. On a split install (`--dir <install-dir>
--hermes-home <data-dir>`) it lives at `<install-dir>/venv`.

Finally enable it:

```bash
hermes plugins enable xmpp-platform
```

### As a pip package

```bash
pip install hermes-plugin-xmpp
```

`pip install` alone is not sufficient for a Hermes-internal install —
Hermes discovers platform plugins from `$HERMES_HOME/plugins/<dir>/`
via its dir scanner, not via the `hermes.platforms` entry-point group
(which is currently informational). Use the drop-in path above unless
you're embedding `hermes_plugin_xmpp` as a library in some other host
that does consume the `hermes.platforms` group.

## Discovery contract

Hermes's directory scanner (`hermes_cli/plugins.py:_load_directory_module`)
imports `$HERMES_HOME/plugins/xmpp-platform/__init__.py` as
`hermes_plugins.xmpp_platform` and calls its top-level `register(ctx)`.
The `ctx` is a `PluginContext` whose `register_platform(...)` method
takes named keyword args and wires an entry into
`gateway.platform_registry`.

This repo's top-level `__init__.py` is a thin shim: it calls the inner
`hermes_plugin_xmpp.adapter.register()` (which returns a descriptor
dict for forward-compat with other plugin hosts), translates the dict
into `register_platform()` kwargs, and adds the Hermes-side metadata
(`required_env`, `allowed_users_env`, `allow_all_env`,
`cron_deliver_env_var`, `install_hint`, `emoji`) that the inner
descriptor doesn't surface.

If you're embedding the adapter in a non-Hermes host, you can keep
calling `hermes_plugin_xmpp.adapter.register()` directly and consume
the dict yourself.

## Configure

The interactive route is the simplest:

```bash
hermes gateway setup
# pick XMPP, answer the prompts
```

Or set the env vars yourself in `~/.hermes/.env`:

```
XMPP_JID=hermes@chat.rzem.ai
XMPP_PASSWORD=replace-me
XMPP_ALLOWED_JIDS=me@chat.rzem.ai,other@chat.rzem.ai
XMPP_MUC_ROOMS=team@conference.chat.rzem.ai
XMPP_MUC_NICKNAME=hermes-bot
```

Or, equivalently, drop a block into `~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    xmpp:
      extra:
        jid: hermes@chat.rzem.ai
        password: replace-me
        allowed_jids:
          - me@chat.rzem.ai
        muc_rooms:
          - team@conference.chat.rzem.ai
        muc_nickname: hermes-bot
```

Env values always win over YAML values; YAML wins over defaults.

### Full env-var reference

| Variable | Required? | Default | Purpose |
|---|---|---|---|
| `XMPP_JID` | ✅ | — | Bot Jabber ID (`user@domain`). |
| `XMPP_PASSWORD` | ✅ | — | Bot password. |
| `XMPP_SERVER` | | (JID domain) | Override the XMPP host for DNS / proxying. |
| `XMPP_PORT` | | `5222` | Server port. |
| `XMPP_USE_TLS` | | `true` | Require STARTTLS. |
| `XMPP_RESOURCE` | | `hermes` | XMPP resource portion. |
| `XMPP_MUC_ROOMS` | | (none) | Comma-separated room JIDs to join. |
| `XMPP_MUC_NICKNAME` | | `hermes-bot` | Nick used inside rooms. |
| `XMPP_ALLOWED_JIDS` | | (empty) | Comma-separated bare JIDs allowed to DM the bot. |
| `XMPP_ALLOW_ALL_USERS` | | `false` | Bypass `XMPP_ALLOWED_JIDS` (dev only). |
| `XMPP_HOME_JID` | | first allowed JID | Cron / notification recipient. |
| `XMPP_MAM_REPLAY_GRACE_SECONDS` | | `300` | MAM stanzas newer than this trigger live replies. |
| `XMPP_MAM_CATCHUP_LIMIT` | | `200` | Hard cap on stanzas replayed per catch-up. |

## Run

```bash
hermes gateway
```

DM the bot from your XMPP client, or in any joined MUC say:

```
hermes-bot: how's deployment looking?
```

## State

The MAM catch-up cursor is persisted at
`~/.hermes/state/xmpp_last_seen.json`, keyed by bot JID and scope
(`dm` or `muc:<room_jid>`). Delete the file to force a full archive
pull on next start.

## Server: ejabberd on `chat.rzem.ai`

This plugin is developed against the **ejabberd** instance at
`chat.rzem.ai`. Any reasonably modern ejabberd (≥ 21.x) will do —
the plugin only needs:

- standard c2s on TCP 5222 with STARTTLS,
- `mod_mam` enabled for the user account (Message Archive Management),
- `mod_carboncopy` enabled (Message Carbons),
- `mod_muc` for any MUC rooms you want the bot to join,
- `mod_http_upload` (advertised on its own service JID, typically
  `upload.chat.rzem.ai`) if you want image / file delivery.

All of these are on by default in stock ejabberd builds.

### Provisioning the bot account

On the ejabberd host (or anywhere `ejabberdctl` is on `$PATH`):

```bash
ejabberdctl register hermes chat.rzem.ai 'replace-me'
ejabberdctl register me     chat.rzem.ai 'mepw'
```

If you prefer the web admin, the equivalent lives at
`https://chat.rzem.ai/admin/server/chat.rzem.ai/users/`.

### Pointing Hermes at it

```
XMPP_JID=hermes@chat.rzem.ai
XMPP_PASSWORD=replace-me
XMPP_SERVER=chat.rzem.ai
XMPP_ALLOWED_JIDS=me@chat.rzem.ai
```

`XMPP_SERVER` is optional — slixmpp will resolve `_xmpp-client._tcp`
SRV records under `chat.rzem.ai` and find the host automatically.
Set it only if you need to bypass DNS.

Run `hermes gateway`, then DM `hermes@chat.rzem.ai` from a Gajim /
Dino / Conversations session logged in as `me@chat.rzem.ai`.

## Not yet supported

- **OMEMO end-to-end encryption (XEP-0384).** Substantial extra work;
  PRs welcome.
- **In-band voice transcription.** Voice notes are uploaded and handed
  to Hermes's central STT layer for transcription, but the adapter
  itself does no audio decoding.

## License

MIT — see [LICENSE](./LICENSE).
