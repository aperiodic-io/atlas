from atlas.cmc_id_prototype import CmcAsset, select_verified_candidate


def test_select_verified_candidate_uses_price_to_disambiguate_same_ticker():
    candidates = [
        CmcAsset(cmc_id=1, symbol="ABC", slug="unrelated-abc", price_usd=20),
        CmcAsset(cmc_id=2, symbol="ABC", slug="right-abc", price_usd=100),
    ]

    selected = select_verified_candidate(candidates, exchange_price=101, threshold=0.05)

    assert selected is not None
    assert selected.asset.cmc_id == 2
    assert selected.relative_difference < 0.05


def test_select_verified_candidate_rejects_ambiguous_same_ticker_prices():
    candidates = [
        CmcAsset(cmc_id=1, symbol="SOL", slug="solana", price_usd=100),
        CmcAsset(cmc_id=2, symbol="SOL", slug="wrapped-solana", price_usd=100.2),
    ]

    assert (
        select_verified_candidate(candidates, exchange_price=100, threshold=0.05)
        is None
    )


def test_select_verified_candidate_rejects_prices_outside_threshold():
    candidates = [CmcAsset(cmc_id=1, symbol="ABC", slug="abc", price_usd=50)]

    assert (
        select_verified_candidate(candidates, exchange_price=100, threshold=0.05)
        is None
    )
