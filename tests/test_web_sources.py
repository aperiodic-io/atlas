"""Parsing and choosing what CoinMarketCap, Wikidata, Wikipedia and CoinGecko say."""

from __future__ import annotations

from integrations.web import sources
from integrations.web.sources import ListedCoin

BLOCKED = ("linktr.ee", "gitbook.io", "pump.fun", "wikipedia.org", "twitter.com")


def coin(
    cmc_id: int, symbol: str, rank: int | None, added: str = "2020-01-01T00:00:00Z"
) -> ListedCoin:
    return ListedCoin(
        cmc_id=cmc_id, name=f"coin{cmc_id}", symbol=symbol, rank=rank, date_added=added
    )


def test_a_symbol_match_needs_a_unique_or_clearly_dominant_coin() -> None:
    # Why: dozens of tokens reuse famous symbols; the real coin must win, but two
    # real coins with one symbol must not be guessed (a wrong site is worse than none).
    listing = [
        coin(1, "PEPE", 30),
        coin(2, "PEPE", 4000),
        coin(3, "PEPE", None),
        coin(10, "MOVE", 200),
        coin(11, "MOVE", 600),
        coin(20, "OBSC", 3000),
        coin(21, "OBSC", 9000),
        coin(40, "ONLY", 7000),
    ]
    assert sources.resolve_symbol("PEPE", listing, None) == (1, "cmc_symbol_dominant")
    assert sources.resolve_symbol("MOVE", listing, None)[1].startswith("ambiguous")
    assert sources.resolve_symbol("OBSC", listing, None)[0] is None
    assert sources.resolve_symbol("ONLY", listing, None) == (40, "cmc_symbol_unique")
    assert sources.resolve_symbol("NONE", listing, None) == (
        None,
        "no CoinMarketCap coin with this symbol",
    )


def test_a_symbol_reused_after_atlas_delisted_it_does_not_match() -> None:
    # Why: Atlas's GPT contracts ended in 2024; a coin that took GPT in 2025 is
    # not that asset, and its site would be published as GPT's.
    listing = [coin(50, "GPT", 4000, "2025-03-01T00:00:00Z")]
    cmc_id, reason = sources.resolve_symbol("GPT", listing, "2024-06-30")
    assert cmc_id is None
    assert reason.startswith("reused symbol")
    assert sources.resolve_symbol("GPT", listing, None) == (50, "cmc_symbol_unique")


def test_tokenised_stocks_fan_tokens_and_pre_ipo_tokens_borrow_their_site() -> None:
    assert sources.borrowed_site("Adobe Tokenized bStocks")
    assert sources.borrowed_site("AMD Tokenised Stock (xStock)")
    assert sources.borrowed_site("Santos FC Fan Token")
    assert sources.borrowed_site("OpenAI\nPre-IPO stock (Republic)")
    assert sources.borrowed_site("Anything", ["DeFi", "Tokenized Stock"])
    assert not sources.borrowed_site("Ondo", ["DeFi", "Real World Assets Protocols"])
    assert not sources.borrowed_site("PAX Gold")
    assert not sources.borrowed_site("Chiliz")


def test_website_is_the_first_one_off_blocked_hosts() -> None:
    # Why: CMC often lists a Linktree, a docs page or a launchpad first; those
    # rank the platform, not the asset.
    assert (
        sources.choose_website(
            ["https://linktr.ee/foo", "https://app.foo.xyz/en"], BLOCKED
        )
        == "foo.xyz"
    )
    assert (
        sources.choose_website(
            ["https://foo.gitbook.io", "https://pump.fun/coin/abc"], BLOCKED
        )
        is None
    )
    assert (
        sources.choose_website(
            ["https://www.ethereum.org/", "https://en.wikipedia.org/wiki/Ethereum"],
            BLOCKED,
        )
        == "ethereum.org"
    )


def test_parses_cmc_detail_and_listing() -> None:
    detail = sources.parse_cmc_detail(
        {
            "data": {
                "id": 1027,
                "name": "Ethereum",
                "symbol": "ETH",
                "slug": "ethereum",
                "status": "active",
                "urls": {
                    "website": ["https://www.ethereum.org/"],
                    "twitter": ["https://twitter.com/ethereum"],
                },
                "statistics": {"rank": 2},
            }
        },
        1027,
    )
    assert detail == {
        "name": "Ethereum",
        "symbol": "ETH",
        "slug": "ethereum",
        "status": "active",
        "websites": ["https://www.ethereum.org/"],
        "rank": 2,
    }
    coins, total = sources.parse_cmc_listing(
        {
            "data": {
                "cryptoCurrencyList": [
                    {
                        "id": 1,
                        "name": "Bitcoin",
                        "symbol": "BTC",
                        "cmcRank": 1,
                        "dateAdded": "2010-07-13T00:00:00.000Z",
                    }
                ],
                "totalCount": "8124",
            }
        }
    )
    assert (coins[0].cmc_id, coins[0].rank, total) == (1, 1, 8124)


def test_ticker_shape_picks_the_exchange() -> None:
    assert sources.exchange_set("TSLA") == "US"
    assert sources.exchange_set("005930") == "KRX"
    assert sources.exchange_set("7203") == "TOKYO"
    # Company names never go to a ticker query (a 6-letter name matching a US
    # ticker would be a different company).
    for ticker in ("TOYOTA", "HK0700", "BRK.B", "CAT.EQUITY"):
        assert sources.exchange_set(ticker) is None


def row(**kw: str) -> dict[str, str]:
    return kw


def test_etfs_and_ambiguous_tickers_stay_unmapped() -> None:
    # Why: an ETF's site is its issuer's (ishares.com says nothing about IBIT),
    # and a ticker on two current companies cannot be guessed.
    q = "http://www.wikidata.org/entity/"
    rows = [
        row(
            ticker="TSLA",
            item=q + "Q478214",
            label="Tesla, Inc.",
            site="https://www.tesla.com/",
            article="https://en.wikipedia.org/wiki/Tesla,_Inc.",
            etf="false",
        ),
        row(
            ticker="TSLA",
            item=q + "Q478214",
            site="https://www.tesla.com/en_us/",
            etf="false",
        ),
        row(
            ticker="TSLA", item=q + "Q478214", site="https://www.tesla.cn/", etf="false"
        ),
        row(ticker="SPY", item=q + "Q1", site="https://ssga.com", etf="true"),
        row(ticker="DUP", item=q + "Q2", site="https://a.com", etf="false"),
        row(ticker="DUP", item=q + "Q3", site="https://b.com", etf="false"),
        row(
            ticker="ONE",
            item=q + "Q4",
            site="https://one.com",
            article="https://en.wikipedia.org/wiki/One",
            etf="false",
        ),
        row(ticker="ONE", item=q + "Q5", site="https://old-one.com", etf="false"),
    ]
    items = sources.equity_items(rows)
    tsla = sources.resolve_company(items["TSLA"], BLOCKED)
    assert tsla == sources.Company("Q478214", "Tesla, Inc.", "tesla.com", "Tesla,_Inc.")
    assert sources.resolve_company(items["SPY"], BLOCKED).startswith("etf")  # type: ignore[union-attr]
    assert "ambiguous" in sources.resolve_company(items["DUP"], BLOCKED)  # type: ignore[operator]
    assert sources.resolve_company(items["ONE"], BLOCKED).qid == "Q4"  # type: ignore[union-attr]
    assert isinstance(sources.resolve_company([], BLOCKED), str)


def test_many_country_pages_still_name_one_domain() -> None:
    sites = [f"https://www.apple.com/{i}/" for i in range(5)] + [
        "https://shop.example",
        "https://twitter.com/a",
        "https://twitter.com/b",
    ]
    assert sources.pick_domain(sites, BLOCKED) == "apple.com"
    # A tie goes to the shorter domain.
    assert (
        sources.pick_domain(
            ["https://www.nvidia.com", "https://www.nvidia-gpus.com"], BLOCKED
        )
        == "nvidia.com"
    )


def test_titles_are_decoded_but_keep_underscores() -> None:
    assert (
        sources.article_title("https://en.wikipedia.org/wiki/Apple_Inc.")
        == "Apple_Inc."
    )
    assert (
        sources.article_title("https://en.wikipedia.org/wiki/Nestl%C3%A9") == "Nestlé"
    )
    assert sources.article_title("https://en.wikipedia.org/wiki/AT%26T") == "AT&T"
    assert sources.article_title("https://en.wikipedia.org/") is None


def test_crypto_articles_match_only_when_unambiguous() -> None:
    rows = [
        {
            "slug": "bitcoin",
            "article": "https://en.wikipedia.org/wiki/Bitcoin",
            "site": "https://bitcoin.org",
        },
        {
            "article": "https://en.wikipedia.org/wiki/Solana_(blockchain_platform)",
            "site": "https://solana.com",
        },
        {
            "article": "https://en.wikipedia.org/wiki/Token_A",
            "site": "https://shared.io",
        },
        {
            "article": "https://en.wikipedia.org/wiki/Company_A",
            "site": "https://www.shared.io",
        },
    ]
    index = sources.CryptoArticles.index(rows, BLOCKED)
    assert index.slug("bitcoin") == "Bitcoin"
    assert index.domain("solana.com") == "Solana_(blockchain_platform)"
    assert index.domain("shared.io") is None
    assert index.slug("solana") is None


def test_wikipedia_titles_resolve_to_their_canonical_article() -> None:
    # Why: pageviews of a redirect title count almost nothing; the map must
    # name the target, and drop a title that does not exist.
    payload = {
        "query": {
            "normalized": [{"from": "Tesla,_Inc.", "to": "Tesla, Inc."}],
            "redirects": [{"from": "Brent Crude", "to": "Brent crude"}],
            "pages": [
                {"title": "Tesla, Inc."},
                {"title": "Brent crude"},
                {"title": "No such page", "missing": True},
            ],
        }
    }
    got = sources.parse_titles(payload)
    assert got["Tesla,_Inc."] == "Tesla,_Inc."
    assert got["Brent_Crude"] == "Brent_crude"
    assert got["No_such_page"] is None


def test_coingecko_ids_are_matched_conservatively() -> None:
    coins = [
        {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"},
        {"id": "pepe", "symbol": "pepe", "name": "Pepe"},
        {"id": "pepe-2", "symbol": "pepe", "name": "Pepe 2.0"},
        {"id": "dup-a", "symbol": "dup", "name": "Dup"},
        {"id": "dup-b", "symbol": "dup", "name": "Dup"},
    ]
    assert sources.match_coingecko(coins, "bitcoin", "BTC", "Bitcoin") == "bitcoin"
    assert sources.match_coingecko(coins, "pepe-coin", "PEPE", "Pepe") == "pepe"
    assert sources.match_coingecko(coins, None, "DUP", "Dup") is None
    assert sources.match_coingecko(coins, None, "XYZ", "Xyz") is None
