# Integrations: widget, Slack, Telegram and Discord

Open **Agents → Integrations** to put a published agent where people already work. Creating one needs the `curate` or `administer` grant and an agent you can edit. Each integration has its own setup panel with the exact steps, the embed code or webhook URL, and test buttons.

## Who an integration answers as

An answer is only as safe as the identity behind it, so every integration states one:

| Mode | Available on | Behaviour |
|---|---|---|
| **Fixed** | all | Answers as the person who created it. Everyone using it sees what that person can see, and never more. Good for public help desks over public documents. |
| **Signed-in users** | widget | The host site's server signs a short-lived token for the logged-in user (`user_id.expiry.HMAC`), and the widget answers as that person. Approvals work in the widget. |
| **Mapped people** | Slack, Telegram, Discord | Chat-app user ids are mapped to Smriti users, and unmapped people are refused. Setting the map needs `administer`. |

## Safety checks for answers that leave Smriti

- **Personal data is masked** in replies sent to Slack, Telegram and Discord. This is on by default, because those services store every message. The reply says that values were hidden.
- **Classified answers stay home.** An answer citing a document above the integration's limit (default `internal`) is replaced by a link back to Smriti, and the event is audited.
- **Approvals are buttons.** Only the person who asked can press them, and the action runs as the integration's identity.
- **Inbound requests are authenticated.**
  - Slack: signing secret and a 5-minute timestamp window.
  - Telegram: a secret token set with the webhook.
  - Discord: an Ed25519 signature.
  - Widget: an allow-list of site origins and a per-client rate limit.
- **Tokens are encrypted at rest** and never returned by the API.

The origin check stops other websites from embedding your widget. It does not stop someone who copies the URL into a script. A **fixed** widget is therefore public to anyone who can reach Smriti, so use **signed-in users** for anything that isn't public.

## Website widget

```html
<script src="https://smriti.example/widget.js" data-smriti-widget="wg_…"
        data-host="https://smriti.example" async></script>
```

**Attributes:**
- `data-user-token`: the signed token, for signed widgets.
- `data-position="left"`: put the bubble on the left.
- `data-open="true"`: open the panel on page load.

**JavaScript:** `SmritiWidget.open()`, `.close()`, `.ask(text)` and `.setUserToken(token)`.

**Behaviour:**
- It renders inside a shadow root, so the page's CSS can't break it.
- It streams answers, with citations and a list of sources.
- It keeps the conversation for the browser tab.

Signing a user on your server:

```python
import hashlib, hmac, time
exp = int(time.time()) + 3600
sig = hmac.new(SIGNING_KEY.encode(), f"{user_id}.{exp}".encode(), hashlib.sha256).hexdigest()
token = f"{user_id}.{exp}.{sig}"
```

## Slack, Telegram, Discord

For each bot, Smriti must be reachable from the internet at `SMRITI_PUBLIC_URL`. On a closed network, use the widget instead.

- **Slack:** Events API. It answers when mentioned in a channel, and in direct messages. Replies go in the thread, and a thread continues one conversation. It needs `app_mentions:read`, `chat:write` and `im:history`.
- **Telegram:** create a bot with @BotFather, paste the token, then press **Connect webhook**. Each chat continues one conversation for 6 hours. `/ask question` also works.
- **Discord:** paste the Application ID, Public Key and Bot token, set the Interactions Endpoint URL, then press **Register /ask**.

## API

| Method | Path | |
|---|---|---|
| `GET` | `/api/channels` | your integrations and the kinds available |
| `POST` | `/api/channels` | create: `kind`, `agent_id`, `identity`, `mask_pii`, `max_class`, then per kind `origins`, `title`, `greeting`, `color` / `bot_token`, `signing_secret` / `public_key`, `application_id`, `users` |
| `PUT` | `/api/channels/{id}` | change settings, turn it on or off |
| `DELETE` | `/api/channels/{id}` | delete it |
| `GET` | `/api/channels/{id}/signing-key` | a signed widget's key (creator or admin only, audited) |
| `POST` | `/api/channels/{id}/register` | Telegram webhook or Discord `/ask` command |
| `POST` | `/api/widget/{id}/config`, `/chat`, `/approve/{run}` | the widget's own endpoints (CORS, origin-checked) |
| `POST` | `/api/channels/{id}/slack`, `/telegram`, `/discord` | webhooks the chat apps call |
