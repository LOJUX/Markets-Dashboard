# Global Markets Now

A single-page global markets dashboard hosted free on GitHub Pages. GitHub Actions rebuilds it **every 15 minutes on weekdays, 06:00–23:00 Dublin time**. Each run fetches the latest closes, live intraday quotes, policy rates, the economic calendar and headlines.

The page answers "what's going on in markets?" without swamping you:

1. **Next up**: a countdown strip of the next big releases, with a red TODAY badge on release day.
2. **What's going on**: a short briefing covering stocks across regions, yields and the dollar, commodities and the VIX, what is driving things, the top story and what's next.
3. **Key markets**: a fixed panel of 15 headline numbers (stocks, bonds, currencies, commodities and risk) with the latest move, the past week and a 3-month trend. Underneath it are the Fed, ECB, BoE and BoJ policy rates, with each bank's last change and next meeting. The list is set in `key_markets` and `key_banks` in `config.yaml`.
4. **Out of the ordinary**: up to 5 moves that are large *for that market*, today or over the past week (see below). If nothing qualifies, it shows the closest.
5. **Just released**: rate decisions and data from the last 36 hours, with actual vs consensus, the surprise and the market reaction.
6. **Coming up**: the next 7 days of high-impact releases. Second-tier data (`minor: true` in `config.yaml`) only appears under Just released.
7. **All markets** (collapsed): every bond, FX pair, commodity and index, plus every central bank.
8. **Headlines**: the 6 most market-relevant stories of the last 24h, ranked by topic, by whether the story is market-wide or about a market-moving company, and by how many outlets carry it. Personal-finance and stock-tip pieces rank lower. At most 2 stories come from one outlet, 3 from one topic and 1 per big company.
9. **Data checks** (collapsed).

All times are shown in Europe/Dublin. Hover over or tap any number to see its source, its time (live quote or close) and the close the move is measured from.

---

## Setup (about 10 minutes)

### 1. Create the repository

```bash
git init -b main
```

```bash
git add . && git commit -m "Global markets dashboard"
```

Create an empty repository on GitHub (public, or private on a plan that includes Pages), then:

```bash
git remote add origin https://github.com/<you>/<repo>.git
```

```bash
git push -u origin main
```

### 2. Add the secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**.

| Secret | Required? | What it does |
|---|---|---|
| `FRED_API_KEY` | Recommended | Fed funds target from FRED, FRED fallback for US Treasury yields, a cross-check of US yields against H.15, and a cross-check of US release dates. Get a free key at <https://fred.stlouisfed.org/docs/api/api_key.html>. Without it, the Fed rate comes from the NY Fed API and those checks show "not run". |
| `ANTHROPIC_API_KEY` | Optional | If set, Claude (`claude-opus-5-5`) writes the "What's going on" briefing from the numbers and headlines on the page. If it isn't set, or the call fails, a rule-based summary is used. Server-side refusal fallback is turned on (`fallbacks: "default"`). |

### 3. Let Actions push commits

Go to **Settings → Actions → General → Workflow permissions** and choose **Read and write permissions**. The workflows commit the updated history back to the repo.

### 4. Enable GitHub Pages

Go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**.

### 5. Run it once

Go to **Actions → Update dashboard (every 15 min) → Run workflow**. When it finishes, the page URL appears on the run summary (usually `https://<you>.github.io/<repo>/`).

After that, [`update.yml`](.github/workflows/update.yml) runs on its own every 15 minutes, Monday to Friday, 06:00–23:00 Dublin. GitHub cron runs in UTC, so the schedule covers both summer and winter time, and the first step skips runs outside the Dublin window. A run takes about 1–2 minutes. It commits `data/` only when something final has changed: a new close, a new release figure or a new summary. Most runs deploy the page without committing anything.

Things to know:

- **Delays.** GitHub can start scheduled runs 5–20 minutes late when it is busy, so the page is usually 15–35 minutes old. The header always shows when it was built. If it is more than 45 minutes old inside the window, an amber banner says so. An open page reloads itself every 5 minutes.
- **Actions minutes.** Public repositories run Actions for free. A private repository on the free plan gets 2,000 minutes a month, and this schedule uses roughly 1,500–2,000. For a private repo, change the cron to `*/30 4-22 * * 1-5`, and change `refresh.every_minutes` in `config.yaml` to match.
- **Claude cost.** The summary is only regenerated when the set of unusual moves, new releases or rate changes differs from last time, or after an hour. That is at most about 17 calls a day.

---

## Adding or removing tickers: `config.yaml`

Everything is configured in [`config.yaml`](config.yaml). To add an index, append to `series`:

```yaml
  - {id: SMI, name: SMI, group: equities, region: Europe, close: "17:30 Europe/Zurich",
     sources: [{type: yahoo, symbol: "^SSMI"}]}
```

- `group`: `yields`, `fx`, `commodities` or `equities`. Yields are shown in % with changes in bp. Everything else shows changes in %.
- `sources`: tried in order until one returns data. Types: `yahoo`, `cnbc`, `fred`, `boe`, `bundesbank`, `mof`.
- `close`: when the daily session ends, in local exchange time. A bar dated today is ignored until then, so partial days never enter the history.

To remove a ticker, delete its line. Its old rows stay in `data/history.csv` but are no longer shown.

You can also edit the economic releases (`calendar.releases`, matched by regex against calendar titles), the reaction assets for each country, the news feeds and the news category keywords in the same file.

### Central banks: `central_banks.yaml`

[`central_banks.yaml`](central_banks.yaml) is the **only** place meeting dates live. The rates table and the calendar both read them from here. It also holds the rates for banks without a usable free API (BoJ, RBA, RBNZ, PBoC). When you check or update the file, update `last_reviewed`. The page shows a warning once it is more than 60 days old. Each bank's rate is also compared with its latest decision on the economic calendar, and the row is flagged if they disagree. Dates are currently filled in through December 2026 (January 2027 for the PBoC). Add 2027 dates once the banks publish them. A row says "add to YAML" when it runs out of dates.

---

## Data sources and their limits

| Data | Primary source | Fallback / cross-check | Limits |
|---|---|---|---|
| Indices, FX, futures, Bitcoin, US 10Y/30Y | Yahoo Finance via `yfinance`: daily closes plus 5-minute intraday bars | — | Unofficial API. It can lag or rate-limit, and European index closes sometimes appear a few hours late. Intraday bars are usually 15–45 minutes behind real time. Every live value is labelled with the time of its bar. |
| Live yields | CNBC quote API (batched) | — | Near real time. Used for the series whose history comes from CNBC. |
| US 2Y | CNBC daily bars | FRED `DGS2` | `^IRX` is the 13-week bill, not the 2Y, so it isn't used. FRED is a day behind. |
| DE/UK/FR/IT/IE/JP 10Y | CNBC daily bars (benchmark bonds, ~2y history) | Bundesbank (DE), BoE (UK), Japan MoF (JP) cross-checks | CNBC is unofficial, with no official free daily source for FR/IT/IE. The official DE/UK curves are fitted yields (Svensson / par), not benchmark bonds, so a few bp difference is normal. Their tolerance is 15bp. |
| Dutch TTF gas | Yahoo `TTF=F` | — | Yahoo returns only the latest bar, no history. History builds up one day at a time from the first run, so 1d/1w/1m changes show "n/a" until enough days exist. |
| Fed funds | FRED `DFEDTARU` | NY Fed EFFR API (no key) | Shows the upper bound of the target range. |
| ECB deposit rate | ECB Data Portal | — | Change date is the *effective* date (usually 6 days after the decision). |
| Bank Rate | Bank of England database | — | |
| SNB, BoC | SNB data portal, Bank of Canada Valet API | — | |
| BoJ, RBA, RBNZ, PBoC | `central_banks.yaml` (hand-maintained) | Calendar decision cross-check | The RBA's statistics site blocks automated downloads (HTTP 403). |
| New rate decisions | TradingView calendar (actual) | — | A decision is shown as soon as it appears on the calendar, usually within minutes of the announcement. If the official series or the YAML hasn't caught up yet, the rate is taken from the announcement and marked **announced**. Manually maintained banks also get a reminder to update the YAML. |
| Economic calendar | TradingView economic-calendar API: time, actual, consensus, previous | Forex Factory weekly JSON (time cross-check, fills a missing consensus); FRED release dates (US date cross-check, needs key) | Both calendars are unofficial, free endpoints. TradingView has no consensus for some releases, and Forex Factory covers this week only and rate-limits repeat requests. Missing consensus shows **n/a**. It is never estimated. |
| Irish HICP flash | CSO release calendar JSON + CSO press statement | — | Not on either commercial calendar. No free consensus exists, so it always shows n/a. |
| News | RSS: CNBC, FT, BBC, MarketWatch, Guardian, RTÉ, Fed, ECB, BoE | — | Headlines only. Deduplication is word overlap, so two stories about the same event in very different words can both appear. |

**Failure handling.** Each source is fetched on its own, and a broken one never stops the build. If today's fetch fails, the last stored value is shown with a **not refreshed** badge. If a series' last close is older than expected, it gets a **stale** badge. If there is no data at all, it shows **data unavailable** with the time of the last attempt. Every failure is listed under *Data checks* at the bottom of the page. Changes and sparklines only use history from the same source as the current value, so switching to a fallback never creates a fake jump.

**Live vs close.** History stores completed daily closes only. When an intraday quote belongs to a session newer than the last stored close, the page shows the quote and measures the move from that close. Otherwise it shows the close and the move from the close before it. The "As of" column says which: a time, or "close Thu".

**Unusual moves** are judged against each market's own normal. σ is the standard deviation of its daily moves over the past year. A move is flagged when today's move (since the last close) is beyond 2σ, or the past week's move is beyond 2σ of a normal week (daily σ × √5). There are no fixed % thresholds, so a jumpy market like Bitcoin or the VIX isn't flagged for an ordinary day, while a quiet market making an unusual move is. Headline markets get a small boost in the ranking. The settings are `sigma`, `week_sigma` and `max_shown` under `movers`.

**Market reactions** to a release are measured from the previous day's close: to that day's close once it is stored, otherwise to the latest intraday quote after the release, labelled with its time.

---

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
FRED_API_KEY=... .venv/bin/python build.py
```

The page is written to `site/index.html`. Open it in a browser, or serve it with `python3 -m http.server --directory site`.

Files in `data/` are committed on purpose:

- `history.csv`: daily closes for every series.
- `calendar_state.json`: release actuals and end-of-day reactions, kept for 3 days.
- `summary_cache.json`: the last Claude summary, reused while nothing material has changed.

If `data/history.csv` is deleted, the next run backfills about 1 year of history (around 30 seconds).
