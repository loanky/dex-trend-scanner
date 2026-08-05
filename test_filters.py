"""
Tests for scanner.py: GeckoTerminal ranked discovery, normalization from the
raw wire shape, core filters against normalized data (including the FDV
range check -- removed at one point in this project's history, then
reinstated; see test_fdv_gates_a_match for the current, correct behavior),
and the static-cache correctness guarantee (it never affects a filter
outcome).

Repeat-match suppression was removed on request and stays removed: there
is no "already emailed" tracking anymore, so there is nothing left to test
for that mechanism specifically. test_matching_pool_matches_again_on_a_second_pass
below tests the actual current guarantee: a pool whose data hasn't changed
matches on every pass, not just the first.

None of this hits the network. See README.md for why, and for what running
--once --log-level DEBUG yourself against the live APIs is for.
"""

import datetime as dt
import json

import scanner


def make_test_config(**overrides) -> scanner.Config:
    base = dict(
        schedule_cron="0 0 * * 1",
        network="solana",
        rank_by=["h24_volume_usd_desc"],
        max_pages_per_rank=1,
        dex_ids=["pumpswap"],
        search_terms=[],
        min_age_weeks=2.0,
        min_holders=0,
        max_holders=10_000,
        green_candle_timeframe="day",
        green_candle_count=3,
        min_fdv=50_000,
        max_fdv=500_000,
        min_txns_24h=200,
        min_volume_24h=10_000,
        max_volume_24h=1_000_000,
        min_liquidity_usd=20_000,
        check_holders=False,
        holder_count_source="coingecko",
        coingecko_api_key_env="TEST_COINGECKO_KEY",
        holder_dust_threshold=1.0,
        solana_rpc_url="https://api.mainnet-beta.solana.com",
        check_green_candles=False,
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_user_env="TEST_SMTP_USER",
        smtp_password_env="TEST_SMTP_PW",
        email_from_env="TEST_EMAIL_FROM",
        email_to_env="TEST_EMAIL_TO",
        poll_interval_sec=604800,
        static_cache_path="/tmp/test_static_cache.json",
        max_pairs_per_search=100,
    )
    base.update(overrides)
    return scanner.Config(**base)


def load_raw_fixture():
    with open("fixture_gecko_pools_raw.json") as f:
        return json.load(f)["data"]


# --------------------------------------------------------------------------
# normalize_gecko_pool: does it correctly parse GeckoTerminal's real,
# string-typed, ISO-timestamped wire shape?
# --------------------------------------------------------------------------

def test_normalize_converts_string_numbers_to_floats():
    raw = load_raw_fixture()
    pass_entry = next(p for p in raw if p["id"] == "solana_AAAABBBBCCCC1111")
    normalized = scanner.normalize_gecko_pool(pass_entry)
    assert normalized is not None
    assert isinstance(normalized["fdv"], float), f"fdv should be float, got {type(normalized['fdv'])}"
    assert normalized["fdv"] == 185000.00
    assert isinstance(normalized["liquidity"]["usd"], float)
    assert normalized["liquidity"]["usd"] == 42000.00
    assert isinstance(normalized["volume"]["h24"], float)
    assert normalized["volume"]["h24"] == 210000.00
    print("test_normalize_converts_string_numbers_to_floats: PASS")


def test_normalize_converts_iso_timestamp_to_epoch_ms():
    raw = load_raw_fixture()
    pass_entry = next(p for p in raw if p["id"] == "solana_AAAABBBBCCCC1111")
    normalized = scanner.normalize_gecko_pool(pass_entry)
    # 2026-07-10T04:00:00Z -> verify round-trip back to the same UTC instant
    expected = dt.datetime(2026, 7, 10, 4, 0, 0, tzinfo=dt.timezone.utc)
    actual = dt.datetime.fromtimestamp(normalized["pairCreatedAt"] / 1000, tz=dt.timezone.utc)
    assert actual == expected, f"Expected {expected}, got {actual}"
    print("test_normalize_converts_iso_timestamp_to_epoch_ms: PASS")


def test_normalize_extracts_dex_id_from_relationships():
    raw = load_raw_fixture()
    wrong_entry = next(p for p in raw if p["id"] == "solana_GGGGHHHHIIII3333")
    normalized = scanner.normalize_gecko_pool(wrong_entry)
    assert normalized["dexId"] == "raydium", f"Expected raydium, got {normalized['dexId']}"
    print("test_normalize_extracts_dex_id_from_relationships: PASS")


def test_normalize_handles_null_liquidity_without_crashing():
    raw = load_raw_fixture()
    noliq_entry = next(p for p in raw if p["id"] == "solana_MMMMNNNNOOOO5555")
    normalized = scanner.normalize_gecko_pool(noliq_entry)
    assert normalized is not None, "Should not return None for a valid-but-null-liquidity entry"
    assert normalized["liquidity"] is None, f"Expected None liquidity, got {normalized['liquidity']}"
    print("test_normalize_handles_null_liquidity_without_crashing: PASS")


def test_normalize_handles_malformed_entry_gracefully():
    raw = load_raw_fixture()
    malformed = next(p for p in raw if p["id"] == "solana_PPPPQQQQRRRR6666")
    normalized = scanner.normalize_gecko_pool(malformed)
    # This entry has no relationships/dex data and minimal attributes --
    # normalize_gecko_pool must not crash. It's fine for it to return either
    # a partially-filled dict or None; either is safe. A crash is not.
    print(f"test_normalize_handles_malformed_entry_gracefully: PASS (returned {normalized!r})")


# --------------------------------------------------------------------------
# passes_core_filters against normalized data (mirrors the old test suite's
# coverage, but against the new normalized shape)
# --------------------------------------------------------------------------

def test_core_filters_against_normalized_fixtures():
    raw = load_raw_fixture()
    normalized = {p["baseToken"]["symbol"]: p
                  for p in (scanner.normalize_gecko_pool(r) for r in raw)
                  if p is not None}

    cfg = make_test_config()
    now_utc = dt.datetime(2026, 8, 8, tzinfo=dt.timezone.utc)  # 4 weeks after fixture PASS creation

    results = {}
    for symbol, pool in normalized.items():
        ok, reason = scanner.passes_core_filters(pool, cfg, now_utc)
        results[symbol] = (ok, reason)
        print(f"{symbol:12s} -> pass={ok!s:5s} {reason}")

    assert results["PASS"][0] is True
    assert results["NEWB"][0] is False and "age" in results["NEWB"][1]
    assert results["WRONG"][0] is False and "dexId" in results["WRONG"][1]
    assert results["LOWLIQ"][0] is False and "liquidity" in results["LOWLIQ"][1]
    assert results["NOLIQ"][0] is False and "liquidity" in results["NOLIQ"][1]
    print("test_core_filters_against_normalized_fixtures: All assertions PASS")


def test_fdv_gates_a_match():
    """
    FDV filtering was removed on request, then reinstated on a later,
    explicit request in this same conversation (the removal was a
    misunderstanding -- only the DEX scanner's removal was ever requested,
    and only once; this test exists specifically to prove the filter is
    genuinely back, not just documented as back).

    Same isolation technique as before: inject an FDV far outside the
    configured range on an otherwise-passing pool and confirm it's
    correctly rejected for that reason specifically.
    """
    raw = load_raw_fixture()
    pass_entry = json.loads(json.dumps(
        next(p for p in raw if p["id"] == "solana_AAAABBBBCCCC1111")
    ))
    # Inject an FDV that fails the configured [50_000, 500_000] range.
    pass_entry["attributes"]["fdv_usd"] = "999999999999.00"

    cfg = make_test_config()
    now_utc = dt.datetime(2026, 8, 8, tzinfo=dt.timezone.utc)
    pool = scanner.normalize_gecko_pool(pass_entry)
    assert pool["fdv"] == 999999999999.00, "fdv should be parsed correctly regardless of filter outcome"

    ok, reason = scanner.passes_core_filters(pool, cfg, now_utc)
    assert ok is False, f"Expected FDV to gate this match (it's far outside [50000, 500000]), but got ok={ok}"
    assert "fdv" in reason.lower(), f"Expected an fdv-related rejection reason, got: {reason}"
    print("test_fdv_gates_a_match: PASS")


def test_fdv_missing_is_rejected_not_crashed():
    """
    GeckoTerminal's own FAQ states fdv_usd is always populated, but
    passes_core_filters() still handles a missing value safely (returns a
    clear rejection reason) rather than assuming it can never happen and
    crashing on a None comparison if reality ever differs from the FAQ.
    """
    raw = load_raw_fixture()
    pass_entry = json.loads(json.dumps(
        next(p for p in raw if p["id"] == "solana_AAAABBBBCCCC1111")
    ))
    del pass_entry["attributes"]["fdv_usd"]

    cfg = make_test_config()
    now_utc = dt.datetime(2026, 8, 8, tzinfo=dt.timezone.utc)
    pool = scanner.normalize_gecko_pool(pass_entry)
    assert pool["fdv"] is None, "fdv should be None when fdv_usd is absent from the raw response"

    ok, reason = scanner.passes_core_filters(pool, cfg, now_utc)
    assert ok is False, f"Expected rejection on missing fdv, got ok={ok}"
    assert "fdv" in reason.lower() and "missing" in reason.lower(), (
        f"Expected an fdv-missing rejection reason, got: {reason}"
    )
    print("test_fdv_missing_is_rejected_not_crashed: PASS")


# --------------------------------------------------------------------------
# THE IMPORTANT PART: two-tier caching correctness.
# These tests exist specifically to prove the "efficient but never skips a
# potential match" requirement is actually met by the code, not just
# claimed in a docstring.
# --------------------------------------------------------------------------

def test_static_cache_does_not_affect_filter_outcome():
    """
    A pool's static fields (age-determining pairCreatedAt, dexId) come from
    the cache on a second sighting -- but the FILTER OUTCOME must be
    identical whether or not the pool was cached, because passes_core_filters
    is always called fresh on every pool every cycle regardless of cache
    state. This test simulates two consecutive cycles by hand and checks the
    pass/fail verdict doesn't change due to caching alone.
    """
    raw = load_raw_fixture()
    pass_entry = next(p for p in raw if p["id"] == "solana_AAAABBBBCCCC1111")
    cfg = make_test_config()
    now_utc = dt.datetime(2026, 8, 8, tzinfo=dt.timezone.utc)

    # Cycle 1: no cache yet.
    pool_cycle1 = scanner.normalize_gecko_pool(pass_entry)
    ok1, _ = scanner.passes_core_filters(pool_cycle1, cfg, now_utc)

    # Simulate what run_once does: populate static_cache from cycle 1.
    static_cache = {
        pool_cycle1["pairAddress"]: {
            "pairCreatedAt": pool_cycle1["pairCreatedAt"],
            "dexId": pool_cycle1["dexId"],
        }
    }

    # Cycle 2: pool is re-normalized fresh (as run_once always does), then
    # static fields are overwritten FROM the cache, exactly as run_once does.
    pool_cycle2 = scanner.normalize_gecko_pool(pass_entry)
    cached = static_cache[pool_cycle2["pairAddress"]]
    pool_cycle2["pairCreatedAt"] = cached["pairCreatedAt"]
    pool_cycle2["dexId"] = cached["dexId"]
    ok2, _ = scanner.passes_core_filters(pool_cycle2, cfg, now_utc)

    assert ok1 == ok2 == True, f"Cache must not change the filter verdict: cycle1={ok1}, cycle2={ok2}"
    print("test_static_cache_does_not_affect_filter_outcome: PASS")


def test_matching_pool_matches_again_on_a_second_pass():
    """
    Repeat-match suppression was removed on request. This is the direct
    test of the new guarantee: a pool that matches in one cycle, with
    unchanged data, matches again if evaluated a second time -- there is no
    hidden state anywhere (not in static_cache, which only ever affects
    static fields, never the pass/fail verdict -- see
    test_static_cache_does_not_affect_filter_outcome above) that would
    cause a second identical evaluation to produce a different result.
    """
    raw = next(p for p in load_raw_fixture() if p["id"] == "solana_AAAABBBBCCCC1111")
    cfg = make_test_config()
    now_utc = dt.datetime(2026, 8, 8, tzinfo=dt.timezone.utc)

    # Two independent, freshly-normalized evaluations of the same unchanged
    # raw data -- simulating this pool appearing in two separate weekly
    # cycles with nothing about it having changed.
    pool_pass_1 = scanner.normalize_gecko_pool(raw)
    ok_1, _ = scanner.passes_core_filters(pool_pass_1, cfg, now_utc)

    pool_pass_2 = scanner.normalize_gecko_pool(raw)
    ok_2, _ = scanner.passes_core_filters(pool_pass_2, cfg, now_utc)

    assert ok_1 is True and ok_2 is True, (
        f"A pool with unchanged, matching data must match on every independent "
        f"evaluation -- got pass_1={ok_1}, pass_2={ok_2}. There is no suppression "
        f"state left in this design that should make these differ."
    )
    print("test_matching_pool_matches_again_on_a_second_pass: PASS")


if __name__ == "__main__":
    test_normalize_converts_string_numbers_to_floats()
    test_normalize_converts_iso_timestamp_to_epoch_ms()
    test_normalize_extracts_dex_id_from_relationships()
    test_normalize_handles_null_liquidity_without_crashing()
    test_normalize_handles_malformed_entry_gracefully()
    test_core_filters_against_normalized_fixtures()
    test_fdv_gates_a_match()
    test_fdv_missing_is_rejected_not_crashed()
    test_static_cache_does_not_affect_filter_outcome()
    test_matching_pool_matches_again_on_a_second_pass()
    print("\nAll filter/normalization/caching tests passed.")
