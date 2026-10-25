"""Small real-firmware state machine, replay, and an explicit bad-rule demo.

--demo-failure intentionally asserts that repeated presses are forbidden after
verifying their real edges. It demonstrates failure recording/shrinking, not a
Lisem or firmware defect. Normal runs never enable that extra assertion.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

from hypothesis import Phase, settings, __version__ as hypothesis_version
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from hypothesis import strategies as st
from lisem import Client
from firmware import boot_to_shell, firmware_uid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lpk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--demo-failure", action="store_true")
    parser.add_argument("--max-seconds", type=int, default=300,
                        help="Host search budget, checked between bounded operations (60..3600)")
    args = parser.parse_args()
    if not 60 <= args.max_seconds <= 3600:
        parser.error("--max-seconds must be 60..3600")
    args.output.mkdir(parents=True, exist_ok=False)
    firmware_hash = hashlib.sha256(args.lpk.read_bytes()).hexdigest()
    attempts = []
    deadline = time.monotonic() + args.max_seconds

    def check_budget():
        if time.monotonic() >= deadline:
            raise KeyboardInterrupt("Stateful search budget exhausted")

    class FirmwareMachine(RuleBasedStateMachine):
        def __init__(self):
            check_budget()
            super().__init__()
            self.directory = args.output / ("attempt-" + uuid.uuid4().hex)
            self.directory.mkdir()
            self.record = {"version": 1, "lpk_sha256": firmware_hash,
                           "hypothesis": hypothesis_version, "board": "arcs-mini",
                           "uid": "ff00a37c102d9e65", "demo_failure": args.demo_failure,
                           "steps": [], "failed": False}
            attempts.append(self.record)
            self.stack = ExitStack()
            self.failure = None
            try:
                self.client = self.stack.enter_context(Client(data_dir=args.output / "library"))
                self.device = self.stack.enter_context(self.client.temporary_instance(
                    board="arcs-mini", uid=self.record["uid"], lpk=args.lpk,
                    evidence_dir=self.directory / "evidence"))
                self.run = self.stack.enter_context(self.device.start(max_virtual_seconds=90, timeout=120))
                boot_to_shell(self.run)
                self.record["build"] = self.client.build
                self.record["qemu"] = self.run.status()["qemu"]
                self.record["qemu"].pop("pid", None)
                self.save()
            except BaseException:
                self.stack.__exit__(*sys.exc_info())
                raise

        def save(self):
            (self.directory / "steps.json").write_text(json.dumps(self.record, indent=2) + "\n")

        def perform(self, action, **params):
            check_budget()
            step = {"action": action, **params}
            self.record["steps"].append(step)
            self.save()
            try:
                if action == "identity":
                    assert firmware_uid(self.run) == self.device.uid
                elif action == "usb_det":
                    with self.run.signal_trace(["USB_DET"], capacity=32) as trace:
                        delivered = self.run.set_inputs({"usb_det": params["enabled"]})
                        assert delivered.status == "applied"
                    assert trace.events[-1]["level"] is params["enabled"]
                elif action == "keys":
                    with self.run.signal_trace(["POWER_KEY"], capacity=32) as trace:
                        delivered = self.run.button(count=params["count"], hold_ms=params["hold_ms"], gap_ms=30)
                        assert delivered.status == "completed"
                    edges = [e for e in trace.events if not e.get("initial")]
                    assert [e["level"] for e in edges] == [False, True] * params["count"]
                    assert all(b["virtual_ns"] - a["virtual_ns"] == params["hold_ms"] * 1_000_000
                               for a, b in zip(edges[::2], edges[1::2]))
                    if args.demo_failure:
                        assert params["count"] < 2, "DEMO: deliberately overstrict no-repeat rule"
                else:
                    raise ValueError("Unknown replay action")
                step["verified"] = True
            except BaseException as exc:
                self.failure = exc
                self.client.mark_failed(str(exc))
                self.record.update(failed=True, error=f"{type(exc).__name__}: {exc}")
                self.save()
                raise
            self.save()

        @rule()
        def identity(self):
            self.perform("identity")

        @rule(enabled=st.booleans())
        def usb_det(self, enabled):
            self.perform("usb_det", enabled=enabled)

        @rule(count=st.integers(1, 3), hold_ms=st.integers(13, 20))
        def keys(self, count, hold_ms):
            self.perform("keys", count=count, hold_ms=hold_ms)

        @invariant()
        def retains_real_run(self):
            try:
                state = self.run.status()
                assert not state["finished"] and not state.get("error")
            except Exception as exc:
                self.client.mark_failed(str(exc))
                self.record.update(failed=True, error=f"{type(exc).__name__}: {exc}")
                self.save()
                raise

        def teardown(self):
            self.stack.close()

    failure = None
    try:
        if args.replay:
            recorded = json.loads(args.replay.read_text())
            assert recorded["version"] == 1 and recorded["lpk_sha256"] == firmware_hash
            assert recorded["demo_failure"] == args.demo_failure, "Replay must use the recorded predicate"
            machine = FirmwareMachine()
            try:
                for step in recorded["steps"]:
                    machine.perform(step["action"], **{k: v for k, v in step.items() if k not in ("action", "verified")})
            finally:
                machine.teardown()
        else:
            run_state_machine_as_test(FirmwareMachine, settings=settings(
                max_examples=3, stateful_step_count=4, deadline=None,
                derandomize=True, phases=(Phase.generate, Phase.shrink)))
    except (Exception, KeyboardInterrupt) as exc:
        failure = exc
    failures = [record for record in attempts if record["failed"]]
    if failures:
        minimal = min(failures, key=lambda r: (len(r["steps"]), sum(s.get("count", 0) + s.get("hold_ms", 0) for s in r["steps"])))
        (args.output / "minimal-replay.json").write_text(json.dumps(minimal, indent=2) + "\n")
    report = {"passed": failure is None, "demo_failure": args.demo_failure,
              "attempts": len(attempts), "failed_attempts": len(failures),
              "min_failed_steps": min((len(r["steps"]) for r in failures), default=None),
              "max_failed_steps": max((len(r["steps"]) for r in failures), default=None)}
    if failure is not None:
        report["error"] = f"{type(failure).__name__}: {failure}"
    (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    if failure is not None:
        raise failure


if __name__ == "__main__":
    main()
