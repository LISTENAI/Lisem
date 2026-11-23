"""Board modem controls retain run identity and reserve their UART."""
import json
import pytest
from lisem import Rejected, StaleRun


def test_modem_sim_and_run_ownership(native_client, tmp_path):
    board = {"schema_version": 1, "id": "modem-test", "name": "Modem test",
             "chip": "ls2684", "storage": {"flash_bytes": 16777216},
             "devices": {"cellular": {"model": "ml307c-gc-cn"}},
             "nets": {"TX": {"endpoints": ["cellular.TX", "chip.PA8"]},
                      "RX": {"endpoints": ["cellular.RX", "chip.PA9"]}}}
    path = tmp_path / "board.json"
    path.write_text(json.dumps(board))
    native_client._rpc.call("board_import", {"path": str(path)})
    with native_client.temporary_instance(board="modem-test") as device:
        with device.start(max_virtual_seconds=30, timeout=40, download=True) as run:
            first = run.modem("cellular")
            assert first["sim_present"] and not first["host_network"]
            assert first["run"] == run.id
            removed = run.modem("cellular", sim_present=False)
            assert not removed["sim_present"] and not removed["pdp_active"]
            assert removed["connected_sockets"] == 0
            with pytest.raises(Rejected, match="Unknown modem"):
                run.modem("missing", sim_present=True)
            with pytest.raises(Rejected, match="UART channel is unavailable"):
                run._call("uart_write", {"channel": 2, "hex": "41540d"})
            with pytest.raises(ValueError):
                run.modem("cellular", sim_present=1)
        with device.start(max_virtual_seconds=30, timeout=40, download=True) as current:
            assert current.modem("cellular")["sim_present"]
            with pytest.raises(StaleRun):
                run.modem("cellular", sim_present=False)
