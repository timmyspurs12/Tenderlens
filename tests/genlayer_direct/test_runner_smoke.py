"""Smoke the official in-memory Direct Mode deploy/read/write path only."""
from pathlib import Path


CONTRACT = Path(__file__).parent / "contracts" / "storage.py"


def test_direct_mode_deploy_read_and_write(direct_deploy):
    storage = direct_deploy(str(CONTRACT), "initial")

    assert storage.get_value() == "initial"
    storage.set_value("updated")
    assert storage.get_value() == "updated"
