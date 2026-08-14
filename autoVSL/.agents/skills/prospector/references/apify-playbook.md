# Apify Scraping Playbook

How prospector gets raw data. Principle: Apify actors change and get deprecated — at run time, search the Apify store (apify.com/store) for the current best-maintained actor per source (sort by users + recent updates), rather than relying on hardcoded actor names. The API pattern is stable even when actors aren't.

## API pattern (stable)

```bash
# Run an actor synchronously and get dataset items in one call
curl -s -X POST \
  "https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items?token=$APIFY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{...actor input...}'

# Or async for big jobs: POST /runs, poll /runs/{runId}, then GET /datasets/{datasetId}/items
```

In Claude Code, prefer the Apify MCP server if configured (exposes actors as tools); otherwise use the REST pattern above via bash. Store `APIFY_TOKEN` in env, never in outputs or committed files. CLI wrapper: `scripts/apify_run.sh <ACTOR_ID> <input.json|-> [output.json]` — saves items to `research/raw/` by default.

## Source → data → extraction map

### 1. Meta Ad Library (the crown jewel)
- **Actor type to search:** "Facebook Ad Library scraper"
- **Query by:** competitor page name/ID, or keyword search per country
- **Capture per ad:** ad ID/snapshot URL, start date (→ longevity!), active status, creative text, media URLs, number of near-identical variants, platforms, CTA type
- **Extraction:** longevity_days = today − start_date; variant clusters (same concept, different hooks) = scaling signal. Transcribe video hooks (first 3s) for the hook bank.
- **Winning-signal note:** Meta shows NO views/engagement on US ads — longevity + variant count IS the winner metric (advertisers only keep feeding what converts). Bonus proxy: ads also delivered in the EU carry real reach numbers (EU transparency law); prefer actors that capture `eu_total_reach` when available.
- Manual fallback: facebook.com/ads/library (public, no login).

### 2. TikTok Creative Center + TikTok scraping
- **Actor types:** "TikTok scraper" (profiles/hashtags/keywords), plus manual Creative Center Top Ads (ads.tiktok.com/business/creativecenter) for ranked winning ads by region/industry with CTR-tier data
- **Capture:** video URL, views/likes/shares/comments velocity, hook transcript, on-screen text, sound used
- **Extraction:** comments are dual-value — engagement signal AND VoC mine ("where do I buy", "does it work for X" = awareness gaps)

### 2b. TikTok Shop (affiliate sales intelligence)
- **Actor type:** "TikTok Shop scraper" (product listings, sales counts, creator videos per product)
- **Why it's special:** sales-ranked videos are CONVERSION proof, not attention proof — a video with high attributed sales is a proven selling structure regardless of view count
- **Capture:** product sales volume, top affiliate videos per product, creator handles, video hooks (first 3s), price points
- **Extraction:** treat top-sellers' videos as Mode 3 winners → hook bank with evidence "tiktok_shop_sales_rank"; affiliate creator styles → spokesperson/format intel for the editor
- Upgrade path: Kalodata (external SaaS, ~$50/mo) is the purpose-built TikTok Shop analytics tool (sales-ranked videos/creators/products) if scraped data proves valuable

### 3. Amazon reviews (competitor products)
- **Actor type:** "Amazon reviews scraper"
- **Input:** competitor ASINs; pull 1-star and 5-star in separate runs
- **Capture:** rating, verbatim text, date, verified flag, helpful votes (weight by votes)
- **Extraction:** run VoC schema (frameworks §3); helpful-vote-weighted phrases first

### 4. Reddit
- **Actor type:** "Reddit scraper"
- **Input:** subreddits + keyword searches ("[category] doesn't work", "alternatives to [competitor]", "[pain point]")
- **Extraction:** failed-solution histories, objection scripts, unguarded identity language

### 5. YouTube
- **Actor types:** "YouTube scraper" + "YouTube comments scraper"
- **Targets:** competitor VSLs/reviews, category education videos
- **Extraction:** comments → VoC; competitor VSL transcripts → full teardowns (frameworks §7)

### 6. Instagram / competitor organic
- **Actor type:** "Instagram scraper"
- **Capture:** top-performing organic posts (engagement outliers = organic-validated angles), story highlights structure, comment VoC

### 7. Competitor sites & funnels
- **Actor type:** "Website content crawler" (or web_fetch for single pages)
- **Capture:** landing page headlines/subheads (their best-tested copy), offer construction, upsell flow, review widgets (more VoC), PDP claim language
- Watch for: advertorials/pre-sell pages linked from ads — these reveal the full angle articulation

### 8. Google Ads Transparency Center
- adstransparency.google.com — search advertisers; scrape or fetch. Search/YouTube ad copy reveals keyword-level angles.

### 9. Review platforms (Trustpilot, Judge.me widgets, etc.)
- **Actor type:** "Trustpilot scraper" or site-specific
- Same VoC extraction as Amazon.

## Per-run hygiene

- Save raw scrape JSON to `research/raw/[source]-[target]-[date].json` before any processing — reprocessable later as frameworks evolve.
- Every processed insight carries its source URL — no orphan claims.
- Respect rate limits and each platform's ToS; use residential proxies via Apify settings when actors recommend it; never scrape behind logins with user credentials.
- Budget note: run counts cost money — for census jobs, scrape metadata broadly, then media/transcripts only for the longevity-filtered shortlist.

## Monitoring loops (Mode 4 support)

Set up recurring scrapes (Apify schedules or cron in the agent environment):
- Weekly: competitor ad library diff — new ads = new tests they're running; disappeared ads = killed creative (both are signal)
- Weekly: TikTok Creative Center top-ads refresh for the category
- Monthly: fresh review pull on top 3 competitors (new complaints = shifting sophistication)
Diff outputs against the previous run; only deltas go to the report + banks.
