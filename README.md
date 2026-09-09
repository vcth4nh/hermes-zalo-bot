# hermes-zalo-bot

Official **Zalo Bot API** platform plugin for [Hermes Agent](https://hermes-agent.nousresearch.com/).
Chat with your Hermes agent from Zalo direct messages and groups.

- Uses the official Bot API (`bot-api.zaloplatforms.com`), not a personal-account library, so there is no account-ban risk.
- Long polling by default (no public URL needed) or a webhook behind your reverse proxy.
- Direct messages and groups (Zalo delivers group messages only when the bot is @mentioned or replied to).
- Text in both directions, incoming photos passed to the agent, typing indicator, cron delivery.
- Zero extra dependencies for polling (`httpx` ships with Hermes). Webhook mode needs `aiohttp`.

## Requirements

- Hermes Agent 0.21 or newer.
- A bot token from [Zalo Bot Creator](https://bot.zapps.me/docs/create-bot/) (format `numeric_id:secret`).

## Install

```bash
git clone https://github.com/vcth4nh/zalo-hermes-brigde.git
cp -r zalo-hermes-brigde/zalo ~/.hermes/plugins/zalo      # or: ln -s "$PWD/zalo-hermes-brigde/zalo" ~/.hermes/plugins/zalo
```

Put the token and your Zalo user id in `~/.hermes/.env`:

```bash
ZALO_BOT_TOKEN=1234567890:xxxxxxxxxxxxxxxx
ZALO_ALLOWED_USERS=<your Zalo user id>
```

Enable the plugin and the platform, then start the gateway:

```bash
hermes plugins enable zalo            # answer "n" to the tool-override question
hermes config set platforms.zalo.enabled true
hermes gateway run                    # or: hermes gateway restart
```

`hermes status` should now show `Zalo Bot ✓ configured (plugin)`. Send your bot a message in Zalo.

### Find your Zalo user id

Set `ZALO_ALLOW_ALL_USERS=true` for a first run, send the bot a message, and read the gateway log line
`[zalo] message.text.received from user <id> in dm <id>`. Put that id in `ZALO_ALLOWED_USERS` and remove the allow-all flag.
Alternatively run `ZALO_BOT_TOKEN=... python scripts/smoke_echo.py` without Hermes; it prints the sender id.

## Configuration

Every setting is an env var or a key under `platforms.zalo.extra` in `~/.hermes/config.yaml`. The config file value wins.

| Env var | `extra` key | Default | Meaning |
|---|---|---|---|
| `ZALO_BOT_TOKEN` | `token` | required | bot token |
| `ZALO_MODE` | `mode` | `polling` | `polling` or `webhook` |
| `ZALO_POLL_TIMEOUT` | `poll_timeout` | `30` | getUpdates long-poll seconds |
| `ZALO_WEBHOOK_URL` | `webhook_url` | | public HTTPS URL Zalo calls (path included) |
| `ZALO_WEBHOOK_SECRET` | `webhook_secret` | | 8-256 chars; Zalo echoes it in `X-Bot-Api-Secret-Token` |
| `ZALO_WEBHOOK_HOST` | `webhook_host` | `127.0.0.1` | local bind address |
| `ZALO_WEBHOOK_PORT` | `webhook_port` | `8790` | local bind port |
| `ZALO_ALLOWED_GROUPS` | `allowed_groups` | any group | comma-separated group chat ids |
| `ZALO_ALLOWED_USERS` | | | comma-separated user ids (checked by Hermes core) |
| `ZALO_ALLOW_ALL_USERS` | | `false` | answer anyone (development only) |
| `ZALO_HOME_CHANNEL` | | | chat id for cron and `hermes send` delivery |
| `ZALO_HOME_CHANNEL_NAME` | | | display name for that chat |

`config.yaml` example:

```yaml
plugins:
  enabled: [zalo]
platforms:
  zalo:
    enabled: true
    extra:
      mode: polling
      allowed_groups: "1234567890123456789"
```

### Webhook mode

Zalo needs a public HTTPS URL and rejects localhost and private addresses. Run a reverse proxy (Caddy, nginx) that forwards the path to the local port, then:

```bash
pip install aiohttp                     # inside the Hermes environment
ZALO_MODE=webhook
ZALO_WEBHOOK_URL=https://bot.example.com/zalo/webhook
ZALO_WEBHOOK_SECRET=<8-256 random characters>
```

The adapter binds `ZALO_WEBHOOK_HOST:ZALO_WEBHOOK_PORT`, serves `GET /health`, and registers the URL with Zalo on every start. Polling and webhook are mutually exclusive on Zalo's side; polling mode deletes a registered webhook automatically.

### Groups

Add the bot to a group through its share link (the group leader confirms). Zalo delivers only messages that @mention the bot or reply to one of its messages, so the adapter needs no mention gate. `ZALO_ALLOWED_GROUPS` limits which groups are answered; senders still need to pass `ZALO_ALLOWED_USERS` or the allow-all flag. Group support is marked "internal beta" by Zalo.

### Limits

- Messages are capped at 2000 characters; Hermes splits longer replies.
- Replies are sent with `parse_mode: markdown` and retried as plain text if Zalo rejects the markup.
- Incoming photos are downloaded so the agent can see them. Voice messages and stickers arrive as a placeholder note.
- Outgoing images need a public URL (`sendPhoto`). Local files cannot be sent.
- No threads, edits, reactions, or reply quoting: the Bot API has no such parameters.

## Development

Tests for the API layer run anywhere; the adapter tests need a Hermes checkout:

```bash
git clone --depth 1 https://github.com/NousResearch/hermes-agent.git ../hermes-agent
(cd ../hermes-agent && uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -e . pytest aiohttp pyyaml)
HERMES_AGENT_SRC=$PWD/../hermes-agent ../hermes-agent/.venv/bin/python -m pytest -q
```

Live check without Hermes (waits for one DM and echoes it back):

```bash
ZALO_BOT_TOKEN=... python scripts/smoke_echo.py --wait 60
```

## License

MIT.
