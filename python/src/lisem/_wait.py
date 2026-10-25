"""One absolute deadline for synchronous, read-only observations."""
import math
import time

from .errors import ObservationTimeout, OperationTimeout


class ObservationDeadline:
    def __init__(self, run, target, timeout, *, channel=None):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("Observation timeout must be positive and finite")
        self.expires = time.monotonic() + timeout
        self.context = {"run": run.id, "target": target}
        if channel is not None:
            self.context["channel"] = channel

    def remaining(self):
        return max(0, self.expires - time.monotonic())

    def error(self, phase, kind):
        context = dict(self.context, phase=phase, kind=kind)
        detail = "deadline expired" if kind == "deadline" else "RPC failed before observation deadline"
        error_type = ObservationTimeout if kind == "deadline" else OperationTimeout
        label = f"UART {context['channel']}" if "channel" in context else "Run"
        error = error_type(f"{label} observation {context['target']} {detail} "
                           f"(run={context['run']}, phase={phase})")
        error.observation = context
        return error

    def check(self, phase):
        if self.remaining() <= 0:
            raise self.error(phase, "deadline")

    def rpc(self, run, method, params=None):
        remaining = self.remaining()
        if remaining <= 0:
            raise self.error(method, "deadline")
        try:
            result = run._call(method, params, timeout=min(5, remaining),
                               mutation=False, _deadline=self.expires)
        except OperationTimeout as cause:
            kind = "deadline" if self.remaining() <= 0 else "rpc"
            raise self.error(method, kind) from cause
        self.check(method)
        return result

    def pause(self):
        self.check("poll")
        time.sleep(min(.02, self.remaining()))
