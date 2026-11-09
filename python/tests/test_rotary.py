"""Native quadrature input retains run ownership and delivered edge counts."""
import json
import pytest
from lisem import Rejected, StaleRun


def test_rotary_delivery_and_stale_identity(native_client, tmp_path):
    board = {"schema_version": 1, "id": "rotary-test", "name": "Rotary test",
             "chip": "ls2684", "storage": {"flash_bytes": 16777216},
             "devices": {"knob": {"model": "quadrature-encoder",
                         "parameters": {"edges_per_detent": 2}}},
             "nets": {"A": {"endpoints": ["chip.PA4", "knob.A"], "pull": "up"},
                      "B": {"endpoints": ["chip.PA5", "knob.B"], "pull": "up"}},
             "links": []}
    path = tmp_path / "board.json"
    path.write_text(json.dumps(board))
    native_client._rpc.call("board_import", {"path": str(path)})
    with native_client.temporary_instance(board="rotary-test") as device:
        with device.start(max_virtual_seconds=30, timeout=40, download=True) as run:
            first = run.rotary("knob", 3)
            assert first.status == "completed"
            assert first.raw["delivered_edges"] == 6
            assert first.raw["phase"] == 2
            second = run.rotary("knob", -3)
            assert second.raw["phase"] == 0
            assert second.raw["sequence"] > first.raw["sequence"]
            with pytest.raises(Rejected, match="Stale rotary"):
                run._call("rotary", dict(id=device.id, rotary="knob", action="cancel",
                          sequence=first.raw["sequence"]))
            with pytest.raises(Rejected, match="Unknown rotary"):
                run.rotary("missing", 1)
            with pytest.raises(Rejected, match="detents"):
                run.rotary("knob", 0)
        with pytest.raises(Rejected, match="not running"):
            run.rotary("knob", 1)
        with device.start(max_virtual_seconds=10, timeout=20, download=True):
            with pytest.raises(StaleRun):
                run.rotary("knob", 1)
