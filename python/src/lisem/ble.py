"""Generic run-bound BLE central. Application codecs belong to callers."""

from contextlib import AbstractContextManager
from collections import deque

from ._wait import ObservationDeadline
from .errors import (
    ATTError,
    CleanupError,
    CompatibilityError,
    DeliveryUnknown,
    HistoryLost,
    LisemError,
    OperationTimeout,
    Rejected,
    StaleRun,
    preserve_exception,
)


CLEANUP_SECONDS = 5


def _timeout(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < value <= 120
    ):
        raise ValueError("BLE timeout must be >0 and <=120 seconds")
    return max(1, int(value * 1000))


def _deadline(run, target, timeout):
    _timeout(timeout)
    return ObservationDeadline(run, target, timeout)


def _preserve_cleanup(original, cleanup):
    if not isinstance(cleanup, DeliveryUnknown):
        failure = DeliveryUnknown("BLE cancellation was not confirmed")
        failure.__cause__ = cleanup
        cleanup = failure
    preserve_exception(original, cleanup)


class BLE(AbstractContextManager):
    """One virtual central owned by this Run; open lazily on first use."""

    def __init__(self, run):
        self.run, self.peer, self.connection = run, None, None
        self.closed = False

    def __enter__(self):
        self._open()
        return self

    def __exit__(self, typ, exc, tb):
        try:
            self.close()
        except BaseException as cleanup:
            preserve_exception(exc, cleanup)

    def _check(self):
        if "ble_central" not in self.run.instance.worker.features:
            raise CompatibilityError("Worker does not support the BLE central contract")

    def capabilities(self, *, timeout=5):
        budget = _deadline(self.run, "BLE capabilities", timeout)
        self._check()
        return budget.rpc(self.run, "ble", {"action": "capabilities"})

    def _open(self, _budget=None):
        budget = _budget or _deadline(self.run, "BLE open", 5)
        budget.check("owner")
        if self.run.retired or self.run.instance.current_run is not self.run:
            raise StaleRun("The handle belongs to an earlier run")
        self._check()
        if self.closed:
            raise Rejected("BLE central is closed")
        if self.peer is None:
            value = self._mutate({"action": "open"}, budget)
            self.peer = value["peer"]
            self.run.tasks.append(self)
            budget.check("open")

    def _mutate(self, request, budget):
        remaining = budget.remaining()
        if remaining <= 0:
            raise budget.error(request["action"], "deadline")
        return self.run._call(
            "ble",
            request,
            _record=False,
            timeout=min(5, remaining),
            _deadline=budget.expires,
        )

    def _call(self, action, *, connection=None, _budget=None, **params):
        budget = _budget or _deadline(self.run, "BLE " + action, 5)
        self._open(budget)
        request = dict(params, action=action, peer=self.peer)
        if connection is not None:
            request["connection"] = connection.id
        if action in ("status", "events", "operation"):
            return budget.rpc(self.run, "ble", request)
        budget.check(action)
        # Raw values may contain credentials. Only explicitly collected events
        # are available to callers, outside ordinary SDK operation evidence.
        return self._mutate(request, budget)

    def _abort_submission(self, original):
        try:
            self.close(timeout=CLEANUP_SECONDS)
        except BaseException as cleanup:
            _preserve_cleanup(original, cleanup)

    def trace(self, enabled=True, *, timeout=5):
        """Explicitly include raw link PDUs in bounded in-memory events."""
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        budget = _deadline(self.run, "BLE trace", timeout)
        result = self._call("trace", enabled=enabled, _budget=budget)
        budget.check("trace")
        return result

    def status(self, *, timeout=5):
        return self._call("status", _budget=_deadline(self.run, "BLE status", timeout))

    def scan(self, *, timeout=5):
        """Start active scanning; timeout bounds submission, not scan duration."""
        budget = _deadline(self.run, "BLE scan", timeout)
        value = self._call("scan", _budget=budget)
        budget.check("scan")
        return BLEEvents(self, value["sequence"])

    def connect(self, address, *, address_type, timeout=10):
        """One total deadline covers submission and actual peer link evidence."""
        budget = _deadline(self.run, "BLE connect", timeout)
        try:
            value = self._call(
                "connect",
                address=address,
                address_type=address_type,
                timeout_ms=_timeout(timeout),
                _budget=budget,
            )
        except (DeliveryUnknown, KeyboardInterrupt) as original:
            self._abort_submission(original)
            raise
        connection = BLEConnection(self, value["connection"])
        self.connection = connection
        BLEOperation(connection, value["operation"]["id"])._wait(budget)
        return connection

    def events(self, *, after=None, timeout=5):
        budget = _deadline(self.run, "BLE event cursor", timeout)
        self._open(budget)
        if after is None:
            after = self._call("status", _budget=budget)["sequence"]
        budget.check("cursor")
        return BLEEvents(self, after)

    def cancel(self):
        self.close()

    def close(self, *, timeout=CLEANUP_SECONDS):
        """Stop this central's transmissions; the peer observes link loss."""
        if self.closed:
            return
        budget = _deadline(self.run, "BLE close", timeout)
        if self.peer is not None:
            state = budget.rpc(self.run, "status")["session"]
            if not state.get("finished"):
                value = self._call("close", _budget=budget)
                if value["link"].get("connected") or value["link"].get("connecting"):
                    raise CleanupError("BLE central stop was not confirmed")
        self.closed = True
        budget.check("close")


class BLEConnection(AbstractContextManager):
    def __init__(self, central, identifier):
        self.central, self.id = central, identifier
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, typ, exc, tb):
        try:
            if not self.closed:
                if self.central.closed:
                    self.closed = True
                else:
                    budget = _deadline(
                        self.central.run, "BLE connection cleanup", CLEANUP_SECONDS
                    )
                    state = self._call("status", _budget=budget)
                    if state.get("connection") != self.id:
                        raise StaleRun(
                            "BLE cleanup response belongs to another connection"
                        )
                    operation = state.get("operation")
                    if operation and operation["status"] == "pending":
                        BLEOperation(self, operation["id"]).cancel(_budget=budget)
                    # A terminal operation's cancel is a no-op: it may have
                    # completed since status, leaving a live link to disconnect.
                    # Native disconnect also confirms an already ended link.
                    budget.check("disconnect")
                    self.disconnect(
                        timeout=max(0.001, budget.remaining()), _budget=budget
                    )
        except BaseException as cleanup:
            preserve_exception(exc, cleanup)

    def _call(self, action, **params):
        if self.closed or self.central.connection is not self:
            raise StaleRun("BLE connection handle is no longer current")
        return self.central._call(action, connection=self, **params)

    def submit(self, action, *, timeout=10, _budget=None, **params):
        """Submit asynchronously. timeout sets the native operation deadline."""
        if action not in ("discover", "read", "write", "subscribe", "disconnect"):
            raise ValueError("Unsupported GATT operation")
        timeout_ms = _timeout(timeout)
        budget = _budget or _deadline(self.central.run, "BLE submission", 5)
        try:
            value = self._call(action, timeout_ms=timeout_ms, _budget=budget, **params)
        except (DeliveryUnknown, KeyboardInterrupt) as original:
            self.central._abort_submission(original)
            raise
        operation = BLEOperation(self, value["operation"]["id"])
        try:
            budget.check("submission")
        except OperationTimeout as original:
            operation._cancel_after_failure(original)
            raise
        return operation

    def _perform(self, action, timeout, _budget=None, **params):
        budget = _budget or _deadline(self.central.run, "BLE " + action, timeout)
        operation = self.submit(action, timeout=timeout, _budget=budget, **params)
        return operation._wait(budget)

    def discover(self, kind="services", *, start=1, end=65535, timeout=10):
        return self._perform("discover", timeout, kind=kind, start=start, end=end)[
            "attributes"
        ]

    def read(self, handle, *, timeout=10):
        value = self._perform("read", timeout, handle=handle)
        return bytes.fromhex(value["value"])

    def write(self, handle, value, *, response=True, timeout=10):
        if not isinstance(value, bytes):
            raise TypeError("BLE value must be bytes")
        if not isinstance(response, bool):
            raise TypeError("response must be a bool")
        return self._perform(
            "write", timeout, handle=handle, value=value.hex(), response=response
        )

    def subscribe(self, value_handle, cccd_handle, *, indications=False, timeout=10):
        """Write the discovered CCCD; observe notifications after that write."""
        budget = _deadline(self.central.run, "BLE subscribe", timeout)
        cursor = self.central._call("status", _budget=budget)["sequence"]
        watch = BLEEvents(self.central, cursor, self, value_handle)
        self._perform(
            "subscribe",
            timeout,
            _budget=budget,
            handle=cccd_handle,
            value="0200" if indications else "0100",
        )
        return watch

    def notifications(self, *, after=None):
        if after is None:
            after = self.central.status()["sequence"]
        return BLEEvents(self.central, after, self)

    def disconnect(self, *, timeout=10, _budget=None):
        result = self._perform("disconnect", timeout, _budget=_budget)
        self.closed = True
        return result


class BLEOperation:
    def __init__(self, connection, identifier):
        self.connection, self.id = connection, identifier

    def status(self, *, timeout=5, _budget=None):
        budget = _budget or _deadline(
            self.connection.central.run, "BLE operation " + self.id, timeout
        )
        return self.connection._call("operation", operation=self.id, _budget=budget)[
            "operation"
        ]

    def cancel(self, *, timeout=CLEANUP_SECONDS, _budget=None):
        budget = _budget or _deadline(
            self.connection.central.run, "BLE cancel " + self.id, timeout
        )
        value = self.connection._call("cancel", operation=self.id, _budget=budget)
        operation = value["operation"]
        if operation["id"] != self.id or operation["status"] == "pending":
            raise DeliveryUnknown("BLE cancellation was not confirmed")
        if operation["status"] == "cancelled" and (
            value["link"].get("connected") or value["link"].get("connecting")
        ):
            raise DeliveryUnknown("BLE transmissions remain active after cancellation")
        budget.check("cancel")
        return operation

    def _cancel_after_failure(self, original):
        try:
            self.cancel(timeout=CLEANUP_SECONDS)
        except BaseException as cleanup:
            _preserve_cleanup(original, cleanup)

    def wait(self, *, timeout=10):
        return self._wait(
            _deadline(self.connection.central.run, "BLE operation " + self.id, timeout)
        )

    def _wait(self, budget):
        try:
            while True:
                value = self.status(_budget=budget)
                status = value["status"]
                if status == "timeout":
                    failure = OperationTimeout(
                        value.get("error", "Native BLE operation timed out")
                    )
                    failure.native_result = value
                    raise failure
                if "att_error" in value:
                    raise ATTError(value)
                if status in ("error", "interrupted", "cancelled"):
                    raise LisemError(value.get("error", status))
                budget.check("operation result")
                if status != "pending":
                    return value
                budget.pause()
        except (OperationTimeout, KeyboardInterrupt) as original:
            self._cancel_after_failure(original)
            raise


class BLEEvents:
    """Paged observation; old cursors raise HistoryLost instead of dropping data."""

    def __init__(self, central, after, connection=None, handle=None):
        self.central, self.cursor = central, after
        self.connection, self.handle = connection, handle
        self.pending = deque()

    def read(self, *, timeout=5, _budget=None):
        budget = _budget or _deadline(self.central.run, "BLE events", timeout)
        self.central._open(budget)
        if (
            self.connection is not None
            and self.central.connection is not self.connection
        ):
            raise StaleRun("BLE observation belongs to an earlier connection")
        if self.pending:
            budget.check("buffer")
            result = list(self.pending)
            self.pending.clear()
            return result
        page = self.central._call(
            "events", connection=self.connection, after=self.cursor, _budget=budget
        )
        if page["lost"]:
            raise HistoryLost(
                f"BLE history lost {page['lost']} events; last cursor={self.cursor}"
            )
        self.cursor = page["cursor"]
        result = []
        for raw in page["events"]:
            if raw["kind"] == "history_lost":
                raise HistoryLost("BLE transport history lost; reconnect required")
            if self.connection is not None and (
                raw.get("connection") != self.connection.id
                or raw["kind"] not in ("notification", "indication")
            ):
                continue
            if self.handle is not None and raw.get("handle") != self.handle:
                continue
            event = dict(raw)
            if "value" in event:
                event["value"] = bytes.fromhex(event["value"])
            if "pdu" in event:
                event["pdu"] = bytes.fromhex(event["pdu"])
            result.append(event)
        budget.check("event decode")
        if (
            self.connection is not None
            and not result
            and not page["link"].get("confirmed")
        ):
            raise LisemError(
                "BLE connection interrupted: "
                + str(page["link"].get("error") or "inactive")
            )
        return result

    def expect(self, predicate, *, timeout=10):
        budget = _deadline(self.central.run, "BLE event match", timeout)
        while True:
            events = self.read(_budget=budget)
            for index, event in enumerate(events):
                try:
                    matched = predicate(event)
                    budget.check("match")
                except BaseException:
                    self.pending.extend(events[index:])
                    raise
                if matched:
                    self.pending.extend(events[index + 1 :])
                    return event
            budget.pause()
