import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="Update snapshot CSV files instead of comparing",
    )


@pytest.fixture
def update_snapshots(request):
    return request.config.getoption("--update-snapshots")
