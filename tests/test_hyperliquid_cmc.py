from datetime import UTC, datetime, timedelta
import json

import pytest
import requests

from integrations import hyperliquid as hl
from integrations import cmc_new_symbol_mapping as mapping
from integrations.cmc_id_probe import (
    CmcAsset,
    CmcCatalogue,
    CatalogueDiagnostics,
    PriceObservation,
)


NOW = datetime(2026, 10, 4, tzinfo=UTC)


@pytest.mark.parametrize(
    ("symbol", "expected", "multiplier"),
    [
        ("kPEPE", "PEPE", 1000),
        ("kBONK", "BONK", 1000),
        ("KHYPE", "KHYPE", 1),
        ("UBTC", "UBTC", 1),
        ("hyna:1000PEPE", "PEPE", 1000),
        ("cash:BTC", "BTC", 1),
        ("xyz:STX", "XYZ:STX", 1),
        ("flx:GAS", "FLX:GAS", 1),
    ],
)
def test_explicit_aliases_only(symbol, expected, multiplier):
    assert (
        hl.lookup_symbol(symbol, {"PEPE", "BONK", "BTC", "KHYPE", "UBTC", "STX", "GAS"})
        == expected
    )
    assert hl.contract_multiplier(symbol) == multiplier


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class Session:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(kwargs)
        payload = next(self.payloads)
        if isinstance(payload, Exception):
            raise payload
        return Response(payload)

    def get(self, url, **kwargs):
        return self.post(url, **kwargs)


def spot_payload(price="25", quote="USDC", name="Hyperliquid"):
    return [
        {
            "tokens": [
                {"index": 0, "name": quote},
                {"index": 150, "name": "HYPE", "fullName": name, "tokenId": "0xhype"},
            ],
            "universe": [{"name": "@107", "tokens": [150, 0]}],
        },
        [{"coin": "@107", "markPx": price}],
    ]


def test_spot_indices_and_identity_are_retained():
    session = Session([spot_payload()])
    observations, identities = hl.fetch_spot_market_data(session)
    price = observations["HYPE/USDC"]
    assert price.price == 25
    assert price.quote_currency == "USDC"
    assert price.instrument_id == "@107"
    assert price.observed_at.tzinfo is UTC
    assert identities["HYPE/USDC"]["fullName"] == "Hyperliquid"
    assert identities["HYPE/USDC"]["tokenId"] == "0xhype"


def test_spot_contexts_are_joined_by_coin_and_outcomes_are_ignored():
    payload = spot_payload()
    payload[1].insert(0, {"coin": "#80001", "markPx": "0.3"})
    observations, _ = hl.fetch_spot_market_data(Session([payload]))
    assert observations["HYPE/USDC"].price == 25


def test_missing_spot_context_keeps_identity_but_withholds_price():
    payload = spot_payload()
    payload[1] = []
    observations, identities = hl.fetch_spot_market_data(Session([payload]))
    assert observations == {}
    assert identities["HYPE/USDC"]["tokenId"] == "0xhype"


@pytest.mark.parametrize("price", ["nan", "inf", "-1", "0", None, "garbage"])
def test_invalid_spot_prices_are_not_used(price):
    observations, _ = hl.fetch_spot_market_data(Session([spot_payload(price)]))
    assert observations == {}


def test_non_dollar_spot_quotes_are_not_assumed_to_be_usd():
    observations, _ = hl.fetch_spot_market_data(Session([spot_payload(quote="HYPE")]))
    assert observations == {}


@pytest.mark.parametrize(
    "payload", [{}, [[], []], [{"tokens": [], "universe": [{}]}, []]]
)
def test_malformed_market_envelopes_fail_closed(payload):
    with pytest.raises(hl.HyperliquidError):
        hl.fetch_spot_market_data(Session([payload]), max_attempts=1)


def test_transient_errors_retry(monkeypatch):
    monkeypatch.setattr(hl.time, "sleep", lambda _: None)
    session = Session([requests.Timeout("timeout"), spot_payload()])
    assert hl.fetch_spot_market_data(session)[0]
    assert len(session.calls) == 2


def test_perp_contexts_skip_delisted_and_use_mark_prices():
    session = Session(
        [
            [
                {
                    "universe": [
                        {"name": "BTC"},
                        {"name": "OLD", "isDelisted": True},
                        {"name": "kPEPE"},
                    ]
                },
                [{"markPx": "60000"}, {"markPx": "1"}, {"markPx": "0.01"}],
            ]
        ]
    )
    observations = hl.fetch_perp_market_data(session)
    assert set(observations) == {"BTC", "KPEPE"}
    assert observations["KPEPE"].price == 0.01
    assert observations["KPEPE"].base_units_per_contract == 1
    assert observations["BTC"].quote_currency == "USDC"


def test_builder_request_keeps_namespace():
    session = Session(
        [
            [
                {"collateralToken": 0, "universe": [{"name": "hyna:BTC"}]},
                [{"markPx": "60000"}],
            ]
        ]
    )
    observations = hl.fetch_perp_market_data(session, dex="hyna")
    assert observations["HYNA:BTC"].instrument_id == "hyna:BTC"
    assert session.calls[0]["json"]["dex"] == "hyna"


@pytest.mark.parametrize("collateral", [None, 1, 2, 150])
def test_builder_collateral_is_not_assumed_to_be_usd(collateral):
    session = Session(
        [
            [
                {"collateralToken": collateral, "universe": [{"name": "hyna:BTC"}]},
                [{"markPx": "60000"}],
            ]
        ]
    )
    assert hl.fetch_perp_market_data(session, dex="hyna") == {}


def occurrence(symbol="BTC", exchange="hyperliquid-perps", original_id=None):
    return mapping.SymbolOccurrence(
        exchange, original_id or symbol, symbol, NOW - timedelta(days=1)
    )


def catalogue(symbol="BTC", name="Bitcoin", price=60000):
    return CmcCatalogue(
        (CmcAsset(1, symbol, name.lower(), price, NOW, True, name),),
        CatalogueDiagnostics(1, 1, 1, (), 0, False),
    )


def test_hyperliquid_collection_normalizes_known_multipliers():
    rows = {
        "hyperliquid-perps": [
            {"id": "kPEPE", "symbol": "KPEPE", "first_capture": NOW.isoformat()}
        ]
    }
    assert mapping.collect_new_symbols(rows, {"PEPE"}, NOW)[0].lookup_symbol == "PEPE"


def test_builder_crypto_scope_matches_coverage_and_explicit_override():
    rows = {
        "hyperliquid-perps": [
            {"id": "xyz:STX", "symbol": "XYZ:STX", "first_capture": NOW.isoformat()},
            {"id": "hyna:BTC", "symbol": "HYNA:BTC", "first_capture": NOW.isoformat()},
        ]
    }
    new = mapping.collect_new_symbols(rows, {"BTC", "STX"}, NOW)
    assert [item.lookup_symbol for item in new] == ["BTC"]
    assert mapping.coverage_report(rows, NOW, 1)["rows_in_window_out_of_scope"] == 1
    explicit = mapping.collect_new_symbols(
        rows, {"BTC", "STX"}, NOW, only_symbols=frozenset({"XYZ:STX"})
    )
    assert explicit[0].lookup_symbol == "XYZ:STX"


def test_hyperliquid_instances_resolve_independently():
    rows = {
        "hyperliquid-perps": [
            {"id": "BTC", "symbol": "BTC", "first_capture": NOW.isoformat()},
            {"id": "hyna:BTC", "symbol": "HYNA:BTC", "first_capture": NOW.isoformat()},
        ],
        "hyperliquid-spot": [
            {"id": "BTC/USDC", "symbol": "BTC", "first_capture": NOW.isoformat()}
        ],
    }
    new = mapping.collect_new_symbols(rows, {"BTC"}, NOW)
    assert len(new) == 3
    assert all(len(item.occurrences) == 1 for item in new)


def test_concurrent_perp_mapping_requires_price_agreement_and_alignment():
    new = mapping.NewSymbol("BTC", (occurrence(),))
    windows = {"BTC": (mapping.MappedWindow(1, NOW - timedelta(days=100), None),)}
    for price, observed_at, expected in [
        (60000, NOW, mapping.MatchStatus.APPROVED),
        (1, NOW, mapping.MatchStatus.UNCERTAIN),
        (60000, NOW - timedelta(hours=1), mapping.MatchStatus.UNCERTAIN),
    ]:
        observations = {
            "hyperliquid-perps": {
                "BTC": PriceObservation(
                    price, "USD", observed_at, "hyperliquid-perps", "BTC"
                )
            }
        }
        result = mapping.resolve_new_symbols(
            [new], catalogue(), windows, observations, {}, None
        )[0]
        assert result.status == expected


def test_hyperliquid_does_not_borrow_binance_names_for_spot():
    new = mapping.NewSymbol("BTC", (occurrence("BTC", "hyperliquid-spot", "BTC/USDC"),))
    evidence = mapping.build_symbol_evidence(
        new, catalogue(), {}, {"BTC": {"assetName": "Bitcoin"}}
    )
    assert not evidence.identified_candidates


@pytest.mark.parametrize("exchange", ["hyperliquid-spot", "hyperliquid-perps"])
def test_hyperliquid_requires_its_own_fresh_price_even_for_reuse(exchange):
    new = mapping.NewSymbol("BTC", (occurrence(exchange=exchange),))
    windows = {"BTC": (mapping.MappedWindow(1, NOW - timedelta(days=100), None),)}
    decisions = mapping.resolve_new_symbols([new], catalogue(), windows, {}, {}, None)
    assert decisions[0].status != mapping.MatchStatus.APPROVED


def test_spot_ticker_cannot_inherit_perp_identity():
    new = mapping.NewSymbol("BTC", (occurrence("BTC", "hyperliquid-spot", "BTC/USDC"),))
    windows = {"BTC": (mapping.MappedWindow(1, NOW - timedelta(days=100), None),)}
    observations = {
        "hyperliquid-spot": {
            "BTC/USDC": PriceObservation(60000, "USDC", NOW, "hyperliquid-spot", "@5")
        }
    }
    decision = mapping.resolve_new_symbols(
        [new], catalogue(), windows, observations, {}, None
    )[0]
    assert decision.status != mapping.MatchStatus.APPROVED


def test_spot_full_name_establishes_identity_with_provenance():
    new = mapping.NewSymbol(
        "HYPE", (occurrence("HYPE", "hyperliquid-spot", "HYPE/USDC"),)
    )
    observations = {
        "hyperliquid-spot": {
            "HYPE/USDC": PriceObservation(25, "USDC", NOW, "hyperliquid-spot", "@107")
        }
    }
    identities = {
        "hyperliquid-spot:HYPE/USDC": {"fullName": "Hyperliquid", "tokenId": "0xhype"}
    }
    evidence = mapping.build_symbol_evidence(
        new, catalogue("HYPE", "Hyperliquid", 25), observations, identities
    )
    decision = mapping.decide(evidence, None)
    assert decision.status == mapping.MatchStatus.APPROVED
    assert decision.method == "identity_hyperliquid_full_name"
    assert "hyperliquid" in mapping.build_prompt(evidence)


@pytest.mark.parametrize(
    ("platform", "address", "expected"),
    [
        ("HyperEVM", "0xabc", [1]),
        ("Ethereum", "0xabc", []),
        ("HyperEVM", "0xdef", []),
    ],
)
def test_contract_identity_requires_network_and_address(platform, address, expected):
    metadata = {
        "hyperliquid-spot:HYPE/USDC": {
            "name": "HYPE",
            "evmContract": {"address": "0xAbC"},
        }
    }
    session = Session(
        [
            {
                "data": {
                    "id": 1,
                    "platforms": [
                        {"contractPlatform": platform, "contractAddress": address}
                    ],
                }
            }
        ]
    )
    hl.enrich_contract_identity(
        session, metadata, catalogue("HYPE", "Hyperliquid", 25), min_interval_seconds=0
    )
    assert metadata["hyperliquid-spot:HYPE/USDC"]["cmc_contract_matches"] == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://app.hyperliquid.xyz/explorer/token/0xbaf265ef389da684513d98d68edf4eae",
            [1],
        ),
        ("https://evil.example/explorer/token/0xbaf265ef389da684513d98d68edf4eae", []),
        (
            "https://app.hyperliquid.xyz/explorer/token/0xbaf265ef389da684513d98d68edf4eae?fake=1",
            [],
        ),
        (
            "https://app.hyperliquid.xyz/explorer/token/0x00000000000000000000000000000000",
            [],
        ),
    ],
)
def test_native_token_id_identity_uses_exact_official_explorer_link(url, expected):
    metadata = {
        "hyperliquid-spot:HYPE/USDC": {
            "name": "HYPE",
            "tokenId": "0xbaf265ef389da684513d98d68edf4eae",
            "evmContract": None,
        }
    }
    session = Session(
        [{"data": {"id": 1, "platforms": None, "urls": {"explorer": [url]}}}]
    )
    hl.enrich_contract_identity(
        session, metadata, catalogue("HYPE", "Hyperliquid", 25), min_interval_seconds=0
    )
    assert metadata["hyperliquid-spot:HYPE/USDC"]["cmc_contract_matches"] == expected
    if expected:
        assert (
            metadata["hyperliquid-spot:HYPE/USDC"]["cmc_contract_checks"]["1"][0][
                "source_url"
            ]
            == url
        )


def test_detail_response_cannot_substitute_a_different_cmc_id(monkeypatch):
    metadata = {
        "hyperliquid-spot:HYPE/USDC": {
            "name": "HYPE",
            "evmContract": {"address": "0xabc"},
        }
    }
    session = Session(
        [
            {
                "data": {
                    "id": 999,
                    "platforms": [
                        {"contractPlatform": "HyperEVM", "contractAddress": "0xabc"}
                    ],
                }
            }
        ]
    )
    hl.enrich_contract_identity(
        session,
        metadata,
        catalogue("HYPE", "Hyperliquid", 25),
        max_attempts=1,
        min_interval_seconds=0,
    )
    assert not metadata["hyperliquid-spot:HYPE/USDC"].get("cmc_contract_matches")


def test_exact_contract_approves_without_a_name():
    new = mapping.NewSymbol(
        "HYPE", (occurrence("HYPE", "hyperliquid-spot", "HYPE/USDC"),)
    )
    observations = {
        "hyperliquid-spot": {
            "HYPE/USDC": PriceObservation(25, "USDC", NOW, "hyperliquid-spot", "@107")
        }
    }
    identities = {
        "hyperliquid-spot:HYPE/USDC": {
            "cmc_contract_matches": [1],
            "cmc_contract_checks": {
                "1": [{"contractPlatform": "HyperEVM", "contractAddress": "0xabc"}]
            },
        }
    }
    evidence = mapping.build_symbol_evidence(
        new, catalogue("HYPE", "Hyperliquid", 25), observations, identities
    )
    assert mapping.decide(evidence, None).method == "identity_hyperliquid_contract"


@pytest.mark.parametrize(("price", "offset"), [(100, 0), (25, -1000)])
def test_contract_identity_cannot_override_bad_price_evidence(price, offset):
    new = mapping.NewSymbol(
        "HYPE", (occurrence("HYPE", "hyperliquid-spot", "HYPE/USDC"),)
    )
    observations = {
        "hyperliquid-spot": {
            "HYPE/USDC": PriceObservation(
                price,
                "USDC",
                NOW + timedelta(seconds=offset),
                "hyperliquid-spot",
                "@107",
            )
        }
    }
    identities = {"hyperliquid-spot:HYPE/USDC": {"cmc_contract_matches": [1]}}
    evidence = mapping.build_symbol_evidence(
        new, catalogue("HYPE", "Hyperliquid", 25), observations, identities
    )
    assert mapping.decide(evidence, None).status != mapping.MatchStatus.APPROVED


def market_payload(*markets, total=None):
    return {
        "data": {
            "id": 8112,
            "slug": "hyperliquid",
            "numMarketPairs": len(markets) if total is None else total,
            "marketPairs": list(markets),
        }
    }


def market(symbol="BTC", cmc_id=1, **extra):
    return {
        "exchangeId": 8112,
        "category": "perpetual",
        "marketId": 1351165,
        "marketUrl": f"https://app.hyperliquid.xyz/trade/{symbol}",
        "baseSymbol": symbol,
        "baseCurrencyId": cmc_id,
        **extra,
    }


def test_cmc_market_identity_retains_exact_instrument_link():
    identities = hl.fetch_cmc_perp_identities(Session([market_payload(market())]))
    assert identities["hyperliquid-perps:BTC"]["cmc_market_ids"] == [1]
    assert identities["hyperliquid-perps:BTC"]["cmc_markets"][0]["marketUrl"].endswith(
        "/trade/BTC"
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"exchangeId": 1},
        {"category": "spot"},
        {"baseCurrencyId": True},
        {"marketUrl": "https://evil.example/trade/BTC"},
        {"marketUrl": "https://app.hyperliquid.xyz/trade/UBTC"},
        {"marketUrl": "https://app.hyperliquid.xyz/trade/BTC/USDC"},
        {"marketUrl": "https://app.hyperliquid.xyz/trade/BTC?fake=1"},
    ],
)
def test_unrelated_market_cannot_establish_identity(extra):
    assert (
        hl.fetch_cmc_perp_identities(Session([market_payload(market(**extra))])) == {}
    )


def test_cmc_market_pagination_and_conflicting_ids_are_preserved():
    session = Session(
        [market_payload(market(), total=2), market_payload(market(cmc_id=2), total=2)]
    )
    identities = hl.fetch_cmc_perp_identities(session, page_size=1)
    assert identities["hyperliquid-perps:BTC"]["cmc_market_ids"] == [1, 2]
    assert session.calls[1]["params"]["start"] == 2


def test_cmc_market_pagination_cannot_claim_completion_from_an_empty_page():
    with pytest.raises(hl.HyperliquidError, match="incomplete"):
        hl.fetch_cmc_perp_identities(Session([market_payload(total=1)]))


@pytest.mark.parametrize("payload", [{}, {"data": {"id": 1}}, market_payload(total=-1)])
def test_invalid_cmc_market_envelopes_are_unusable(payload):
    with pytest.raises(hl.HyperliquidError):
        hl.fetch_cmc_perp_identities(Session([payload]), max_attempts=1)


def test_cmc_market_http_failure_retries_and_failure_is_bounded(monkeypatch):
    monkeypatch.setattr(hl.time, "sleep", lambda _: None)
    session = Session([requests.Timeout(), market_payload(market())])
    assert hl.fetch_cmc_perp_identities(session)
    assert len(session.calls) == 2
    with pytest.raises(hl.HyperliquidError):
        hl.fetch_cmc_perp_identities(Session([requests.Timeout()]), max_attempts=1)


def test_contract_details_are_cached_across_quote_pairs():
    token = {"name": "HYPE", "evmContract": {"address": "0xabc"}}
    identities = {
        "hyperliquid-spot:HYPE/USDC": dict(token),
        "hyperliquid-spot:HYPE/USDH": dict(token),
    }
    session = Session(
        [
            {
                "data": {
                    "id": 1,
                    "platforms": [
                        {"contractPlatform": "HyperEVM", "contractAddress": "0xabc"}
                    ],
                }
            }
        ]
    )
    hl.enrich_contract_identity(
        session,
        identities,
        catalogue("HYPE", "Hyperliquid", 25),
        min_interval_seconds=0,
    )
    assert len(session.calls) == 1
    assert all(value["cmc_contract_matches"] == [1] for value in identities.values())


def test_detail_lookup_skips_candidates_that_already_fail_the_price_check():
    identities = {
        "hyperliquid-spot:HYPE/USDC": {
            "name": "HYPE",
            "evmContract": {"address": "0xabc"},
        }
    }
    session = Session([])
    observations = {
        "HYPE/USDC": PriceObservation(1, "USDC", NOW, "hyperliquid-spot", "@107")
    }
    hl.enrich_contract_identity(
        session,
        identities,
        catalogue("HYPE", "Hyperliquid", 25),
        observations=observations,
    )
    assert session.calls == []
    assert identities["hyperliquid-spot:HYPE/USDC"]["cmc_contract_matches"] == []


@pytest.mark.parametrize(
    "change",
    ["duplicate_token", "missing_token", "duplicate_context", "duplicate_pair"],
)
def test_corrupt_spot_joins_do_not_silently_choose_an_identity(change):
    payload = spot_payload()
    if change == "duplicate_token":
        payload[0]["tokens"].append(payload[0]["tokens"][0])
    elif change == "missing_token":
        payload[0]["universe"][0]["tokens"] = [999, 0]
    elif change == "duplicate_context":
        payload[1].append(payload[1][0])
    else:
        payload[0]["universe"].append(payload[0]["universe"][0])
    with pytest.raises(hl.HyperliquidError):
        hl.fetch_spot_market_data(Session([payload]), max_attempts=1)


def test_explicit_perp_identity_does_not_override_an_untrustworthy_catalogue():
    from dataclasses import replace

    new = mapping.NewSymbol("BTC", (occurrence(),))
    incomplete = replace(
        catalogue(), diagnostics=CatalogueDiagnostics(10, 1, 1, (), 0, False)
    )
    observations = {
        "hyperliquid-perps": {
            "BTC": PriceObservation(60000, "USD", NOW, "hyperliquid-perps", "BTC")
        }
    }
    identities = {"hyperliquid-perps:BTC": {"cmc_market_ids": [1]}}
    evidence = mapping.build_symbol_evidence(new, incomplete, observations, identities)
    assert mapping.decide(evidence, None).status == mapping.MatchStatus.UNCERTAIN


def test_perp_identity_does_not_override_an_approved_ledger_id(tmp_path):
    from integrations.cmc_mappings import CmcMapping, MappingStore

    new = mapping.NewSymbol("BTC", (occurrence(),))
    store = MappingStore(tmp_path / "ledger.json")
    store.upsert(
        CmcMapping(
            new.occurrences[0].instance, 2, "old-bitcoin", "approved", "manual", NOW
        )
    )
    observations = {
        "hyperliquid-perps": {
            "BTC": PriceObservation(60000, "USD", NOW, "hyperliquid-perps", "BTC")
        }
    }
    identities = {"hyperliquid-perps:BTC": {"cmc_market_ids": [1]}}
    evidence = mapping.build_symbol_evidence(new, catalogue(), observations, identities)
    decision = mapping.decide(evidence, None)
    applicable, conflicts = mapping.partition_conflicts(store, [decision])
    assert applicable == []
    assert conflicts == [decision]
    assert store.get(new.occurrences[0].instance).cmc_id == 2


def test_id_quote_refresh_preserves_identity_and_real_source_time():
    session = Session(
        [
            {
                "data": {
                    "id": 1,
                    "statistics": {"price": "61000"},
                    "latestUpdateTime": NOW.isoformat(),
                }
            }
        ]
    )
    refreshed = hl.refresh_cmc_id_quotes(
        session, catalogue(), {1}, min_interval_seconds=0
    )
    assert refreshed.assets[0].price_usd == 61000
    assert refreshed.assets[0].last_updated == NOW
    assert refreshed.assets[0].cmc_id == 1
    assert refreshed.assets[0].slug == "bitcoin"
    assert session.calls[0]["params"]["id"] == 1


@pytest.mark.parametrize(
    ("price", "timestamp", "cmc_id"),
    [
        ("nan", NOW.isoformat(), 1),
        ("inf", NOW.isoformat(), 1),
        (0, NOW.isoformat(), 1),
        (-1, NOW.isoformat(), 1),
        (60000, "2026-10-04T00:00:00", 1),
        (60000, None, 1),
        (60000, NOW.isoformat(), 999),
    ],
)
def test_invalid_id_quotes_are_withheld(price, timestamp, cmc_id):
    failed = set()
    session = Session(
        [
            {
                "data": {
                    "id": cmc_id,
                    "statistics": {"price": price},
                    "latestUpdateTime": timestamp,
                }
            }
        ]
    )
    original = catalogue()
    refreshed = hl.refresh_cmc_id_quotes(
        session,
        original,
        {1},
        max_attempts=1,
        min_interval_seconds=0,
        failed_ids=failed,
    )
    assert refreshed == original
    assert failed == {1}


def test_failed_quote_refresh_withholds_mapping_even_if_old_quote_agrees():
    new = mapping.NewSymbol("BTC", (occurrence(),))
    identities = {
        "hyperliquid-perps:BTC": {"cmc_market_ids": [1], "cmc_quote_failed_ids": [1]}
    }
    observations = {
        "hyperliquid-perps": {
            "BTC": PriceObservation(60000, "USD", NOW, "hyperliquid-perps", "BTC")
        }
    }
    evidence = mapping.build_symbol_evidence(new, catalogue(), observations, identities)
    assert mapping.decide(evidence, None).status == mapping.MatchStatus.UNCERTAIN


def test_aligned_candle_retains_actual_close_time_and_contract_units():
    target = datetime.now(UTC) - timedelta(minutes=5)
    start = int(target.timestamp() // 60 * 60000)
    original = PriceObservation(
        0.01, "USDC", datetime.now(UTC), "hyperliquid-perps", "kPEPE"
    )
    session = Session(
        [[{"s": "kPEPE", "i": "1m", "t": start, "T": start + 59999, "c": "0.009"}]]
    )
    candle = hl.fetch_aligned_candle(session, original, target)
    assert candle.price == 0.009
    assert candle.base_units_per_contract == 1
    assert candle.observed_at == datetime.fromtimestamp((start + 59999) / 1000, tz=UTC)
    assert session.calls[0]["json"]["req"]["coin"] == "kPEPE"


@pytest.mark.parametrize(
    "override",
    [
        {"s": "OTHER"},
        {"i": "5m"},
        {"T": 0},
        {"t": 0},
        {"c": "nan"},
        {"c": "0"},
    ],
)
def test_candle_must_identify_the_exact_closed_interval(override):
    target = datetime.now(UTC) - timedelta(minutes=5)
    start = int(target.timestamp() // 60 * 60000)
    row = {
        "s": "BTC",
        "i": "1m",
        "t": start,
        "T": start + 59999,
        "c": "60000",
        **override,
    }
    price = PriceObservation(
        60000, "USDC", datetime.now(UTC), "hyperliquid-perps", "BTC"
    )
    assert hl.fetch_aligned_candle(Session([[row]]), price, target) is None


@pytest.mark.parametrize("offset", [timedelta(minutes=1), -timedelta(hours=2)])
def test_candle_does_not_turn_future_or_ancient_quotes_into_evidence(offset):
    session = Session([])
    price = PriceObservation(
        60000, "USDC", datetime.now(UTC), "hyperliquid-perps", "BTC"
    )
    assert hl.fetch_aligned_candle(session, price, datetime.now(UTC) + offset) is None
    assert session.calls == []


def test_aligned_candle_fallback_preserves_the_mapping_tolerance(monkeypatch):
    new = mapping.NewSymbol("BTC", (occurrence(),))
    observations = {
        "hyperliquid-perps": {
            "BTC": PriceObservation(
                60000, "USDC", NOW + timedelta(minutes=10), "hyperliquid-perps", "BTC"
            )
        }
    }
    identities = {"hyperliquid-perps:BTC": {"cmc_market_ids": [1]}}
    monkeypatch.setattr(
        hl,
        "fetch_aligned_candle",
        lambda _session, _price, _target: PriceObservation(
            60000, "USDC", NOW + timedelta(seconds=59), "hyperliquid-perps", "BTC"
        ),
    )
    mapping._align_hyperliquid_prices([new], identities, catalogue(), observations, 180)
    evidence = mapping.build_symbol_evidence(new, catalogue(), observations, identities)
    assert mapping.decide(evidence, None).status == mapping.MatchStatus.APPROVED
    assert (
        identities["hyperliquid-perps:BTC"]["hyperliquid_price_source"]["type"]
        == "candleSnapshot"
    )
    strict = mapping.build_symbol_evidence(
        new,
        catalogue(),
        observations,
        identities,
        max_timestamp_skew=timedelta(seconds=10),
    )
    assert mapping.decide(strict, None).status == mapping.MatchStatus.UNCERTAIN


def test_cmc_market_alias_can_resolve_only_an_id_in_the_catalogue():
    new = mapping.NewSymbol("OLD", (occurrence("OLD"),))
    observations = {
        "hyperliquid-perps": {
            "OLD": PriceObservation(60000, "USD", NOW, "hyperliquid-perps", "OLD")
        }
    }
    for cmc_id, expected in [
        (1, mapping.MatchStatus.APPROVED),
        (999, mapping.MatchStatus.UNMAPPED),
    ]:
        identities = {"hyperliquid-perps:OLD": {"cmc_market_ids": [cmc_id]}}
        evidence = mapping.build_symbol_evidence(
            new, catalogue(), observations, identities
        )
        assert mapping.decide(evidence, None).status == expected


def test_spot_and_perp_run_is_reviewable_dry_run_is_byte_preserving_and_rerun_is_idempotent(
    tmp_path, monkeypatch
):
    (tmp_path / "hyperliquid-perps.json").write_text(
        json.dumps(
            [
                {
                    "id": "BTC",
                    "symbol": "BTC",
                    "first_capture": (NOW - timedelta(days=1)).isoformat(),
                }
            ]
        )
    )
    (tmp_path / "hyperliquid-spot.json").write_text(
        json.dumps(
            [
                {
                    "id": "HYPE/USDC",
                    "symbol": "HYPE",
                    "first_capture": (NOW - timedelta(days=1)).isoformat(),
                }
            ]
        )
    )
    assets = (*catalogue().assets, *catalogue("HYPE", "Hyperliquid", 25).assets)
    # Distinct stable IDs for the two assets.
    from dataclasses import replace

    cmc = CmcCatalogue(
        (assets[0], replace(assets[1], cmc_id=32196)),
        CatalogueDiagnostics(2, 2, 2, (), 0, False),
    )
    monkeypatch.setattr(mapping, "fetch_cmc_catalogue", lambda _: cmc)
    monkeypatch.setattr(
        hl,
        "fetch_spot_market_data",
        lambda _: (
            {
                "HYPE/USDC": PriceObservation(
                    25, "USDC", NOW, "hyperliquid-spot", "@107"
                )
            },
            {"HYPE/USDC": {"fullName": "Hyperliquid", "tokenId": "0xhype"}},
        ),
    )
    monkeypatch.setattr(
        hl,
        "fetch_perp_market_data",
        lambda _, **_kwargs: {
            "BTC": PriceObservation(60000, "USD", NOW, "hyperliquid-perps", "BTC")
        },
    )
    monkeypatch.setattr(
        hl,
        "fetch_cmc_perp_identities",
        lambda _: {"hyperliquid-perps:BTC": {"cmc_market_ids": [1]}},
    )
    monkeypatch.setattr(hl, "enrich_contract_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        hl, "refresh_cmc_id_quotes", lambda _, catalogue, _ids, **_kwargs: catalogue
    )
    monkeypatch.setattr(
        mapping,
        "fetch_public_assets",
        lambda: pytest.fail("a Hyperliquid-only run must not call Binance"),
    )
    original = {path: path.read_bytes() for path in tmp_path.glob("*.json")}
    kwargs = {
        "data_dir": tmp_path,
        "exchanges": ("hyperliquid-perps", "hyperliquid-spot"),
        "use_llm": False,
        "now": NOW,
    }
    preview = mapping.run(**kwargs, dry_run=True)
    assert preview["approved"] == 2
    assert {path: path.read_bytes() for path in original} == original
    assert not (tmp_path / "cmc_mappings.json").exists()
    result = mapping.run(**kwargs)
    assert result["rows_updated"] == {"hyperliquid-perps": 1, "hyperliquid-spot": 1}
    store = mapping.MappingStore.load(tmp_path / "cmc_mappings.json")
    assert len(store.mappings) == 2
    assert all(
        item.status == "approved" and item.evidence["hyperliquid_assets"]
        for item in store.mappings
    )
    assert mapping.run(**kwargs)["has_changes"] is False


def test_apply_decisions_only_touches_the_instrument_instance():
    old = occurrence()
    evidence = mapping.build_symbol_evidence(
        mapping.NewSymbol("BTC", (old,)), catalogue(), {}
    )
    decision = mapping.Decision(
        evidence,
        mapping.MatchStatus.APPROVED,
        1,
        "bitcoin",
        "manual",
        "high",
        "reviewed",
    )
    rows = {
        "hyperliquid-perps": [
            {
                "id": "BTC",
                "symbol": "BTC",
                "first_capture": old.first_capture.isoformat(),
            },
            {"id": "BTC", "symbol": "BTC", "first_capture": NOW.isoformat()},
        ]
    }
    mapping.apply_decisions(rows, [decision])
    assert rows["hyperliquid-perps"][0]["cmc_id"] == 1
    assert "cmc_id" not in rows["hyperliquid-perps"][1]
