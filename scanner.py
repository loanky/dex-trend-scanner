"""
Solana DEX pool scanner -- ranked discovery across all Solana tokens.

ARCHITECTURE CHANGE FROM THE EARLIER PUMPFUN-ONLY VERSION:
DexScreener's public API has NO endpoint that returns "all tokens on a chain,
ranked by volume/market cap/% change" -- its only chain-wide route is
/latest/dex/search?q=<term>, which requires a free-text query and returns
relevance-ranked results, not metric-ranked ones. Confirmed by reading
DexScreener's complete OpenAPI reference directly (docs.dexscreener.com/api/
reference): every endpoint either needs a specific token/pair address you
already know, or is unrelated profile/boost/ad metadata. There is no ranked
listing endpoint anywhere in it.

GeckoTerminal's free, keyless API DOES have this:
    GET https://api.geckoterminal.com/api/v2/networks/{network}/pools
        ?sort=<field>&page=<n>
Confirmed directly from GeckoTerminal's own changelog
(apiguide.geckoterminal.com/changelogs), which documents two sort values as
added features: h24_volume_usd_desc and h24_tx_count_desc. Ascending variants
and other metrics (market cap, price-change%) follow the same naming pattern
in community client libraries, but were NOT found written out explicitly in
GeckoTerminal's own docs during this build -- see RANKING METRICS below for
exactly which values are confirmed vs. inferred-by-convention.

This version therefore uses GeckoTerminal /pools as the PRIMARY discovery AND
core-filter data source (it already returns fdv_usd, volume_usd, reserve_in_usd,
transactions, and pool_created_at per pool -- everything the core filters need
-- so DexScreener is no longer required for the main pipeline). DexScreener's
/search is kept as an optional secondary source (see search_terms in
config.yaml) for anyone who still wants pump.fun-style keyword search
alongside ranked discovery, but it's no longer load-bearing -- leave
search_terms as an empty list to skip it entirely.

RANKING METRICS (rank_by in config.yaml):
  rank_by is a list, but entries are swept SEQUENTIALLY -- one full metric
  (all its configured pages) at a time, then merged/de-duplicated by pool
  address -- NOT queried simultaneously (GeckoTerminal's API accepts only
  one `sort` value per request regardless). A second entry genuinely
  doubles discovery-phase API calls every cycle. Defaults to a single
  entry so the metric can be freely reconfigured without that cost; add a
  second only if you actually want both swept and merged every cycle.

  CONFIRMED FREE (from GeckoTerminal's own changelog, for the plain
  /pools?sort= endpoint used here):
    - h24_volume_usd_desc   -- highest 24h volume first
    - h24_tx_count_desc     -- most 24h transactions first
  CONFIRMED TO EXIST BUT NOT FREE: a four-value sort list
  (h24_tx_count_desc, h24_volume_usd_desc, h24_price_percentage_change_desc,
  pool_created_at_desc) exists on CoinGecko's separate Megafilter endpoint
  (/v3/onchain/pools/megafilter) -- confirmed Analyst-tier-and-above (paid)
  directly from CoinGecko's own status page and their own Megafilter
  tutorial. Not usable with the free Demo key this scanner uses; not wired
  into fetch_ranked_pools, which calls the plain /pools endpoint only.
  INFERRED BY CONVENTION, NOT independently confirmed for the free /pools
  endpoint (their API is explicitly marked Beta and undocumented values
  could 400 or silently no-op):
    - h24_volume_usd_asc, h24_tx_count_asc
    - market_cap_usd_desc / _asc, fdv_usd_desc / _asc  (market_cap_usd is
      null for tokens not verified on CoinGecko -- fdv_usd is more reliably
      populated for pump.fun-style tokens per GeckoTerminal's own FAQ,
      which states fdv_usd "will always be returned" since it's a pure
      supply x price calculation with no verification dependency)
  TEST ANY NON-CONFIRMED VALUE YOURSELF (--once --log-level DEBUG) before
  trusting it in a schedule. If GeckoTerminal rejects or ignores an unknown
  sort value, this script logs the raw response rather than silently
  returning wrong-order results -- see fetch_ranked_pools()'s docstring.

TWO-TIER CACHING BECAME ONE-TIER ON REQUEST (see version history at the
end of this docstring): this script used to also track an "already
emailed" set to avoid repeat emails for the same pool. That suppression
was removed on request -- every pool that matches every filter gets
emailed EVERY cycle it still matches, including pools that matched in a
previous cycle. Read this carefully if you're picking this script back up
later: this is an intentional, explicit choice, not an oversight.

  1. STATIC CACHE (static_cache.json): pair address -> fields that CANNOT
     change once observed (pool_created_at, dex_id, network, base token
     address). Once a pair has been seen once, this script never re-fetches
     these fields for it again -- there is nothing to gain from doing so.
     This is a pure fetch-avoidance optimization; it never causes a filter
     to be skipped.

  2. MUTABLE DATA: volume, FDV, liquidity, txns, price-change -- all of
     these are re-fetched (via the same ranked /pools call that does
     discovery) and every filter is re-evaluated against them EVERY SINGLE
     CYCLE for every pool currently in the ranked window, with NO
     exceptions. A pool that failed min_liquidity_usd last cycle can clear
     it next cycle; skipping re-evaluation would silently drop a real
     future match. Caching applies only to tier 1 (static, provably
     unchanging) data, never to tier 2.

  THERE IS NO SUPPRESSION STEP. A pool that matches every filter is
  emailed. If it still matches next cycle, it is emailed again next cycle.
  This means: expect repeat emails for tokens whose data continues to
  satisfy your filters week over week -- that is the requested behavior,
  not a bug.

Pipeline for candle-color and holder-count (unchanged from the earlier
version, still verified against the docs cited in each function's
docstring):
  2. CANDLE COLOR -> GeckoTerminal OHLCV endpoint (see last_n_candles_green)
  3. HOLDER COUNT -> CoinGecko onchain Token Info (see get_coingecko_holder_count),
       with an optional Solana-RPC top-20 fallback (see get_top_holder_count)

CADENCE: default scheduling is WEEKLY, UTC+0, via external cron/GitHub
Actions -- see README.md and .github/workflows/scan.yml. poll_interval_sec
in config.yaml is only consulted in --loop mode (see main()); on a weekly
cadence, an external weekly trigger calling --once is what's recommended --
see README.md for why a --loop process sleeping for a week at a time isn't a
great fit for any of the free hosts discussed.

FILTER LIMITATIONS -- READ BEFORE RELYING ON THIS SCRIPT:
  - Holder count via CoinGecko is labeled Beta by CoinGecko themselves, and
    this script could not confirm the live response shape (see
    get_coingecko_holder_count's docstring). The solana_rpc fallback caps at
    the top 20 holders regardless of true count.
  - "Token age" is measured from pool_created_at (pool creation), not from
    token mint time. For pump.fun-style tokens these are usually close
    together but are not guaranteed to be identical.
  - GitHub Actions cron has known timing drift and a minimum 5-minute
    granularity; on a weekly cadence this drift is proportionally
    negligible, but it's still not to-the-second precise. See README.md.
  - GeckoTerminal's /pools sort parameter is Beta and only two exact values
    are independently confirmed (see RANKING METRICS above). Non-confirmed
    values should be tested before being trusted in a schedule.
  - This script could not live-test any network call from the sandbox it
    was built in -- see each function's docstring for exactly what that
    means for that specific integration. Run --once --log-level DEBUG
    yourself before trusting a schedule.

Run manually:
    python3 scanner.py --config config.yaml --once
Run in a loop (only sensible on a host you keep alive yourself, e.g. a VPS):
    python3 scanner.py --config config.yaml --loop
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import smtplib
import ssl
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests
import yaml

log = logging.getLogger("dex_scanner")

DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
GECKOTERMINAL_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/{network}/pools"
GECKOTERMINAL_OHLCV_URL = (
    "https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/ohlcv/{timeframe}"
)
COINGECKO_TOKEN_INFO_URL = (
    "https://api.coingecko.com/api/v3/onchain/networks/{network}/tokens/{address}/info"
)
SOLANA_RPC_DEFAULT = "https://api.mainnet-beta.solana.com"

# Sort values independently confirmed in GeckoTerminal's own changelog.
# Anything else is inferred-by-convention -- see module docstring.
CONFIRMED_RANK_BY_VALUES = {"h24_volume_usd_desc", "h24_tx_count_desc"}

# Conservative defaults. These are NOT guarantees from the providers; they are
# defensive throttles based on the rate limits documented (with varying
# consistency) as of 2026-08-01. Tighten them if you see repeated 429s.
DEXSCREENER_MIN_INTERVAL_SEC = 0.25     # ~240/min, under the documented 300/min ceiling
# GECKOTERMINAL_MIN_INTERVAL_SEC was originally set assuming a 10/min limit
# because sources disagreed at the time this was first written. Verified
# directly against GeckoTerminal's own FAQ (apiguide.geckoterminal.com/faq)
# and their own changelog, both confirming the CURRENT public rate limit is
# 30 calls/min, not 10 -- the changelog shows it was raised from 10 to 30 at
# some point, and older/third-party sources hadn't caught up. This constant
# is shared across THREE call sites: fetch_ranked_pools (discovery),
# fetch_ohlcv (candle check), and resolve_base_token_address (mint lookup).
GECKOTERMINAL_MIN_INTERVAL_SEC = 3.333  # ~18/min, 40% margin under the confirmed 30/min ceiling
                                         # (genuinely 2x the prior 20% margin -- see the note
                                         # in RateLimiter's docstring below for why simply
                                         # doubling the interval would NOT have doubled the
                                         # margin, since margin is nonlinear relative to interval)
COINGECKO_MIN_INTERVAL_SEC = 0.7        # ~85/min, under the documented 100/min Demo tier cap
SOLANA_RPC_MIN_INTERVAL_SEC = 0.5


class RateLimiter:
    """
    Token-bucket-of-one: blocks until min_interval has elapsed since last
    call. Optionally supports a "ramp-up" period: for the first
    ramp_up_calls calls of a fresh instance, uses ramp_up_interval_sec
    instead of min_interval_sec, then reverts to min_interval_sec for
    every call after that.

    WHY RAMP-UP EXISTS (found from a real live run, not a guess): a fresh
    RateLimiter's _last_call starts at 0.0, so the FIRST call on any new
    instance is never delayed at all -- elapsed-since-0.0 is always huge,
    so the "wait if elapsed < min_interval" check never triggers. This is
    correct for spacing out consecutive calls, but it means a brand-new
    process (which is what every GitHub Actions run is -- fresh process,
    fresh RateLimiter, every single time) starts with an unthrottled call,
    then settles into steady spacing after that.

    This matters because a steady-state average (e.g. "24 calls/min at a
    2.5s interval, comfortably under a 30/min ceiling") describes an
    indefinite average -- it does NOT describe the density of calls in the
    first ~15-20 seconds of a fresh process, which is denser than the
    average once you remove that free first call. A real DEBUG-level log
    from an actual live run confirmed this exactly: 8 GeckoTerminal calls
    landed within 17.5 seconds (computed timestamps matched a 2.5s gap
    between EVERY pair of calls, confirming the throttle itself was never
    violated) before the 8th call hit a 429. 8 calls / 17.5s works out to
    ~27.4 calls/min if that density continued -- uncomfortably close to
    the documented 30/min ceiling if GeckoTerminal enforces its limit as a
    rolling or fixed window (common for rate limiting) rather than a pure
    smooth long-run average.

    Ramp-up directly targets this: widen the interval specifically for the
    first several calls (the exact window where density is highest),
    computed to hold ~20-22 calls/min during that period -- real, computed
    margin under the 30/min ceiling, not just barely under it -- then
    relax back to the normal steady-state interval once enough calls have
    happened that a 60s rolling window would naturally include some of the
    slower early calls too, further diluting density on its own.
    """

    def __init__(self, min_interval_sec: float, ramp_up_calls: int = 0,
                 ramp_up_interval_sec: float = 0.0):
        self.min_interval_sec = min_interval_sec
        self.ramp_up_calls = ramp_up_calls
        self.ramp_up_interval_sec = ramp_up_interval_sec
        self._last_call = 0.0
        self._call_count = 0

    def wait(self):
        active_interval = self.min_interval_sec
        if self._call_count < self.ramp_up_calls:
            active_interval = self.ramp_up_interval_sec

        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < active_interval:
            time.sleep(active_interval - elapsed)
        self._last_call = time.monotonic()
        self._call_count += 1


dex_limiter = RateLimiter(DEXSCREENER_MIN_INTERVAL_SEC)
# BUG FIX (found during audit): fetch_ranked_pools, fetch_ohlcv, and
# resolve_base_token_address all hit api.geckoterminal.com and must share
# ONE rate budget. An earlier version of this file created two SEPARATE
# RateLimiter instances (gecko_pools_limiter, gecko_ohlcv_limiter) from the
# same GECKOTERMINAL_MIN_INTERVAL_SEC constant -- since each RateLimiter
# tracks its own independent _last_call timer, two instances running
# concurrently (which genuinely happens: run_once's per-pool loop calls
# fetch_ohlcv and then resolve_base_token_address back-to-back for the
# same pool) could each independently permit a call every 2.5s, summing to
# a worst-case combined rate of 48/min against GeckoTerminal's real 30/min
# ceiling -- 60% OVER the documented limit, not safely under it. Fixed by
# using ONE shared instance for all three call sites, so the 2.5s interval
# is genuinely a floor between ANY two GeckoTerminal calls, regardless of
# which function makes them.
#
# RAMP-UP FIX (found from a real live run, see RateLimiter's docstring for
# the full reasoning): the first 10 calls use a wider interval than the
# steady-state one, since a fresh process's early calls are denser than
# the long-run average implies. Both this ramp-up interval and the
# steady-state GECKOTERMINAL_MIN_INTERVAL_SEC above were widened together
# to genuinely double their respective safety margins under the 30/min
# ceiling (not just doubled in raw seconds -- margin is nonlinear
# relative to interval, so naively doubling the interval would have
# tripled the margin instead of doubling it; see the worked computation
# in this project's README.md for the exact arithmetic). 6.0s during
# ramp-up holds ~10 calls/min, a 66.7% margin under 30/min -- genuinely
# 2x the prior 33.3% margin the original 3.0s ramp-up interval provided.
gecko_limiter = RateLimiter(GECKOTERMINAL_MIN_INTERVAL_SEC,
                             ramp_up_calls=10, ramp_up_interval_sec=6.0)
coingecko_limiter = RateLimiter(COINGECKO_MIN_INTERVAL_SEC)
rpc_limiter = RateLimiter(SOLANA_RPC_MIN_INTERVAL_SEC)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Config:
    # Documentation-only for GitHub Actions users: GitHub's schedule trigger
    # cannot read this value (see config.yaml's comment on this field for
    # why). Genuinely used only if you build --loop or real-crontab
    # scheduling around it yourself.
    schedule_cron: str

    # Discovery (GeckoTerminal ranked /pools -- primary)
    network: str                      # GeckoTerminal network slug, e.g. "solana"
    rank_by: list[str]                # list of sort values to sweep, e.g. ["h24_volume_usd_desc"]
    max_pages_per_rank: int           # GeckoTerminal /pools returns 20 pools/page
    dex_ids: list[str]                # empty list = no dex filter (all Solana DEXes)

    # Discovery (DexScreener /search -- optional secondary; [] to disable)
    search_terms: list[str]

    # Filters
    min_age_weeks: float
    min_holders: int
    max_holders: int
    green_candle_timeframe: str      # "day" or "hour" (GeckoTerminal vocabulary)
    green_candle_count: int
    min_fdv: float
    max_fdv: float
    min_txns_24h: int
    min_volume_24h: float
    max_volume_24h: float
    min_liquidity_usd: float

    # Holder check
    check_holders: bool
    holder_count_source: str          # "coingecko" (default, real count) or "solana_rpc" (top-20 fallback)
    coingecko_api_key_env: str        # name of env var holding the free CoinGecko Demo API key
    holder_dust_threshold: float      # only used when holder_count_source == "solana_rpc"
    solana_rpc_url: str               # only used when holder_count_source == "solana_rpc"

    # Candle check
    check_green_candles: bool

    # Email -- addresses are read from environment variables (see
    # send_match_email), never stored directly in config.yaml, so they
    # never sit in plaintext in a possibly-public git repo. Each field
    # here holds only the NAME of an env var, not the address itself.
    smtp_host: str
    smtp_port: int
    smtp_user_env: str         # env var holding the address you authenticate as
    smtp_password_env: str
    email_from_env: str        # env var holding the "From" address (usually same as smtp_user_env)
    email_to_env: str          # env var holding recipient address(es), comma-separated for multiple

    # Operational / caching
    poll_interval_sec: int            # only used in --loop mode, see main()
    static_cache_path: str            # tier 1: provably-unchanging pool fields
    max_pairs_per_search: int         # cap on DexScreener /search results per term


def load_config(path: str) -> Config:
    with open(path, "r") as f:
        raw = yaml.safe_load(f)
    return Config(**raw)


# Tracks every 429 seen during the current run_once() call, for cross-429
# pattern analysis -- see get_429_pattern_summary() and
# log_429_pattern_summary(). Reset at the start of every run_once() call
# (each GitHub Actions run is a fresh process anyway, so this would start
# empty regardless, but run_once() resets it explicitly rather than rely
# on that, in case this script is ever run via --loop on a long-lived
# process, where module-level state WOULD otherwise persist across cycles).
_429_observations: list[dict] = []


def log_429_and_get_backoff(resp: requests.Response, context: str, default_backoff_sec: int) -> int:
    """
    Logs the FULL response headers on a 429, and returns how many seconds
    to actually sleep before retrying. Also records this observation into
    the module-level _429_observations list, for cross-429 pattern
    analysis within a single run -- see get_429_pattern_summary().

    WHY THIS EXISTS: after a real GitHub Actions run hit a 429 despite the
    script's own throttle respecting a 6.0s gap between every call (66.7%
    margin under GeckoTerminal's documented 30/min ceiling), it became
    unclear whether this was (a) GeckoTerminal enforcing a stricter real
    limit than documented, (b) a shared-IP/datacenter-traffic effect on
    GitHub Actions' runner pool, or (c) something else entirely -- and
    continuing to guess and re-tune constants without evidence wasn't
    productive. This function surfaces the actual server response instead.

    RESULT FROM A REAL RUN (confirmed evidence, not a guess): a live 429
    from api.geckoterminal.com came back with Retry-After: 0 -- meaning
    "you may retry immediately" -- and NO X-RateLimit-* or any
    GeckoTerminal-specific headers of any kind; every header present was
    either generic HTTP plumbing or explicitly Cloudflare infrastructure
    (Server: cloudflare, CF-RAY). Retry-After: 0 is the signature of a
    genuine, counting rate limiter whose window had just reset -- NOT
    typical of a bot-detection/infrastructure-level block, which would
    more likely return a bare rejection with no rate-limit metadata at
    all. This is real evidence AGAINST the shared-IP-saturation theory
    being the explanation for that specific 429, though it doesn't rule
    it out entirely (a genuinely fast-clearing burst of unrelated traffic
    could theoretically still produce this same signature).

    No source consulted (official GeckoTerminal docs, or general HTTP
    rate-limiting references) confirms GeckoTerminal's exact header
    convention -- unlike GitHub's API, which documents X-RateLimit-* by
    name, nothing found here says GeckoTerminal does the same. Rather than
    guess a specific header name and silently miss the real one, this
    logs the COMPLETE header dict, so whatever GeckoTerminal actually
    sends is visible in the log regardless of naming convention.

    One genuinely useful diagnostic, from general HTTP rate-limiting
    references (not GeckoTerminal-specific, but a widely-cited pattern):
    a 429 that does NOT include a Retry-After header is more often
    associated with bot-detection/infrastructure-level blocking than with
    a standard, well-behaved rate limiter, which "almost always" sets it.
    This function logs explicitly whether Retry-After was present, as a
    direct signal toward that question -- not proof either way on its
    own, but real evidence rather than another guess.
    """
    headers_dict = dict(resp.headers)
    log.warning("%s 429 -- full response headers: %s", context, headers_dict)

    retry_after_raw = resp.headers.get("Retry-After")
    parsed_retry_after: Optional[int] = None
    backoff = default_backoff_sec

    if retry_after_raw:
        log.warning("%s: server sent Retry-After=%r", context, retry_after_raw)
        if retry_after_raw.isdigit():
            parsed_retry_after = int(retry_after_raw)
            backoff = parsed_retry_after
        else:
            log.warning("%s: Retry-After value isn't a plain integer (could be an "
                         "HTTP-date instead, which this script doesn't parse) -- "
                         "falling back to the default %ds backoff", context, default_backoff_sec)
    else:
        log.warning("%s: NO Retry-After header present. Per general HTTP "
                     "rate-limiting convention, well-behaved rate limiters "
                     "almost always include this -- its absence is a real, "
                     "though not conclusive, signal worth weighing toward "
                     "infrastructure-level blocking rather than a standard "
                     "limiter. Falling back to the default %ds backoff.",
                     context, default_backoff_sec)

    _429_observations.append({
        "context": context,
        "retry_after_raw": retry_after_raw,
        "retry_after_parsed": parsed_retry_after,
        "had_ratelimit_header": any("ratelimit" in k.lower() for k in headers_dict),
        "backoff_used": backoff,
        "timestamp": time.monotonic(),
    })

    return backoff


def log_429_pattern_summary() -> None:
    """
    Logs a summary of every 429 observed so far this run, once discovery
    finishes -- called from run_once() after the discovery loop, not
    after each individual 429, since the point is seeing the PATTERN
    across all of them together, not repeating per-429 detail that
    log_429_and_get_backoff() already logged individually.

    WHAT THIS IS FOR: a single 429 with Retry-After: 0 is one data point.
    If Retry-After stays consistently near-zero across MULTIPLE 429s in
    the same run, that's much stronger evidence for a fast-resetting,
    burst-window rate limiter specifically (the window opens, fills
    briefly, and clears again quickly) -- a materially different
    situation from a limiter with a longer rolling window, and different
    again from a shared-IP effect, which would more plausibly show
    inconsistent or non-zero Retry-After values as other unrelated
    traffic's state bleeds in unpredictably. A no-429-this-run outcome is
    itself informative too, logged explicitly rather than silently saying
    nothing.
    """
    if not _429_observations:
        # Uses .warning(), matching every other message in this feature
        # (found during testing: this was originally .info(), the only
        # mismatched level here -- under some logging configurations, that
        # would make this specific message silently invisible while every
        # other 429-pattern message stayed visible, an inconsistency worth
        # fixing rather than leaving fragile).
        log.warning("No 429s observed this run.")
        return

    count = len(_429_observations)
    retry_after_values = [
        obs["retry_after_parsed"] for obs in _429_observations
        if obs["retry_after_parsed"] is not None
    ]
    any_ratelimit_header = any(obs["had_ratelimit_header"] for obs in _429_observations)

    log.warning("429 PATTERN SUMMARY for this run: %d total 429(s) observed", count)

    if retry_after_values:
        all_near_zero = all(v <= 1 for v in retry_after_values)
        log.warning(
            "429 PATTERN SUMMARY: Retry-After values seen (parsed, in order): %s -- "
            "%s",
            retry_after_values,
            "ALL near-zero (<=1s) -- consistent with a fast-resetting, "
            "burst-window rate limiter" if all_near_zero else
            "NOT all near-zero -- mixed or larger values, worth reading "
            "individually rather than assuming a single simple pattern",
        )
    else:
        log.warning("429 PATTERN SUMMARY: no numeric Retry-After value was ever "
                     "parsed from any of the %d observed 429(s) -- see individual "
                     "warnings above for what each one actually returned.", count)

    log.warning(
        "429 PATTERN SUMMARY: any X-RateLimit-*-style header ever present across "
        "all %d observation(s)? %s", count, any_ratelimit_header,
    )


# --------------------------------------------------------------------------
# GeckoTerminal ranked discovery (primary): /networks/{network}/pools?sort=
# --------------------------------------------------------------------------

def fetch_ranked_pools(network: str, rank_by: str, page: int,
                        session: requests.Session) -> Optional[list[dict]]:
    """
    GET /networks/{network}/pools?sort=<rank_by>&page=<page>
    This is GeckoTerminal's free, keyless, chain-wide ranked pool listing --
    the endpoint DexScreener's API does not have (see module docstring).

    Confirmed response shape (JSON:API style), cross-checked against
    GeckoTerminal's own changelog example and multiple independent
    community client libraries (python geckoterminal-api, Go kkyr/geckoterminal,
    Node alaarab/geckoterminal-api) that all agree on this attributes shape:
        {
          "data": [
            {
              "id": "solana_<pool_address>",
              "type": "pool",
              "attributes": {
                "address": "<pool_address>",
                "name": "TOKEN / SOL",
                "pool_created_at": "2026-01-15T10:00:00Z",   # ISO string, NOT epoch ms
                "fdv_usd": "185000.00",                       # STRING, not number
                "market_cap_usd": null,                       # null if not CoinGecko-verified
                "reserve_in_usd": "42000.00",                 # this is liquidity
                "price_change_percentage": {"h1": "2.1", "h24": "18.2"},
                "transactions": {
                  "h24": {"buys": 900, "sells": 640, "buyers": 500, "sellers": 400}
                },
                "volume_usd": {"h24": "210000.00"}
              },
              "relationships": {"dex": {"data": {"id": "<dex_id>", "type": "dex"}}}
            }
          ]
        }
    IMPORTANT: fdv_usd, reserve_in_usd, and every volume_usd/*.h24 value are
    returned as JSON STRINGS, not numbers, in every source checked -- this
    function converts them, and passes_core_filters expects already-converted
    floats. If the live response differs, this is designed to raise clearly
    (see the try/except below) rather than silently misparse.

    NOTE ON VERIFICATION: like the other GeckoTerminal/CoinGecko endpoints in
    this script, this could not be live-tested from the sandbox this was
    built in. The confirmed sort values (h24_volume_usd_desc,
    h24_tx_count_desc) come directly from GeckoTerminal's own changelog; the
    response shape comes from cross-referencing multiple independent
    community client libraries that all describe the same attributes. Run
    --once --log-level DEBUG against this yourself before trusting a
    schedule -- see README.md.
    """
    if rank_by not in CONFIRMED_RANK_BY_VALUES:
        log.warning(
            "rank_by=%r is not independently confirmed in GeckoTerminal's own docs "
            "(only %s are) -- proceeding, but verify the returned order is actually "
            "what you expect with --log-level DEBUG. See scanner.py module docstring.",
            rank_by, CONFIRMED_RANK_BY_VALUES,
        )

    gecko_limiter.wait()
    url = GECKOTERMINAL_POOLS_URL.format(network=network)
    resp = session.get(
        url,
        params={"sort": rank_by, "page": page},
        headers={"Accept": "application/json;version=20230302"},
        timeout=15,
    )
    if resp.status_code == 429:
        backoff = log_429_and_get_backoff(
            resp, f"GeckoTerminal /pools (sort={rank_by!r}, page={page})", default_backoff_sec=15,
        )
        time.sleep(backoff)
        return None
    if resp.status_code == 400:
        log.error(
            "GeckoTerminal /pools returned 400 for sort=%r -- this sort value is "
            "likely invalid. Response body: %s", rank_by, resp.text[:500],
        )
        return None
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("data") or []


def normalize_gecko_pool(raw_pool: dict) -> Optional[dict]:
    """
    Converts one raw GeckoTerminal /pools entry into the flat dict shape
    passes_core_filters() and the rest of this script expect. Returns None
    (and logs) if the shape doesn't match what's documented, rather than
    letting a malformed entry crash the whole cycle or silently pass with
    wrong data.
    """
    try:
        pool_id = raw_pool["id"]                       # e.g. "solana_ABCDEF..."
        attrs = raw_pool["attributes"]
        pool_address = attrs["address"]
        dex_id = raw_pool.get("relationships", {}).get("dex", {}).get("data", {}).get("id")

        created_raw = attrs.get("pool_created_at")
        created_ms = None
        if created_raw:
            # ISO 8601 string -> epoch ms, kept in the same units passes_core_filters
            # already used for the DexScreener-era pairCreatedAt field.
            created_dt = dt.datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            created_ms = int(created_dt.timestamp() * 1000)

        fdv_raw = attrs.get("fdv_usd")
        fdv = float(fdv_raw) if fdv_raw is not None else None

        liquidity_raw = attrs.get("reserve_in_usd")
        liquidity_usd = float(liquidity_raw) if liquidity_raw is not None else None

        vol_h24_raw = (attrs.get("volume_usd") or {}).get("h24")
        vol_h24 = float(vol_h24_raw) if vol_h24_raw is not None else None

        txns_h24 = (attrs.get("transactions") or {}).get("h24") or {}
        buys = int(txns_h24.get("buys") or 0)
        sells = int(txns_h24.get("sells") or 0)

        name = attrs.get("name") or "UNKNOWN/UNKNOWN"
        base_symbol = name.split("/")[0].strip() if "/" in name else name

        return {
            "pairAddress": pool_id,             # used as the unique cache/dedupe key throughout
            "poolAddressOnly": pool_address,      # used for OHLCV calls, which want the bare address
            "dexId": dex_id,
            "pairCreatedAt": created_ms,
            "fdv": fdv,
            "liquidity": {"usd": liquidity_usd} if liquidity_usd is not None else None,
            "volume": {"h24": vol_h24},
            "txns": {"h24": {"buys": buys, "sells": sells}},
            "baseToken": {
                # GeckoTerminal's /pools listing doesn't include the base
                # token's mint address directly -- only the pool address and
                # a display name. Left None here; resolve_base_token_address()
                # fills this in later, but only for pools that reach the
                # holder-count check (see run_once), to avoid the extra API
                # call for pools that never get that far.
                "address": None,
                "symbol": base_symbol,
                "name": name,
            },
            "url": f"https://www.geckoterminal.com/{pool_id.split('_', 1)[0]}/pools/{pool_address}",
        }
    except (KeyError, TypeError, ValueError) as e:
        log.warning("Could not normalize GeckoTerminal pool entry (skipping it): %s -- raw=%r",
                    e, raw_pool)
        return None


# --------------------------------------------------------------------------
# DexScreener keyword search (optional secondary discovery source)
# --------------------------------------------------------------------------

def dexscreener_search(term: str, session: requests.Session) -> list[dict]:
    """
    Calls GET /latest/dex/search?q=<term>.
    Docs: https://docs.dexscreener.com/api/reference
    No API key. Returns {"schemaVersion": ..., "pairs": [...]}.
    Kept as an OPTIONAL secondary discovery source alongside GeckoTerminal's
    ranked listing -- leave search_terms: [] in config.yaml to skip this
    entirely and rely only on ranked discovery.
    """
    dex_limiter.wait()
    resp = session.get(
        DEXSCREENER_SEARCH_URL,
        params={"q": term},
        headers={"User-Agent": "dex-scanner/1.0"},
        timeout=15,
    )
    if resp.status_code == 429:
        log.warning("DexScreener 429 rate limited on term=%r; backing off 10s", term)
        time.sleep(10)
        return []
    resp.raise_for_status()
    data = resp.json()
    return data.get("pairs") or []


# --------------------------------------------------------------------------
# Core filters -- evaluated fresh every cycle against MUTABLE data (see
# module docstring, tier 2). Never skipped for a previously-seen pair.
# --------------------------------------------------------------------------

def passes_core_filters(pair: dict, cfg: Config, now_utc: dt.datetime) -> tuple[bool, str]:
    """
    Checks every field the normalized pool/pair dict actually contains,
    regardless of whether it came from GeckoTerminal or DexScreener
    (normalize_gecko_pool() and DexScreener's native shape both produce
    compatible fields for every check here).
    Returns (passed, reason_if_failed).
    """
    if cfg.dex_ids and pair.get("dexId") not in cfg.dex_ids:
        return False, f"dexId={pair.get('dexId')!r} not in {cfg.dex_ids}"

    created_ms = pair.get("pairCreatedAt")
    if created_ms is None:
        return False, "pairCreatedAt/pool_created_at missing"
    created_at = dt.datetime.fromtimestamp(created_ms / 1000, tz=dt.timezone.utc)
    age_weeks = (now_utc - created_at).total_seconds() / (7 * 24 * 3600)
    if age_weeks < cfg.min_age_weeks:
        return False, f"age {age_weeks:.2f}w < min {cfg.min_age_weeks}w"

    fdv = pair.get("fdv")
    if fdv is None:
        # GeckoTerminal's own FAQ states fdv_usd is always returned, since
        # it's a pure supply x price calculation with no verification
        # dependency (unlike market_cap_usd, which is null for unverified
        # tokens) -- a missing value here is unexpected, not routine, but
        # still handled safely rather than crashing.
        return False, "fdv missing"
    if not (cfg.min_fdv <= fdv <= cfg.max_fdv):
        return False, f"fdv {fdv} outside [{cfg.min_fdv}, {cfg.max_fdv}]"

    txns = pair.get("txns") or {}
    h24 = txns.get("h24") or {}
    txn_count_24h = (h24.get("buys") or 0) + (h24.get("sells") or 0)
    if txn_count_24h < cfg.min_txns_24h:
        return False, f"24h txns {txn_count_24h} < min {cfg.min_txns_24h}"

    volume = pair.get("volume") or {}
    vol_24h = volume.get("h24")
    if vol_24h is None:
        return False, "volume.h24 missing"
    if not (cfg.min_volume_24h <= vol_24h <= cfg.max_volume_24h):
        return False, f"24h volume {vol_24h} outside [{cfg.min_volume_24h}, {cfg.max_volume_24h}]"

    # liquidity is documented as nullable (DexScreener's OpenAPI schema; also
    # observed as a possibility in GeckoTerminal's shape) -- must reject
    # cleanly here, not crash.
    liquidity = pair.get("liquidity")
    if not liquidity or liquidity.get("usd") is None:
        return False, "liquidity.usd missing/null"
    if liquidity["usd"] < cfg.min_liquidity_usd:
        return False, f"liquidity ${liquidity['usd']:.0f} < min ${cfg.min_liquidity_usd:.0f}"

    return True, ""


# --------------------------------------------------------------------------
# GeckoTerminal OHLCV candle-color check (unchanged from the earlier version)
# --------------------------------------------------------------------------

def fetch_ohlcv(pool_address: str, network: str, timeframe: str,
                 session: requests.Session, limit: int = 10) -> Optional[list[list]]:
    """
    GET /networks/{network}/pools/{pool}/ohlcv/{timeframe}
    Docs: https://apiguide.geckoterminal.com/
    Returns list of [timestamp, open, high, low, close, volume], most recent last
    per GeckoTerminal's documented ordering -- but this script sorts explicitly
    rather than trusting response order, since that ordering guarantee is not
    spelled out in the primary docs we could verify.
    No API key on the free/demo tier; 30 req/min confirmed directly from
    GeckoTerminal's own FAQ and changelog (apiguide.geckoterminal.com) --
    this endpoint shares that limit with fetch_ranked_pools and
    resolve_base_token_address via the single shared gecko_limiter
    instance (see its definition for why this must be ONE shared instance,
    not one per function).
    """
    gecko_limiter.wait()
    url = GECKOTERMINAL_OHLCV_URL.format(network=network, pool=pool_address, timeframe=timeframe)
    # No `aggregate` param is sent -- relying on GeckoTerminal's documented
    # default of 1 (confirmed identically across their own FAQ, a live
    # worked example in their docs showing /ohlcv/day with no aggregate
    # param, and the CoinGecko-hosted reference page). timeframe="day" +
    # default aggregate=1 = genuine 1-day candles, which is what
    # green_candle_timeframe in config.yaml assumes. If you ever change
    # green_candle_timeframe to something needing a non-default aggregate
    # (e.g. 4-hour candles instead of 1-hour), this function would need an
    # explicit aggregate param added -- it doesn't currently support one.
    resp = session.get(url, params={"limit": limit}, timeout=15,
                        headers={"Accept": "application/json;version=20230302"})
    if resp.status_code == 429:
        backoff = log_429_and_get_backoff(
            resp, f"GeckoTerminal /ohlcv (pool={pool_address})", default_backoff_sec=15,
        )
        time.sleep(backoff)
        return None
    if resp.status_code == 404:
        # Pool not indexed by GeckoTerminal yet -- common for very new pools
        return None
    resp.raise_for_status()
    payload = resp.json()
    try:
        return payload["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        log.warning("Unexpected GeckoTerminal OHLCV shape for pool=%s: %r", pool_address, payload)
        return None


def last_n_candles_green(pool_address: str, network: str, timeframe: str,
                          count: int, session: requests.Session) -> Optional[bool]:
    """
    Fetches OHLCV and checks whether the last `count` *closed* candles were
    green (close > open). The most recent candle in a live feed is usually
    still forming, so it is excluded -- we check the `count` candles before it.
    Returns None if data isn't available (caller should decide how to treat that).
    """
    ohlcv = fetch_ohlcv(pool_address, network, timeframe, session, limit=count + 2)
    if not ohlcv or len(ohlcv) < count + 1:
        return None

    # Explicitly sort ascending by timestamp (index 0) rather than trusting order.
    ohlcv_sorted = sorted(ohlcv, key=lambda row: row[0])

    # Drop the most recent (still-forming) candle, then take the last `count`.
    closed_candles = ohlcv_sorted[:-1]
    if len(closed_candles) < count:
        return None
    check_set = closed_candles[-count:]

    for row in check_set:
        _, o, _high, _low, c, _v = row
        if c is None or o is None or c <= o:
            return False
    return True


# --------------------------------------------------------------------------
# CoinGecko holder count (primary method: real total, not top-N approximation)
# -- unchanged from the earlier version --
# --------------------------------------------------------------------------

def get_coingecko_holder_count(mint_address: str, network: str, api_key: str,
                                session: requests.Session) -> Optional[int]:
    """
    GET /onchain/networks/{network}/tokens/{address}/info
    Docs: https://docs.coingecko.com/reference/token-info-contract-address
    Requires a free CoinGecko Demo API key (see README.md for signup steps).

    Response shape, per CoinGecko's published OpenAPI schema and live example
    (a real Solana pump.fun token, "Pippin", CA
    Dfh5DzRgSvvCFDoYc2ciTkMrbDfRKybA4SoFbPmApump):
        {
          "data": {
            "attributes": {
              "holders": {
                "count": 47911,
                "distribution_percentage": {...},
                "last_updated": "..."
              },
              ...
            }
          }
        }

    IMPORTANT -- READ BEFORE TRUSTING THIS IN PRODUCTION: this function's
    request/response handling was built against CoinGecko's documented schema
    and could not be live-tested against the real endpoint (the sandbox this
    script was developed in has no network route to api.coingecko.com). Run
    the script once with --log-level DEBUG against a real token you expect to
    match, and confirm a sane holder_count comes back, before relying on
    scheduled runs. If the live shape differs from what's coded here, this
    function is written to fail loudly (see the KeyError/TypeError handling
    below) rather than silently return a wrong number.

    CoinGecko's own docs mark the `holders` field "currently in Beta, with
    ongoing improvements to coverage and update frequency" -- so a None
    return here (meaning "couldn't get a holder count for this token") is
    expected sometimes, not necessarily a bug in this function.

    NOTE: GeckoTerminal's ranked /pools listing does not return the base
    token's mint address directly (see normalize_gecko_pool()), only the
    pool address. This function needs a real mint address, so the holder
    check in run_once() resolves the mint separately -- see
    resolve_base_token_address().
    """
    if not api_key:
        raise RuntimeError(
            "No CoinGecko API key configured. Set the env var named in "
            "config.yaml's coingecko_api_key_env to a free CoinGecko Demo API "
            "key -- see README.md for signup steps (no card required)."
        )

    coingecko_limiter.wait()
    url = COINGECKO_TOKEN_INFO_URL.format(network=network, address=mint_address)
    resp = session.get(
        url,
        headers={"x-cg-demo-api-key": api_key, "Accept": "application/json"},
        timeout=15,
    )
    if resp.status_code == 429:
        log.warning("CoinGecko 429 rate limited on mint=%s; backing off 15s", mint_address)
        time.sleep(15)
        return None
    if resp.status_code == 401:
        raise RuntimeError(
            "CoinGecko returned 401 Unauthorized. Check that the API key in "
            "your coingecko_api_key_env environment variable is a valid, "
            "active Demo key from https://www.coingecko.com/en/developers/dashboard."
        )
    if resp.status_code == 404:
        # Token not indexed by CoinGecko's onchain data yet -- common for very
        # new or extremely low-activity tokens.
        return None
    resp.raise_for_status()
    payload = resp.json()

    try:
        holders = payload["data"]["attributes"]["holders"]
    except (KeyError, TypeError):
        # Beta field genuinely absent for this token, per CoinGecko's own
        # "ongoing improvements to coverage" caveat -- not a hard error.
        log.debug("No holders data in CoinGecko response for mint=%s", mint_address)
        return None

    if holders is None:
        return None

    count = holders.get("count")
    if count is None:
        log.debug("CoinGecko holders object present but count is null for mint=%s", mint_address)
        return None

    return int(count)


# --------------------------------------------------------------------------
# Solana RPC holder count (fallback: approximate, top 20 largest accounts only)
# -- unchanged from the earlier version --
# --------------------------------------------------------------------------

def get_top_holder_count(mint_address: str, rpc_url: str, dust_threshold: float,
                          session: requests.Session) -> Optional[int]:
    """
    Calls getTokenLargestAccounts via standard Solana JSON-RPC.
    https://solana.com/docs/rpc/http/gettokenlargestaccounts
    This method returns AT MOST the top 20 token accounts by balance -- it is
    not a full holder count. For tokens with more than 20 non-dust holders,
    this will always report a number <= 20 regardless of the true holder count.
    Treat max_holders filtering above ~20 as unenforceable on this free method.
    """
    rpc_limiter.wait()
    resp = session.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [mint_address],
        },
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        log.warning("Solana RPC error for mint=%s: %r", mint_address, payload["error"])
        return None
    accounts = payload.get("result", {}).get("value", [])
    non_dust = [a for a in accounts if float(a.get("uiAmount") or 0) > dust_threshold]
    return len(non_dust)


def resolve_base_token_address(pool_address: str, network: str,
                                session: requests.Session) -> Optional[str]:
    """
    GeckoTerminal's ranked /pools listing (fetch_ranked_pools) does not
    include the base token's mint address -- only the pool address and a
    display name. The holder-count check needs a real mint address, so for
    any pool that reaches that stage, this resolves it via GeckoTerminal's
    per-pool endpoint, which does include it under relationships.base_token.

    GET /networks/{network}/pools/{pool_address}?include=base_token
    This shares ONE rate limiter instance (gecko_limiter) with
    fetch_ranked_pools AND fetch_ohlcv -- all three are GeckoTerminal
    calls against the same 30/min ceiling, and must draw from a single
    shared budget, not one budget per function (see gecko_limiter's
    definition for why using separate instances was a real bug, found and
    fixed during a later audit of this file). This adds real call volume
    for every pool that clears the core filters. See README.md's
    efficiency notes for why this only runs on pools that already passed
    cheaper checks.
    """
    gecko_limiter.wait()
    url = f"{GECKOTERMINAL_POOLS_URL.format(network=network)}/{pool_address}"
    resp = session.get(
        url, params={"include": "base_token"}, timeout=15,
        headers={"Accept": "application/json;version=20230302"},
    )
    if resp.status_code == 429:
        # Was silently returning None with no log or backoff -- inconsistent
        # with every other rate-limit handler in this file (fetch_ranked_pools,
        # fetch_ohlcv, dexscreener_search, get_coingecko_holder_count all log
        # and back off 10-15s on a 429). Fixed to match, and now also logs
        # full headers via log_429_and_get_backoff -- see that function's
        # docstring for why.
        backoff = log_429_and_get_backoff(
            resp, f"GeckoTerminal per-pool endpoint (pool={pool_address})", default_backoff_sec=15,
        )
        time.sleep(backoff)
        return None
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    payload = resp.json()
    try:
        included = payload.get("included") or []
        for item in included:
            if item.get("type") == "token":
                return item["attributes"]["address"]
    except (KeyError, TypeError):
        pass
    return None


# --------------------------------------------------------------------------
# Static cache persistence (tier 1 only -- see module docstring; the
# formerly-tier-3 emailed-suppression set was removed on request)
# --------------------------------------------------------------------------

def load_static_cache(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        log.warning("static_cache at %s is corrupt; starting fresh", path)
        return {}


def save_static_cache(path: str, cache: dict) -> None:
    Path(path).write_text(json.dumps(cache, sort_keys=True))


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def send_match_email(cfg: Config, matches: list[dict]) -> None:
    password = os.environ.get(cfg.smtp_password_env)
    if not password:
        raise RuntimeError(
            f"Environment variable {cfg.smtp_password_env!r} is not set. "
            f"Set it to a Gmail App Password (not your normal password) -- "
            f"see README.md for how to generate one, and note Google Workspace "
            f"accounts must use OAuth2 instead of an App Password."
        )

    # Email addresses are read from environment variables, not config.yaml,
    # so they never sit in plaintext in a (possibly public) git repo -- same
    # reasoning and same pattern as smtp_password_env above. config.yaml
    # holds only the NAME of each env var; the actual address is set as a
    # GitHub Actions secret (or local env var) at runtime.
    smtp_user = os.environ.get(cfg.smtp_user_env)
    if not smtp_user:
        raise RuntimeError(
            f"Environment variable {cfg.smtp_user_env!r} is not set. "
            f"Set it to the Gmail address you're sending FROM (the one the "
            f"App Password belongs to) -- see README.md."
        )

    email_from = os.environ.get(cfg.email_from_env)
    if not email_from:
        raise RuntimeError(
            f"Environment variable {cfg.email_from_env!r} is not set. "
            f"Set it to the sender address for match emails -- see README.md. "
            f"This is usually the same address as {cfg.smtp_user_env!r}."
        )

    email_to_raw = os.environ.get(cfg.email_to_env)
    if not email_to_raw:
        raise RuntimeError(
            f"Environment variable {cfg.email_to_env!r} is not set. "
            f"Set it to one or more recipient addresses, comma-separated for "
            f"multiple recipients (e.g. 'you@example.com,other@example.com') "
            f"-- see README.md."
        )
    email_to = [addr.strip() for addr in email_to_raw.split(",") if addr.strip()]
    if not email_to:
        raise RuntimeError(
            f"Environment variable {cfg.email_to_env!r} is set but contains no "
            f"valid addresses after parsing (got {email_to_raw!r}) -- check for "
            f"stray commas or whitespace-only content."
        )

    subject = f"DEX scan: {len(matches)} match(es) found"
    lines = []
    for m in matches:
        base = m["pair"]["baseToken"]
        source = m.get("holder_count_source")
        if source == "coingecko":
            holder_label = "Holder count (CoinGecko)"
        elif source == "solana_rpc":
            holder_label = "Approx top-20 holder count (Solana RPC)"
        else:
            holder_label = "Holder count"
        contract = base.get("address") or "(mint address unresolved -- see pair URL)"
        lines.append(
            f"{base['symbol']} ({base['name']})\n"
            f"  Contract: {contract}\n"
            f"  Pair URL: {m['pair']['url']}\n"
            f"  FDV: ${m['pair'].get('fdv', 0):,.0f}  "
            f"Liquidity: ${m['pair'].get('liquidity', {}).get('usd', 0):,.0f}  "
            f"24h Vol: ${m['pair'].get('volume', {}).get('h24', 0):,.0f}\n"
            f"  {holder_label}: {m.get('holder_count', 'not checked')}\n"
        )
    body = "\n".join(lines) if lines else "No matches."

    msg = MIMEMultipart()
    msg["From"] = email_from
    msg["To"] = ", ".join(email_to)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    context = ssl.create_default_context()
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(smtp_user, password)
        server.sendmail(email_from, email_to, msg.as_string())
    log.info("Sent match email to %s (%d matches)", email_to, len(matches))


# --------------------------------------------------------------------------
# Main scan cycle
# --------------------------------------------------------------------------

def run_once(cfg: Config) -> None:
    session = requests.Session()
    now_utc = dt.datetime.now(dt.timezone.utc)
    static_cache = load_static_cache(cfg.static_cache_path)

    # Reset 429 pattern tracking for this run -- see log_429_pattern_summary().
    # Explicit reset rather than relying on a fresh process each time, in
    # case this script is ever run via --loop on a long-lived process,
    # where the module-level list would otherwise persist across cycles.
    _429_observations.clear()

    # --- Discovery: GeckoTerminal ranked /pools, swept across every
    # configured rank_by metric and page. Every pool discovered this way
    # gets its MUTABLE fields fresh from this response -- no caching here,
    # per the module docstring's tier-2 rule.
    all_pools: dict[str, dict] = {}
    for rank_by in cfg.rank_by:
        for page in range(1, cfg.max_pages_per_rank + 1):
            try:
                raw_pools = fetch_ranked_pools(cfg.network, rank_by, page, session)
            except requests.RequestException as e:
                # A single failed request (network error, unexpected non-2xx
                # status not already handled inside fetch_ranked_pools, etc.)
                # must not take down the whole weekly cycle -- log it, stop
                # sweeping THIS metric's pages, and move on to the next
                # configured rank_by. Other metrics/pages may still succeed.
                log.error("GeckoTerminal ranked discovery failed for rank_by=%r page=%d: %s",
                          rank_by, page, e)
                break
            if raw_pools is None:
                break  # rate-limited or bad sort value; stop this metric, try the next
            if not raw_pools:
                break  # ran out of pages
            for raw in raw_pools:
                normalized = normalize_gecko_pool(raw)
                if normalized:
                    all_pools[normalized["pairAddress"]] = normalized  # de-dupe across metrics/pages

    log.info("Fetched %d unique pool(s) via GeckoTerminal ranked discovery (%d metric(s) x up to %d page(s))",
              len(all_pools), len(cfg.rank_by), cfg.max_pages_per_rank)

    # --- Optional secondary discovery: DexScreener keyword search, merged
    # in on top of the ranked results. Also fully fresh, no caching.
    # NOTE (found during audit): the `addr not in all_pools` check below
    # can never actually catch a pool that GeckoTerminal already
    # discovered, because the two sources use structurally different pair-
    # address formats -- GeckoTerminal's pairAddress is "solana_<address>"
    # (see normalize_gecko_pool), DexScreener's is a bare pool address with
    # no network prefix. So this dedup only prevents double-counting WITHIN
    # DexScreener's own results across multiple search_terms, not actual
    # cross-source duplicates. In practice this means: if both sources
    # surface the same real pool, it appears TWICE in all_pools under two
    # different keys, and gets fully evaluated (and could be emailed)
    # twice. Not a crash risk and not silently wrong data, but worth
    # knowing if you see what looks like the same token in two lines of a
    # match email with different-looking addresses -- check whether one is
    # prefixed "solana_" and the other isn't.
    for term in cfg.search_terms:
        try:
            pairs = dexscreener_search(term, session)
        except requests.RequestException as e:
            log.error("DexScreener search failed for term=%r: %s", term, e)
            continue
        for p in pairs[: cfg.max_pairs_per_search]:
            addr = p.get("pairAddress")
            if addr and addr not in all_pools:
                all_pools[addr] = p

    log.info("Total %d unique pool(s)/pair(s) after merging discovery sources", len(all_pools))

    # --- Tier-1 static cache: for pools already seen before, this is where
    # the "don't re-fetch data that couldn't have changed" saving actually
    # happens -- NOT by skipping the filter check (that still runs on fresh
    # mutable data below), but by trusting the cached pairCreatedAt/dexId
    # instead of re-deriving them, and by recording new pools' static fields
    # for next cycle's benefit.
    for addr, pool in all_pools.items():
        cached = static_cache.get(addr)
        if cached:
            # Static fields cannot change -- prefer the cached copy over
            # anything freshly parsed, in case this cycle's parse hit an
            # edge case the first one didn't.
            pool["pairCreatedAt"] = cached["pairCreatedAt"]
            pool["dexId"] = cached["dexId"]
        else:
            static_cache[addr] = {
                "pairCreatedAt": pool.get("pairCreatedAt"),
                "dexId": pool.get("dexId"),
            }

    # --- Core filters: evaluated fresh for EVERY pool in this cycle's
    # discovery window, including ones seen (and rejected) in prior cycles.
    # This is the "no potential matches skipped" guarantee -- there is no
    # branch here that skips a pool because it failed before.
    core_pass = []
    for pool in all_pools.values():
        ok, reason = passes_core_filters(pool, cfg, now_utc)
        if ok:
            core_pass.append(pool)
        else:
            log.debug("Rejected %s: %s", pool.get("baseToken", {}).get("symbol"), reason)

    log.info("%d pool(s) passed core filters this cycle", len(core_pass))

    matches = []
    for pool in core_pass:
        pair_addr = pool["pairAddress"]

        # NOTE: there is intentionally no "already emailed before" skip here.
        # That suppression was removed on request -- a pool that matches
        # every filter is emailed every cycle it still matches, including
        # pools that also matched last cycle. See module docstring.

        result = {"pair": pool}

        if cfg.check_green_candles:
            ohlcv_pool_addr = pool.get("poolAddressOnly") or pair_addr
            green = last_n_candles_green(
                ohlcv_pool_addr, cfg.network, cfg.green_candle_timeframe,
                cfg.green_candle_count, session,
            )
            if green is not True:
                log.debug("Rejected %s: green_candle_check=%r", pool["baseToken"]["symbol"], green)
                continue

        # BUG FIX (found during audit): mint address resolution used to
        # live entirely inside the `if cfg.check_holders:` block below,
        # which meant that setting check_holders: false in config.yaml --
        # a legitimate, documented, tested option -- silently resulted in
        # EVERY match email showing "(mint address unresolved -- see pair
        # URL)" instead of a real contract address. Since "email the token
        # contract address matching the filter" is the core original
        # request this whole script was built for, that's a serious bug,
        # not a cosmetic one. Fixed by resolving the mint unconditionally
        # for every pool that reaches this point (i.e. already cleared
        # core filters and the candle check), regardless of whether the
        # holder check itself is enabled.
        mint = pool["baseToken"].get("address")
        if not mint:
            ohlcv_pool_addr = pool.get("poolAddressOnly") or pair_addr
            mint = resolve_base_token_address(ohlcv_pool_addr, cfg.network, session)
            if mint:
                pool["baseToken"]["address"] = mint
        if not mint:
            log.warning(
                "Could not resolve mint address for %s (pair=%s) -- this match "
                "will still be emailed (its data satisfies every configured "
                "filter), but the email will show '(mint address unresolved)' "
                "instead of a real contract address. Check the pair URL in the "
                "email to find the token manually if this happens.",
                pool["baseToken"]["symbol"], pair_addr,
            )

        if cfg.check_holders:
            if not mint:
                log.debug("Rejected %s: could not resolve base token mint address "
                          "(required for the holder check)", pool["baseToken"]["symbol"])
                continue

            if cfg.holder_count_source == "coingecko":
                api_key = os.environ.get(cfg.coingecko_api_key_env)
                holder_count = get_coingecko_holder_count(mint, cfg.network, api_key, session)
                count_label = "holder count"
            elif cfg.holder_count_source == "solana_rpc":
                holder_count = get_top_holder_count(
                    mint, cfg.solana_rpc_url, cfg.holder_dust_threshold, session,
                )
                count_label = "approx top-20 holder count"
            else:
                raise ValueError(
                    f"Unknown holder_count_source={cfg.holder_count_source!r} in config.yaml "
                    f"-- must be 'coingecko' or 'solana_rpc'"
                )

            if holder_count is None:
                log.debug("Rejected %s: %s unavailable", pool["baseToken"]["symbol"], count_label)
                continue
            if not (cfg.min_holders <= holder_count <= cfg.max_holders):
                log.debug(
                    "Rejected %s: %s %d outside [%d, %d]",
                    pool["baseToken"]["symbol"], count_label, holder_count,
                    cfg.min_holders, cfg.max_holders,
                )
                continue
            result["holder_count"] = holder_count
            result["holder_count_source"] = cfg.holder_count_source

        matches.append(result)
        # No emailed.add() here anymore -- see note above and module docstring.
        # This pool will be fully re-evaluated and, if it still matches,
        # emailed again next cycle.

    log.info("%d new match(es) this cycle", len(matches))

    if matches:
        send_match_email(cfg, matches)

    # Logged here, at the true end of the run, so it captures 429s from
    # BOTH discovery (fetch_ranked_pools) AND the later per-candidate-pool
    # checks (fetch_ohlcv, resolve_base_token_address) -- not just
    # discovery alone, since all three functions share the same
    # log_429_and_get_backoff() helper and feed the same tracking list.
    log_429_pattern_summary()

    save_static_cache(cfg.static_cache_path, static_cache)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="Run a single scan cycle and exit")
    parser.add_argument("--loop", action="store_true",
                         help="Run continuously using poll_interval_sec (see module docstring "
                              "for why an external weekly trigger + --once is recommended instead)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)

    if args.loop:
        log.info("Starting loop mode, poll_interval_sec=%d", cfg.poll_interval_sec)
        while True:
            try:
                run_once(cfg)
            except Exception:
                log.exception("Unhandled error in scan cycle; continuing")
            time.sleep(cfg.poll_interval_sec)
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()
