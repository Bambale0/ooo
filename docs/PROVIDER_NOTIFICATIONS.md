# Provider operational notifications

ArgoLink can POST its account/quota notifications to
`https://api.нейроныч.online/webhook/res/` (ASCII host:
`api.xn--e1aikcel5c5a.online`). The path without the trailing slash also accepts
POST directly. These notifications go only to the configured Telegram admin;
they do not change partner balances, model availability or generation status.

Set `PROVIDER_NOTIFICATION_WEBHOOK_SECRET` in the host secret environment and
put the same value in the provider's webhook credential field. The receiver
requires `Authorization: Bearer <secret>`; admin and partner API credentials do
not substitute for it. Never place the secret in the URL, repository, or examples.
`ADMIN_TELEGRAM_ID` and `TELEGRAM_BOT_TOKEN` must also be configured. Missing
receiver/delivery configuration returns 503; missing or invalid credentials return
401. TLS is terminated by the existing production proxy.

The provider's JSON field names are English, including `timestamp`:

```json
{
  "type": "quota_exceed",
  "title": "Provider balance warning",
  "content": "Your remaining credit is {{value}}",
  "values": ["$0.99"],
  "timestamp": 1739950503
}
```

`values` is optional. Placeholders are replaced once, in order; values are never
interpreted as templates, code or Telegram markup. Already-rendered content is
accepted. Unknown notification types are retained, subject to a bounded identifier
format. Notification text is truncated with an ellipsis to fit Telegram's message
limit. Bodies over 64 KiB return 413; malformed JSON returns 400 and invalid fields
return 422 without reflecting the submitted body.

The field names match the supplied provider contract and the upstream
[New API webhook payload](https://github.com/QuantumNous/new-api/blob/1a4166d8e8ba9802d2ca56fe8ecf0ed5404e80d5/service/webhook.go).
Authentication follows the supplied ArgoLink Bearer contract; upstream New API
deployments can use different authentication and are not implicitly trusted here.

Successful acceptance returns HTTP 200 with `{"success": true}` only after the
existing PostgreSQL `bot_notifications` outbox transaction commits. A SHA-256
digest of the normalized event fields is the unique deduplication key, so concurrent
or repeated delivery of the same event queues one notification. A changed timestamp
is a distinct event because the provider has supplied no stable delivery ID.

The existing Telegram worker sends plain text and retries delivery failures using
its persisted backoff. Its delivery guarantee is at least once: a crash after
Telegram accepts a message but before `sent_at` commits can repeat the message.
Logs contain provider, bounded event type, event hash and duplicate status only;
raw content, values and credentials are not logged. No schema migration is needed.
