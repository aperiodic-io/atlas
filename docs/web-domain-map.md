# Web domain map

`atlas/data/web/domain_map.json` names, for each Atlas asset, its
official website domain and English Wikipedia article. Web ranking
datasets (Tranco, Cisco Umbrella, Majestic and CrUX ranks, Wikipedia pageviews)
join to Atlas through it. It ships with the package:

```python
from atlas.web import DOMAIN_MAP_PATH, load_domain_map

domain_map = load_domain_map()
```

or, without installing anything,
`https://raw.githubusercontent.com/aperiodic-io/atlas/main/atlas/data/web/domain_map.json`.

## Format

One JSON object, UTF-8, two-space indented, `schema_version` 1. A change that
breaks readers bumps `schema_version`; new optional fields may be added within
version 1, so readers should ignore fields they do not know.

| Top-level field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | integer | `1`. |
| `generated_at` | string | When the map was built, RFC 3339 UTC (`2026-10-20T04:30:00Z`). |
| `atlas_source` | string | The Atlas commit whose data the map was built from. |
| `entries` | array of objects | One per mapped asset, sorted by `ticker`; tickers are unique. |
| `unmapped` | object | Asset key → why it has no entry, sorted by key. For review; not needed to use the map. |

Example:

```json
{
  "schema_version": 1,
  "generated_at": "2026-10-20T04:30:00Z",
  "atlas_source": "<atlas commit>",
  "entries": [
    {
      "ticker": "AVAX",
      "asset_id": "AVAX",
      "asset_class": "crypto",
      "name": "Avalanche",
      "domain": "avalanche.com",
      "wikipedia": "Avalanche_(blockchain_platform)",
      "atlas_symbols": [
        "AVAX"
      ],
      "atlas_contracts": 308,
      "cmc_id": 5805,
      "method": "atlas_cmc_id",
      "verification": {
        "status": "redirect",
        "evidence": [
          "cmc_website",
          "homepage_redirect",
          "name_in_domain",
          "name_in_title",
          "wikipedia_article"
        ],
        "redirected_from": "avax.network",
        "checked": "2026-10-20"
      }
    }
  ],
  "unmapped": {"KEY": "selfkey.org: hijacked: spam page (slot88)"}
}
```

Entry fields (optional fields are absent rather than empty or null):

| Field | Type | Meaning |
| --- | --- | --- |
| `ticker` | string | The bare ticker people trade the asset under (`BONK`, `TSLA`), unique in the file. When several mapped assets share one, the primary keeps it and the others use a unique key of their own (see step 7). |
| `asset_id` | string, optional | The Atlas asset ID ([symbology](symbology.md)). Absent for a TradFi underlying Atlas mints no asset for (pre-IPO names, equities only listed on venues symbology does not cover yet) and for coins identified by the symbol fallback. |
| `asset_class` | string | `crypto`, `equity`, `pre_market`, `commodity`, `index` or `forex`. |
| `name` | string, optional | The asset's name. |
| `domain` | string, optional | Registrable domain (public suffix + one label) of the asset's site, normalised as described under [Host normalisation](#host-normalisation). |
| `wikipedia` | string, optional | Canonical English Wikipedia title, underscores for spaces. |
| `wikipedia_sitelinks` | object, optional | The same article in every other Wikipedia edition, `{lang: title}`: `lang` is the Wikipedia subdomain (`de`, `ja`, `zh-yue`, `be-tarask`; open editions only, from the Wikimedia sitematrix snapshot in `integrations/web/wikipedias.json`), the title as Wikidata gives it (spaces; replace them with `_` for URLs). Only on entries with an English article, and only Wikipedia editions (no Commons, Wikinews, Wikiquote…). |
| `atlas_symbols` | array of strings | Every Atlas `symbol` of the asset's contracts, for joining back to Atlas. |
| `atlas_contracts` | integer | Number of Atlas contracts on the asset, trading or ended. |
| `cmc_id`, `wikidata` | integer, string; optional | The CoinMarketCap ID or Wikidata item the entry came from. |
| `method` | string | Where the domain came from: `atlas_cmc_id` (the CMC website of the Atlas asset's CMC ID), `wikidata_ticker` (the company Wikidata lists under the ticker), `cmc_symbol_unique` / `cmc_symbol_dominant` (the symbol fallback, below) or `override`. |
| `verification` | object | `status` (`ok`, `redirect`, `flagged`), the `evidence` for the entry, the `flags` that need a human, `redirected_from` after a rebrand, and the day the homepage last answered (`checked`; absent for an entry without a domain). |

A domain belongs to one asset (step 6): only assets of one project (a rename,
a share class) publish the same domain, so a consumer can key rows by domain.

## Host normalisation

The `domain` of an entry is Atlas's own normalisation of a website, and
consumers that match ranking lists against this map must normalise hosts
identically (`integrations/web/domain.py`):

1. `normalize_host`: trim whitespace; drop the scheme (`…://`), path, query and
   fragment, userinfo (`…@`) and port; drop trailing dots; lower-case ASCII
   letters; drop one leading `www.`. The result must contain a dot, not start
   with one, have no empty label, and use only `a-z 0-9 . - _`; otherwise there
   is no host.
2. `registrable`: the host's public suffix plus one label, from the Public
   Suffix List with private suffixes included (each `*.github.io` site is its
   own domain). A bare public suffix has none.

| Input | Host | Domain |
| --- | --- | --- |
| `https://www.Tesla.com/en_us?x=1` | `tesla.com` | `tesla.com` |
| `http://user:pw@solana.com:8080/path` | `solana.com` | `solana.com` |
| `WWW.BINANCE.COM.` | `binance.com` | `binance.com` |
| `https://m.cricbuzz.com` | `m.cricbuzz.com` | `cricbuzz.com` |
| `app.uniswap.org` | `app.uniswap.org` | `uniswap.org` |
| `news.bbc.co.uk` | `news.bbc.co.uk` | `bbc.co.uk` |
| `foo.github.io` | `foo.github.io` | `foo.github.io` |
| `localhost`, `a..b`, `exa mple.com`, `.com` | none | none |

`tests/fixtures/web/psl_parity.tsv` lists the expected host and domain for
3,467 real website strings.

`unmapped` lists every asset of the universe that has no entry, with the reason.

## How it is built

`python -m integrations.web` (see `--help`):

1. **Universe.** Every snapshot row with a `base_asset_id` counts for that
   Atlas asset. A row symbology has not resolved (venues it does not cover yet)
   is attributed by its normalised symbol (`1000PEPE` → `PEPE`, `xyz:TSLA` →
   `TSLA`, Bitget's tokenised `rGS` → `GS`) to the registry asset with that ID, ticker or alias *of the row's own
   class*, never to one CMC added after the row stopped trading. A TradFi row no
   asset matches becomes an asset of its own; a crypto row no asset matches only
   enters through the symbol fallback, under its own key (`HON.CRYPTO` when a
   TradFi `HON` exists), so the two never overwrite each other. Only files named
   after an Atlas exchange definition are read.
2. **Sources.**
   * Crypto: the first website of CoinMarketCap's detail of the asset's CMC ID
     that is not on a blocked host. Tokenised stocks and ETFs, pre-IPO tokens and
     fan tokens are skipped: their site is the issuer's or the club's.
   * Equities: Wikidata's current listing of the ticker on NASDAQ, NYSE or NYSE
     American (1-5 letters), KRX (6 digits) or Tokyo (4 digits); the domain most
     of the company's official websites share; ETFs, dissolved companies and
     ambiguous tickers are skipped.
   * Crypto articles: Wikidata by CMC slug, else by website, only for the
     domain's primary asset and only if no other asset has that article
     (makerdao.com's article is DAI's, not MKR's).
   * Commodities, indices, currencies and names Wikidata's ticker query cannot
     reach (`TOYOTA`, Hong Kong codes) come from the overrides only.
3. **Symbol fallback.** A crypto asset Atlas could not identify (mostly
   delisted coins such as BUSD or Bitcoin Gold) is matched to CoinMarketCap by
   symbol only when one coin has it, or one coin ranked within 1,500 is at least
   five times better ranked than the next, and that coin was listed on CMC no
   later than two months after Atlas *first* saw the ticker (and before its last
   contract ended): GreenTrust (CMC 2021) is not the Golem `GNT` Binance listed
   in 2019. A first capture on a venue's first day or on a bulk-import day
   (Gate's data starts 2022-06-08) is not a listing date and does not count. Tokens only listed on Hyperliquid spot are never matched by symbol
   (HIP-1 tickers are free-for-all, and symbology already applies CMC's
   Hyperliquid listings). The domain is kept only if its homepage answered (or
   sits behind a bot wall on a domain that is the coin's name, such as
   looksrare.org) and names the coin, or CoinGecko lists the same site.
4. **Overrides** (`atlas/data/web/overrides.json`): blocked hosts,
   aliases for unresolved symbols, and forced fields per asset, each with a
   `reason`. A forced field wins over every source and over the homepage check;
   an absent field keeps the source's value, `null` forces none.
   `same_project_as` marks two assets as one project (MATIC and POL, OM and
   MANTRA) so both keep its domain. An override can also identify a coin the
   fallback cannot (Golem's `GNT`). `own_platform` lets the platform's own asset
   use its blocked host: PUMP on pump.fun, Alphabet on google.com, Meta on
   facebook.com. Consumer-brand overrides map a company whose corporate site
   is not where the public goes to its single flagship consumer domain
   (Booking Holdings → booking.com, JPMorgan Chase → chase.com), only when that
   brand clearly dominates its web presence.
5. **Verification.**
   * One GET of `https://<domain>/` per domain and run (then `http://`, then
     `www.`), five redirects at most (followed by hand, one request at a time
     per host, so many domains forwarding to one hosting or parking host queue),
     a 10 s read timeout, 256 KB of body read within 20 s, sixteen domains at a
     time.
   * A parking, for-sale or hosting-placeholder page unmaps the domain: parking
     phrases in the title, parking networks' markers in the body (a script
     sending visitors to `/lander`, Parity's and Gandi's landers), a redirect
     to a `ww<N>.` parking host, an untitled page under 200 bytes that does not
     forward by script. So does spam on an expired project domain: affiliate or
     adult titles (even ones carrying the project's name: jup.io's "Best AI
     Girlfriend Apps ... - Jupiter"), a title with slot, gambling or escort
     words that does not name the asset, or a redirect to another domain whose
     page has such words (benthedog.io, smolecoin.com).
   * A failed check keeps the previous verdict (title, redirect, parked) and
     flags `unreachable`; failures on two different days in a row unmap the
     domain, except for an identity that does not rest on the page (a Wikidata
     company, an override): eaton.com and caterpillar.com stall bots, so they
     stay, flagged. When more than 20% of a run's checks fail, the runner is the
     likelier problem and none of the failures is recorded.
   * A redirect to another registrable domain is a rebrand: the map follows it
     when the target page names the asset (and is not a blocked host) and
     records `redirected_from`; otherwise the domain stays, flagged
     `redirect_unverified`. A redirect to the same brand on another TLD
     (visa.com → visa.de) is a geo-redirect: the domain stays.
   * Identity evidence: the asset's name or ticker in the page title or the
     domain itself; CoinGecko's homepage for the coin (agreement is evidence,
     disagreement a flag); for equities, Atlas's name against Wikidata's.
     An entry without any is flagged `no_identity_evidence`.
   * Every Wikipedia title must exist. A redirect is replaced by its target
     only when the target is still about the asset: not when it names another
     entity ("Basic_Attention_Token" → the Brave browser, "Arweave" → Odysee)
     and not when it is another entry's article (USD1 → World_Liberty_Financial,
     WLFI's). A title an override forces is never rewritten.
   * Other-language articles: Wikidata's sitelinks of each English title
     (`wbgetentities`, 50 titles a query, `maxlag=5`, at most one request a
     second), cached and refreshed 200 titles a run.
6. **One domain, one asset.** A registrable domain is published for one asset,
   the project's main one: a company beats a token (circle.com is Circle's, not
   USDC's), an override beats a looked-up site, an asset's own domain beats one
   another asset only reaches by a redirect (gho.xyz → aave.com is AAVE's), a
   protocol beats its staked, wrapped and stablecoin products (lido.fi is LDO's,
   not stETH's), then the page naming the asset, contracts and CMC rank. Assets
   of one project keep it too: the same Wikipedia article (GOOG and GOOGL) or
   an override's `same_project_as`. A product alone on a site that does not
   carry its name is its issuer's (Pax Dollar on paxos.com) and gets no domain.
7. **Tickers.** When mapped assets share a bare ticker, a company keeps it
   against a coin unless the coin ranks within CoinMarketCap's top 1,500 *and*
   has more contracts (`MA` is Mastercard, `STX` Stacks), and in any case an
   asset that trades today beats one whose contracts all ended (`RIO` is Rio
   Tinto, not the delisted Realio); otherwise the most contracts win, then the
   most still trading, then the better CMC rank. No asset is dropped: the
   others are published under a unique key, their `asset_id`
   (`STX.SEAGATE_...`), else their map key (`C.EQUITY`, Citigroup against the
   coin `C`), else `<TICKER>.<CLASS>` when Atlas minted the bare ID for them
   (`NU.CRYPTO`), else `<TICKER>.<NAME>` (`BTG.OPENVERSE_NETWORK`). The build
   fails if two entries would share a ticker.

## Upstreams and caches

| Upstream | Use | Pacing |
| --- | --- | --- |
| CoinMarketCap data-api `cryptocurrency/detail`, `listing` | websites, names, ranks | ≥1 s |
| Wikidata SPARQL | equities by ticker, crypto articles | ≥5 s |
| Wikipedia API | title checks, 50 per query | ≥1 s |
| Wikidata API `wbgetentities` | other-language sitelinks, 50 titles per query | ≥1 s, `maxlag=5` |
| CoinGecko `coins/list`, `coins/{id}` | homepage cross-check | ≥12 s, at most 30 coins a run |
| each mapped domain | homepage check | one request per host per run |

Requests name Atlas and this repository in their User-Agent. `Retry-After` is
honoured; a 403/451, or a 429 that outlasts the back-off, stops that upstream
for the run and is reported: blocks are never worked around. Wikidata's 5xx and
timeouts are retried once only. CoinMarketCap and CoinGecko are abandoned for
the run after five failures in a row, and the upstream work as a whole stops
after 35 minutes; the affected rows keep their cached values and wait for the
next run. A coin CMC stops serving keeps its websites until a second 404.

Caches in `atlas/data/web/cache/` are committed (and left out of the
wheel). A run only asks upstream about keys it has never seen, domains whose
last check failed, and a bounded slice of the oldest rows: 150 CMC details
(about 2,300 coins: a full cycle in ~16 days), 300 symbol candidates (~3,600
symbols, ~12 days), 200 Wikidata tickers (~3,200, ~16 days) and 300 homepages
(~2,400 domains, ~8 days). Rows of keys that left the universe are pruned.
`--offline` rebuilds from the caches alone, which is how an override change
is previewed.

## The scheduled job

`.github/workflows/web_domain_map.yaml` runs daily at 04:30 UTC, after
`daily-update`, and keeps one pull request labelled `web-domain-map` on the
branch `automation/web-domain-map`. The branch is rebuilt from `main` on every
run (starting from the open PR's caches), so fix a mapping in a separate PR to
`overrides.json` rather than on the bot's branch. The PR body lists added,
removed and changed domains and articles, newly flagged entries, coverage,
verification counts and every upstream error (page titles quoted as code).

Guards:

* **Large change.** A rebuild that loses more than max(20, 2%) of the map's
  entries or domains is not written: the job fails (and alerts Slack) instead
  of proposing it. After checking the losses are real, re-run it by hand with
  `accept-large-change`.
* The map is validated (`tests/test_web_data.py`) before every push;
  the bot's pushes do not trigger the test workflow.
* A run whose map changed only in dates is not proposed; refreshed caches are
  pushed at most weekly.
* Only `main` proposes: a dispatch on another branch is a preview.

## Reviewing flags

| Flag | Meaning | Usual fix |
| --- | --- | --- |
| `no_identity_evidence` | Nothing on the page or from CoinGecko names the asset | Check the site; override the right domain, or `null` |
| `coingecko_disagrees:<domain>` | CoinGecko lists another site | Decide which is current; override if CMC is stale |
| `atlas_name_differs:<name>` | Wikidata's company for the ticker is not Atlas's | Override the company's domain and article |
| `redirects_to_blocked:<host>` | The site forwards to a social or link page | Usually fine (the domain is still the asset's); `null` if abandoned |
| `unreachable` | The last check failed (the verdict shown is the one before); unmapped after a second failed day | None |
| `redirect_unverified:<domain>` | The site forwards to a domain that does not name the asset | Override the new domain if it is a rebrand, else `null` |
| `http_<status>` | An error page | Check; `null` if abandoned |
| `override_*` | An overridden domain redirected, parked or died | Update the override |
