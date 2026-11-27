"""`atlas.update` CLI: choosing which exchanges a run refreshes."""

from __future__ import annotations

import pytest

from atlas.update import EXCHANGES, parse_args, select_exchanges


def test_defaults_to_every_exchange():
    assert select_exchanges("", "") == EXCHANGES


def test_include_keeps_only_the_named_exchanges():
    assert select_exchanges("kraken-perps,kraken-futures", "") == [
        "kraken-perps",
        "kraken-futures",
    ]


def test_exclude_drops_the_named_exchanges_and_keeps_order():
    selected = select_exchanges("", "kraken-perps, kraken-futures")
    assert "kraken-perps" not in selected
    assert "kraken-futures" not in selected
    assert selected == [e for e in EXCHANGES if not e.startswith("kraken-p") and e != "kraken-futures"]


def test_exclude_with_unknown_exchange_is_rejected():
    # A typo here would silently refresh the exchange on the wrong runner.
    with pytest.raises(ValueError, match="kraken-perp"):
        select_exchanges("", "kraken-perp")


def test_parse_args_reads_exclude():
    assert parse_args(["--exclude", "a,b"]).exclude == "a,b"
