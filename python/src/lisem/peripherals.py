"""Saved device inputs and controls bound to one active Run."""
from pathlib import Path
import time

from .errors import (CompatibilityError, DeliveryUnknown, LisemError, OperationTimeout, Rejected,
                     RunEnded, StaleRun)


def _deadline(timeout):
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 180:
        raise ValueError("timeout must be >0 and <=180 seconds")
    return time.monotonic() + timeout


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OperationTimeout("Peripheral operation deadline expired")
    return remaining


class _Control:
    def __init__(self, instance, run=None):
        self.instance, self.run = instance, run

    def _call(self, method, params=None, *, timeout=10, mutation=True):
        if self.run is not None:
            return self.run._call(method, params, timeout=timeout, mutation=mutation)
        if self.instance.closed:
            raise Rejected("Instance handle is closed")
        if self.instance.worker is None:
            self.instance._connect()
        value = dict(params or {}, worker=self.instance.worker.identity["id"])
        # Running changes require the caller's Run identity. The backend also
        # rejects a race with start instead of applying a saved mutation to it.
        return self.instance.worker.call(method, value, timeout=timeout, mutation=mutation)

    def _configuration(self):
        if self.run is not None:
            self.run.status()
        if self.instance.closed:
            raise Rejected("Instance handle is closed")
        value = self.instance.client._rpc.call("inspect", {"id": self.instance.id}, mutation=False)
        self.instance.info = value["device"]
        if self.run is not None:
            self.run.status()
        return value["device"]

    def _change(self, method, params, field, *, timeout, match=None):
        from .client import Delivery
        deadline = _deadline(timeout)
        change = self._call(method, params, timeout=_remaining(deadline))
        request = change.get("request")
        while change.get("status") == "pending":
            if self.run is None:
                raise DeliveryUnknown(f"{method}: saved operation unexpectedly remains pending")
            try:
                state = self.run._call("status", timeout=min(_remaining(deadline), 5), mutation=False)["session"]
            except (OperationTimeout, RunEnded) as exc:
                raise DeliveryUnknown(f"{method}: pending change remains unconfirmed; no replay") from exc
            current = state.get(field, {})
            if request is not None and current.get("request") != request:
                raise DeliveryUnknown(f"{method}: another request replaced the pending change")
            change = current
            if state.get("finished") and change.get("status") == "pending":
                raise DeliveryUnknown(f"{method}: run ended before delivery was confirmed")
            if change.get("status") == "pending":
                time.sleep(min(.02, max(0, deadline - time.monotonic())))
        status = change.get("status")
        if status == "rejected":
            raise Rejected(f"{method}: {change.get('error') or 'rejected'}")
        if status not in ("saved", "applied"):
            raise DeliveryUnknown(f"{method}: final delivery status is unavailable")
        if change.get("error"):
            raise LisemError(f"{method}: {change['error']}")
        if match is not None and not match(change):
            raise DeliveryUnknown(f"{method}: confirmation belongs to another source or medium")
        return Delivery(status, change)


class Wifi(_Control):
    """Configure the AP environment; firmware performs association and IP I/O."""
    def set_present(self, enabled, *, timeout=10):
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        return self._change("access_point", {"enabled": enabled}, "access_point_change",
                            timeout=timeout, match=lambda change: change.get("enabled") is enabled)

    def status(self, *, timeout=10):
        """Return saved availability and a fresh, run-bound AP observation."""
        deadline = _deadline(timeout)
        state = self._call("status", mutation=False, timeout=_remaining(deadline))["session"]
        saved = self.instance.client._rpc.call("inspect", {"id": self.instance.id},
                    mutation=False, timeout=_remaining(deadline))["device"]
        self.instance.info = saved
        if not saved["capabilities"].get("wifi"):
            raise Rejected("Wi-Fi is unavailable on this board")
        live = None
        if state and state.get("runtime_active") and not state.get("finished"):
            if "wifi_observation" not in self.instance.worker.features:
                raise CompatibilityError("This Lisem build lacks Wi-Fi observation support")
            live = self._call("wifi_status", {"run": state["output"]},
                              mutation=False, timeout=_remaining(deadline))
            if live.get("run") != state["output"] or live.get("instance_id") != self.instance.id:
                raise StaleRun("Wi-Fi observation belongs to another instance or Run")
        return {"configured_present": saved["host"].get("wifi_ap_present") is not False,
                "runtime": live}

    def configure(self, access_points, *, timeout=10):
        # Preserve the backend's validation of security, RSSI, channels and SSID.
        environment = {"access_points": [dict(ap) for ap in access_points]}
        return self._change("wifi_environment", {"environment": environment},
                            "wifi_environment_change", timeout=timeout)

    def configuration(self):
        return self._configuration()["environment"]["wifi"]


class Inputs(_Control):
    """Board-declared external inputs; native validation owns their types."""
    def get(self, *, timeout=10):
        _deadline(timeout)
        return self._call("signals", mutation=False, timeout=timeout)

    def set(self, changes, *, timeout=10):
        return self._change("signals", {"changes": changes}, "signal_change",
                            timeout=timeout)


class SD(_Control):
    """Attach or eject a writable raw medium; firmware operates its filesystem."""
    def mount(self, path, *, timeout=10):
        path = str(Path(path).resolve(strict=True))
        return self._change("sd_card", {"medium": {"path": path, "read_only": False}},
                            "sd_change", timeout=timeout,
                            match=lambda change: (change.get("medium") or {}).get("path") == path)

    def eject(self, *, timeout=10):
        return self._change("sd_card", {"medium": None}, "sd_change", timeout=timeout,
                            match=lambda change: "medium" in change and change["medium"] is None)

    def status(self):
        return self._call("sd_card", mutation=False)


class Camera(_Control):
    """Select an image or an explicitly chosen host camera as sensor input."""
    def set_image(self, path, *, timeout=10):
        return self._select({"path": str(Path(path).resolve(strict=True))}, timeout)

    def use_device(self, device_id, *, timeout=10):
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("device_id must be a nonempty identifier from camera_devices()")
        return self._select({"device_id": device_id}, timeout)

    def clear(self, *, timeout=10):
        return self._select({"path": None}, timeout)

    def _select(self, source, timeout):
        return self._change("camera", source, "camera_change", timeout=timeout,
                            match=lambda change: change.get("path") == source.get("path")
                            and change.get("device_id") == source.get("device_id"))

    def status(self):
        if self.run is not None:
            state = self.run.status()
            return {key: state.get(key) for key in
                    ("camera_image", "camera_device", "camera_change", "camera_capture")}
        host = self._configuration()["host"]
        return {key: host.get(key) for key in ("camera_image", "camera_device")}


class USBADB(_Control):
    """Explicitly enable a real USB ADB TCP bridge during this Run."""
    def enable(self, *, timeout=10):
        return self._set(True, timeout)

    def disable(self, *, timeout=10):
        return self._set(False, timeout)

    def _set(self, enabled, timeout):
        _deadline(timeout)
        state = self._call("usb_adb", {"enabled": enabled}, timeout=timeout)
        self._check(state)
        if state.get("enabled") is not enabled:
            raise DeliveryUnknown("USB ADB enabled state was not confirmed")
        return state

    def _check(self, state):
        if state.get("run") != self.run.id:
            raise StaleRun("USB ADB status belongs to another Run")
        if state.get("error"):
            raise Rejected(f"USB ADB: {state['error']}")

    def status(self):
        state = self._call("usb_adb", mutation=False)
        if state.get("run") != self.run.id:
            raise StaleRun("USB ADB status belongs to another Run")
        return state
