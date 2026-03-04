from integrations.arc import (
    _atlas_bucket,
    _candidate_keys,
    _instrument_bucket,
    _pick_best_match,
)


def test_candidate_keys_normalizes_case_and_separators() -> None:
    upper, compact = _candidate_keys("btc-usdt_swap")
    assert upper == "BTC-USDT_SWAP"
    assert compact == "BTCUSDTSWAP"


def test_bucket_mapping() -> None:
    assert _atlas_bucket({"contract_type": "spot"}) == "spot"
    assert _atlas_bucket({"type": "perpetual"}) == "perpetual"
    assert _atlas_bucket({"contract_type": "future"}) == "future"
    assert _atlas_bucket({"contract_type": "option"}) is None

    assert _instrument_bucket({"instrumentType": "spot"}) == "spot"
    assert _instrument_bucket({"instrumentType": "futures", "contractType": "perpetual"}) == "perpetual"
    assert _instrument_bucket({"instrumentType": "futures", "contractType": "weekly"}) == "future"


def test_pick_best_match_prefers_exact_id_and_bucket() -> None:
    row = {"id": "BTC-USDT-SWAP", "contract_type": "perpetual"}
    candidates = [
        {
            "nativeInstrument": "BTC-USDT-SWAP",
            "normalizedInstrument": "BTC-USDT-SWAP",
            "instrumentType": "futures",
            "contractType": "perpetual",
            "assetArcId": "AMB:BTC000000000",
            "active": True,
        },
        {
            "nativeInstrument": "BTC-USDT-240329",
            "normalizedInstrument": "BTC-USDT-240329",
            "instrumentType": "futures",
            "contractType": "weekly",
            "assetArcId": "AMB:BTC000000000",
            "active": True,
        },
    ]
    result = _pick_best_match(row, candidates)
    assert not result.ambiguous
    assert result.instrument is not None
    assert result.instrument["nativeInstrument"] == "BTC-USDT-SWAP"


def test_pick_best_match_flags_ambiguous_tie() -> None:
    row = {"id": "FOOBAR", "contract_type": "spot"}
    candidates = [
        {
            "nativeInstrument": "FOOBAR",
            "normalizedInstrument": "FOOBAR",
            "instrumentType": "spot",
            "assetArcId": "AMB:FOO000000001",
            "active": True,
        },
        {
            "nativeInstrument": "FOOBAR",
            "normalizedInstrument": "FOOBAR",
            "instrumentType": "spot",
            "assetArcId": "AMB:BAR000000001",
            "active": True,
        },
    ]
    result = _pick_best_match(row, candidates)
    assert result.ambiguous
    assert result.instrument is None
