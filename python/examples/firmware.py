"""Offline boot, identity and info-page helpers for ARCS Mini 3.0.3."""
import re


def boot_to_shell(run):
    watch = run.uart[0].watch(cursor=0)
    run.wait(lambda state: state.get("seconds", 0) >= .4, timeout=10)
    delivered = run.button("function", hold_ms=3300, timeout=15)
    assert delivered.status == "completed" or (
        delivered.status == "cancelled" and delivered.raw.get("reason") == "reset")
    watch.expect(b"ListenAI:/$", timeout=25)
    run.wait(lambda state: state.get("seconds", 0) >= 8, timeout=15)
    assert not run.status().get("error")
    return watch


def firmware_uid(run):
    # Observe before the command, so an old boot UID cannot satisfy the check.
    with run.uart[0].watch() as uart:
        run.uart[0].write(b"device get_device_id\r")
        response = uart.expect(rb"Chip ID:\s*[0-9a-fA-F]{16}", regex=True, timeout=10)
        return re.search(rb"Chip ID:\s*([0-9a-fA-F]{16})", response).group(1).decode().lower()


def assert_arcs_otp(device):
    # This is the verified ARCS layout, not a rule for other chip families.
    assert device.info["hardware"]["chip"]["family"] == "arcs"
    assert (device.path / "otp.bin").read_bytes()[8:16].hex() == device.uid


def click_to_cloud_info(run):
    """Unbound/offline 3.0.3 firmware sends a click to its info page."""
    with run.uart[0].watch() as uart:
        outcome = run.button("function", count=1, hold_ms=80)
        assert outcome.status == "completed" and outcome.raw["completed"] == 1
        uart.expect(b"button evt id=0 action=1", timeout=10)
        uart.expect(b"Button redirects to cloud info, status=0", timeout=10)
        uart.expect(b"Open info page message received", timeout=10)
