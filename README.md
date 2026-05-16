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

### As a drop-in plugin

```bash
git clone https://github.com/rzem-ai/hermes-plugin-xmpp.git \
    ~/.hermes/plugins/xmpp
pip install slixmpp PyYAML aiohttp
```

### As a pip package

```bash
pip install hermes-plugin-xmpp
```

When installed via pip, the `hermes.platforms` entry point makes the
plugin discoverable automatically — no copying required.

## Configure

The interactive route is the simplest:

```bash
hermes gateway setup
# pick XMPP, answer the prompts
```

Or set the env vars yourself in `~/.hermes/.env`:

```
XMPP_JID=hermes@example.com
XMPP_PASSWORD=replace-me
XMPP_ALLOWED_JIDS=me@example.com,other@example.com
XMPP_MUC_ROOMS=team@conference.example.com
XMPP_MUC_NICKNAME=hermes-bot
```

Or, equivalently, drop a block into `~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    xmpp:
      extra:
        jid: hermes@example.com
        password: replace-me
        allowed_jids:
          - me@example.com
        muc_rooms:
          - team@conference.example.com
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

## Local testing

This plugin is developed against **jabberd** (jabberd2). A throwaway
container is enough to exercise everything end-to-end:

```bash
docker run --rm -d -p 5222:5222 -p 5269:5269 --name jabberd jabberd/jabberd2
```

Create the two test accounts. The exact path depends on whether your
jabberd image is using SQLite, MySQL, or PostgreSQL storage; for the
default SQLite build:

```bash
docker exec -it jabberd jabberd2-adduser hermes localhost hermespw
docker exec -it jabberd jabberd2-adduser me      localhost mepw
```

If your build doesn't ship `jabberd2-adduser`, enable in-band
registration (XEP-0077) in `c2s.xml` and register from any XMPP
client, or insert rows directly into the `authreg` table. Either
route is fine — the plugin only cares that the JIDs exist and accept
auth.

Point Hermes at it:

```
XMPP_JID=hermes@localhost
XMPP_PASSWORD=hermespw
XMPP_SERVER=localhost
XMPP_ALLOWED_JIDS=me@localhost
```

Run `hermes gateway`, then DM `hermes@localhost` from a Gajim / Dino
session logged in as `me@localhost`.

## Not yet supported

- **OMEMO end-to-end encryption (XEP-0384).** Substantial extra work;
  PRs welcome.
- **In-band voice transcription.** Voice notes are uploaded and handed
  to Hermes's central STT layer for transcription, but the adapter
  itself does no audio decoding.

## License

MIT — see [LICENSE](./LICENSE).
