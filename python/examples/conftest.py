import os
from pathlib import Path
import pytest


@pytest.fixture
def original_lpk():
    value = os.environ.get("LISEM_TEST_LPK")
    if not value:
        pytest.skip("Set LISEM_TEST_LPK to the unchanged ARCS Mini 3.0.3 LPK")
    path = Path(value).resolve()
    assert path.is_file()
    return path


@pytest.fixture
def original_device(lisem_client, original_lpk, tmp_path):
    with lisem_client.temporary_instance(
        board=os.environ.get("LISEM_TEST_BOARD", "arcs-mini"),
        uid=os.environ.get("LISEM_TEST_UID", "ff00a37c102d9e65"),
        lpk=original_lpk, evidence_dir=tmp_path / "evidence",
    ) as device:
        yield device
