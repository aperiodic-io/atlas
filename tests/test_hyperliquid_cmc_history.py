import json
import subprocess

from integrations.hyperliquid_cmc_history import restore_first_captures


def commit(repo, date, rows):
    path = repo / "atlas/data/hyperliquid-spot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "capture",
            "--date",
            date,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_recovery_uses_first_capture_of_current_listing_and_preserves_known_dates(
    tmp_path,
):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    commit(tmp_path, "2026-01-01T00:00:00Z", [{"id": "HYPE/USDC"}, {"id": "OLD/USDC"}])
    commit(tmp_path, "2026-02-01T00:00:00Z", [{"id": "HYPE/USDC"}])
    commit(
        tmp_path,
        "2026-03-01T00:00:00Z",
        [
            {"id": "HYPE/USDC"},
            {"id": "OLD/USDC"},
            {"id": "KEEP/USDC", "first_capture": "2025-01-01T00:00:00Z"},
        ],
    )
    data = tmp_path / "atlas/data"
    original = (data / "hyperliquid-spot.json").read_bytes()
    assert restore_first_captures(tmp_path, dry_run=True) == {"hyperliquid-spot": 2}
    assert (data / "hyperliquid-spot.json").read_bytes() == original
    assert restore_first_captures(tmp_path) == {"hyperliquid-spot": 2}
    rows = json.loads((data / "hyperliquid-spot.json").read_text())
    assert rows == [
        {"id": "HYPE/USDC", "first_capture": "2026-01-01T00:00:00Z"},
        {"id": "OLD/USDC", "first_capture": "2026-03-01T00:00:00Z"},
        {"id": "KEEP/USDC", "first_capture": "2025-01-01T00:00:00Z"},
    ]
    assert restore_first_captures(tmp_path) == {}


def test_uncommitted_rows_are_not_given_an_invented_capture(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    commit(tmp_path, "2026-01-01T00:00:00Z", [{"id": "OLD/USDC"}])
    path = tmp_path / "atlas/data/hyperliquid-spot.json"
    path.write_text(json.dumps([{"id": "NEW/USDC"}]))
    assert restore_first_captures(tmp_path) == {}
    assert "first_capture" not in json.loads(path.read_text())[0]
