# Visitor analytics (PostHog)

TickerDesk has PostHog analytics built in, but it is **off** until you add a
PostHog key. While it's off, no script loads and nothing is sent.

It answers one question: which channels bring visitors, and how many of them
go on to sign up, request a report, and pay.

## Turn it on (about 10 minutes)

1. **Create a PostHog project.** Sign up at <https://us.posthog.com/signup>
   and pick **US Cloud**. The free tier covers 1M events a month. Name the
   project "TickerDesk".
2. **Two privacy settings** in PostHog: Settings → Project:
   - **IP data capture**: turn on "Discard client IP data".
   - **Web analytics / Autocapture**: leave autocapture and session replay
     off. The site already turns them off in code; this keeps it consistent.
3. **Copy the Project API key.** Settings → Project → "Project API key".
   It starts with `phc_`. This key is public by design: it can only send
   events, never read your data.
4. **Website:** open `docs/analytics.js` and paste the key between the quotes
   on this line, then commit and push (a normal PR):
   ```js
   var TD_POSTHOG_KEY = "phc_...";
   ```
   That is the only code change. The site picks it up on the next deploy.
5. **Worker (for `subscribed` / `cancelled`):** pick one of these.
   - **From GitHub (no terminal):** add a repo secret named `POSTHOG_KEY`
     with the same `phc_` key (Settings → Secrets and variables → Actions),
     then run the **R2 Probe** workflow with only **sync_posthog_secret**
     ticked.
   - **From a terminal:** in `cloudflare-worker/`, run
     `npx wrangler secret put POSTHOG_KEY` and paste the key.

   Takes effect at once, no redeploy needed. Don't also add `POSTHOG_KEY`
   under `[vars]` in `wrangler.toml`: a var and a secret with the same name
   clash.
6. **Check it works:** open <https://tickerdesk.io/?utm_source=test> in a
   normal browser window (not one with Do Not Track switched on). Within a
   minute, PostHog → Activity should show a `page_view` with
   `utm_source = test`.

If your PostHog project is on **EU Cloud**, also change the two hosts at the
top of `docs/analytics.js` and in the Content-Security-Policy in
`docs/index.html` to `eu.i.posthog.com` / `eu-assets.i.posthog.com`, and set
a worker var `POSTHOG_HOST = "https://eu.i.posthog.com"`.

To turn it off again, empty the key in `docs/analytics.js` and delete the
worker secret (`npx wrangler secret delete POSTHOG_KEY`).

## The events

| Event | When | Properties |
|---|---|---|
| `page_view` | Any page opens (the app, landing pages, legal pages) | `path`, `referrer_domain` (the site that sent the visitor, blank if none), `utm_source`, `utm_medium`, `utm_campaign` |
| `brief_signup` | A Daily Brief form is accepted by the server (2xx only) | `source`: `footer`, `welcome`, or `lp_<page>` |
| `account_created` | First sign-in of a new account (profile under 30 min old) | none |
| `report_requested` | The worker accepted a research report request | `report` (`adhoc`) |
| `upgrade_clicked` | An upgrade button on the pricing tab or the upgrade pop-up | `plan`, `location` (`pricing_tab` / `modal`), `signed_in` |
| `checkout_started` | Just before the browser goes to Stripe | `plan`, `location` |
| `subscribed` | Stripe confirms a subscription checkout (sent by the worker) | `plan`, `source: stripe_webhook` |
| `cancelled` | Stripe ends a subscription (sent by the worker) | `plan`, `source: stripe_webhook` |

Signed-in visitors are identified with their Supabase user id, the same id
the worker uses for `subscribed` / `cancelled`, so the browser and payment
events join up in one person.

## What it does not collect

- No emails, names or anything typed into a form. Event properties that look
  like an email are dropped before sending.
- No cookies. A signed-out visitor gets a random id in the tab's
  `sessionStorage`, which the browser deletes when the tab closes. So a
  visitor who comes back tomorrow without signing in counts as new.
- No session recording, no automatic click capture, no heatmaps, no surveys.
- Sign-in tokens and Stripe session ids are stripped from page addresses;
  only `utm_*` tags are kept.
- Browsers sending Do Not Track or Global Privacy Control are not tracked at
  all.

The privacy policy (`docs/privacy.html`) describes this. Update it if you
add events.

## The funnel to build

First make one combined "signed up" step: **Data management → Actions → New
action**, name it `Signed up`, and add two match groups: event
`brief_signup` and event `account_created`.

Then **Product analytics → New insight → Funnel**:

1. `page_view`
2. the `Signed up` action
3. `report_requested`
4. `upgrade_clicked`
5. `checkout_started`
6. `subscribed`

Settings: conversion window **14 days**, counted by **unique persons**.
Break it down by `utm_source` (and a second copy by `referrer_domain`) to see
which channels convert, not just which ones bring visitors.

Because signed-out visitors are forgotten when the tab closes, the steps from
a first visit to a sign-up are only joined up within one visit. From sign-in
onward everything joins by user id, across devices and days.

**Other useful insights:**

- **Visitors by source:** Trends → `page_view`, unique users, broken down by
  `utm_source`, weekly. This is the "visitors by source" line on the Monday
  scorecard.
- **Sign-ups by source:** Trends → `brief_signup`, broken down by `source`.
- **Paid users:** Trends → `subscribed` minus `cancelled`, weekly.

Note: PostHog's ready-made "Web analytics" tab reads PostHog's own
`$pageview` event, which this site does not send. Use the insights above
instead.

## Where the code is

- `docs/analytics.js`: the key, the PostHog loader and its privacy
  settings, `tdTrack(event, props)`, `tdIdentify(userId)`. Every page loads
  it with `<script src="/analytics.js" defer>`.
- `docs/index.html`: the app's `tdTrack` calls (search for `window.tdTrack`).
- `landing_pages.py`: the landing-page sign-up form's `brief_signup`.
- `cloudflare-worker/worker.js`: `posthogStripeEvent()`, called once at the
  end of `handleStripeWebhook`, just before the success response. It is
  fire-and-forget (`ctx.waitUntil`), gives up after 5 seconds and catches
  every error, so it can never break or slow down a payment.
- Tests: `analytics_js_test.py`, `analytics_worker_test.py`.
