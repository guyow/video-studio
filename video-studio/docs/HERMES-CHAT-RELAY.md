# Hermes ↔ Studio chat relay (over Tailscale)

> For the Hermes agent on the VPS (`debian`, tailnet). Implements the founder's
> "talk with Hermes inside the studio" chat by RELAYING the sg-hermes Telegram
> group — no Telethon/my.telegram.org needed on the Windows side.

## Why relay (and not a bot bridge on the studio side)

Telegram bots cannot see other bots' messages, so the studio can never read
your (Hermes's) replies with a bot token. You, on the VPS, already hold the
bot that lives in sg-hermes: you see the founder's Telegram messages and you
author your own replies — so you can mirror both into the studio chat.

## Studio endpoints (base: `http://100.84.71.123:5180` on the tailnet)

Authenticate every call with `Authorization: Bearer <agent_token>`.
Fetch the token once (PIN-authenticated session, same login you already use):
`GET /api/agent-token` → `{agent_token, endpoints}`. Never write it to the repo.

- `GET /api/chat?since=<id>` — drain chat messages (`id` = cursor; persist it).
  Founder messages you must relay have `author: "founder"`, `source: "studio"`.
- `POST /api/chat {text}` — with the Bearer token this appears as **Hermes**
  in the studio chat (bubble on the left).
- `GET /api/events?since=<id>` — every chat message also lands here as a
  `chat` event, so your existing event drain can trigger the relay quickly.
- `POST /api/job/<id>/stages/<key>/note {note}` — stage annotations.
- `POST /api/drafts/ugc {fields, note}` — pre-fill the UGC Factory form
  (fields: template, script, action, voice, voice_id, emotion, bg, model,
  seconds, lipsync, image, product). The founder reviews + one-click applies;
  nothing auto-runs.
- `POST /api/plans` / `GET /api/plans?status=` / `POST /api/plans/<id>/status`
  — the approval loop. Paid runs with the Bearer token REQUIRE an approved
  `plan_id` (the studio 403s otherwise).

## The relay loop (add to the watcher, ~2 min cadence or faster)

1. **Studio → Telegram**: drain `GET /api/chat?since=<cursor>`. For each new
   `author=founder, source=studio` message: post it to the sg-hermes group via
   your bot as `👤 Guy (studio): <text>` AND treat it as founder input to your
   chat loop (same as a Telegram message from him).
2. **Your replies → studio**: whenever you answer (in Telegram or not), also
   `POST /api/chat {text}` so the studio bubbles it.
3. **Founder's Telegram messages → studio**: when the founder writes in the
   group from his phone, mirror it with `POST /api/chat` — but the studio
   marks Bearer-token posts as Hermes. Prefix such mirrors clearly, e.g.
   `"(from Telegram) <text>"`, or skip mirroring them if noisy.

Dedupe rule: never relay a message that itself arrived from the other side
(track the ids you've already forwarded — the `id` field is monotonic).

## Studio UI behavior

The chat panels (Agent page + UGC Factory) show a status pill:
- `● via Hermes relay` (green) as soon as a Hermes-authored message arrived in
  the last 6 h — that's you doing the relay above.
- `Telegram off` (amber) until then.
So the founder sees the connection is live purely from you replying.
