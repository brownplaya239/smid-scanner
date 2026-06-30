# TickerDesk live-news ingestor

A tiny always-on worker that holds a websocket to **Alpaca's real-time news
stream** (Benzinga-sourced) and writes each headline into the Supabase
`news_live` table. Browsers subscribe to that table over **Supabase Realtime**,
so headlines hit the Live tape in ~1–2 seconds — the "live tape" feel that
30-second polling can't give.

```
Alpaca news WS  →  this ingestor  →  Supabase news_live (INSERT)
                                          ↓ Supabase Realtime (websocket fan-out)
                                     browsers → prepend + flash
```

The Cloudflare Worker can't hold a persistent socket cheaply, so this small
process is the only always-on piece. Supabase Realtime does the fan-out — no
relay server to write.

## 1. Create the table (once)

Open the Supabase SQL editor for the project and run [`schema.sql`](./schema.sql).
It creates `news_live`, enables RLS with public read, and adds the table to the
`supabase_realtime` publication.

## 2. Environment variables

| Var | Where to get it |
|-----|-----------------|
| `ALPACA_KEY` | Alpaca dashboard → API keys (the same account as the momentum engine works) |
| `ALPACA_SECRET` | Alpaca dashboard → API keys |
| `SUPABASE_URL` | `https://uaeojibmhxbwkhpvmjwy.supabase.co` |
| `SUPABASE_SERVICE_ROLE` | Supabase → Project settings → API → **service_role** key. SERVER-SIDE ONLY — never ship this to the browser or commit it. |

The free Alpaca news tier allows ~900 real-time headlines/day. If that cap
bites on a busy day, switch the source to Benzinga/Massive later (the ingestor
is the only thing that changes).

## 3. Deploy (pick one)

### Railway (easiest for a worker)
1. New project → Deploy from this repo, root directory `news-ingestor/`.
2. Railway auto-detects the Dockerfile.
3. Add the four env vars under **Variables**.
4. Deploy. Watch the logs for `authenticated → subscribing to all news`.

### Fly.io
```bash
cd news-ingestor
fly launch --no-deploy            # accept defaults; it's a worker (no HTTP)
fly secrets set ALPACA_KEY=... ALPACA_SECRET=... \
  SUPABASE_URL=https://uaeojibmhxbwkhpvmjwy.supabase.co \
  SUPABASE_SERVICE_ROLE=...
fly deploy
```

### Plain Docker (any always-on host)
```bash
docker build -t td-news-ingestor news-ingestor/
docker run -d --restart=always \
  -e ALPACA_KEY=... -e ALPACA_SECRET=... \
  -e SUPABASE_URL=https://uaeojibmhxbwkhpvmjwy.supabase.co \
  -e SUPABASE_SERVICE_ROLE=... \
  td-news-ingestor
```

### Local smoke test
```bash
cd news-ingestor && npm install
ALPACA_KEY=... ALPACA_SECRET=... \
SUPABASE_URL=https://uaeojibmhxbwkhpvmjwy.supabase.co \
SUPABASE_SERVICE_ROLE=... node index.mjs
```
You should see `→ <time> [TICKERS] <headline>` lines as news arrives, and rows
appear in the Supabase `news_live` table.

## 4. Verify the tape

With the ingestor running, open the site's News tab → Live tape. New headlines
should pop in at the top with a brief highlight within a second or two of the
ingestor logging them. (The frontend subscription degrades gracefully: if this
worker is down, the tape just falls back to the 30-second Polygon poll.)

## Notes
- Retention: the ingestor deletes rows older than 3 days hourly. RLS blocks anon
  writes; only the service role (used here) can insert/delete.
- Reconnect: on socket drop it reconnects with exponential backoff (1s → 30s).
- Dedupe: inserts use `Prefer: resolution=ignore-duplicates` on the article id,
  and the frontend additionally dedupes by headline against the Polygon feed.
