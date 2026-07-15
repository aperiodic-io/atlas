from datetime import UTC, datetime

from integrations.cmc_mappings import CmcMapping, InstrumentInstance, MappingStore


def test_mapping_store_allows_many_instruments_for_one_cmc_id(tmp_path) -> None:
    first_capture = datetime(2026, 1, 1, tzinfo=UTC)
    btc_spot = InstrumentInstance("binance-spot", "BTCUSDT", first_capture)
    btc_perp = InstrumentInstance("binance-futures", "BTCUSDT", first_capture)
    store = MappingStore(tmp_path / "cmc-mappings.json")

    store.upsert(
        CmcMapping(btc_spot, 1, "bitcoin", "approved", "manual", first_capture)
    )
    store.upsert(
        CmcMapping(btc_perp, 1, "bitcoin", "approved", "manual", first_capture)
    )
    store.save()

    restored = MappingStore.load(tmp_path / "cmc-mappings.json")

    assert restored.get(btc_spot).cmc_id == 1
    assert restored.get(btc_perp).cmc_id == 1


def test_mapping_store_treats_a_relisted_symbol_as_a_new_instance(tmp_path) -> None:
    store = MappingStore(tmp_path / "cmc-mappings.json")
    old = InstrumentInstance(
        "binance-spot", "ABCUSDT", datetime(2025, 1, 1, tzinfo=UTC)
    )
    relisted = InstrumentInstance(
        "binance-spot", "ABCUSDT", datetime(2026, 1, 1, tzinfo=UTC)
    )

    store.upsert(
        CmcMapping(old, 10, "old-abc", "cmc_inactive", "historical", old.first_capture)
    )
    store.upsert(
        CmcMapping(
            relisted, 20, "new-abc", "approved", "manual", relisted.first_capture
        )
    )

    assert store.get(old).cmc_id == 10
    assert store.get(relisted).cmc_id == 20
