import os
import pytest


@pytest.fixture
def native_client(tmp_path):
    from lisem import Client
    executable = os.environ.get("LISEM_EXECUTABLE")
    if not executable:
        pytest.skip("Set LISEM_EXECUTABLE to a native RPC v1 CLI")
    with Client(executable=executable, data_dir=tmp_path / "library") as client:
        yield client
