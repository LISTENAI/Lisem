"""Ordinary-script entry point; uses the same helpers as the pytest example."""
import argparse
from pathlib import Path
from lisem import Client
from firmware import boot_to_shell, firmware_uid, assert_arcs_otp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lpk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--board", choices=["arcs-mini", "arcs-mini3"], default="arcs-mini")
    parser.add_argument("--uid", default="ff00a37c102d9e65")
    args = parser.parse_args()
    with Client(data_dir=args.output / "library") as client:
        with client.temporary_instance(board=args.board, uid=args.uid, lpk=args.lpk,
                                       evidence_dir=args.output / "evidence") as device:
            assert_arcs_otp(device)
            with device.start(max_virtual_seconds=90, timeout=150) as run:
                boot_to_shell(run)
                assert firmware_uid(run) == device.uid
                run.screenshot().save(args.output / "screen.png")
        assert client.instances() == []
    print("PASS: original firmware UID, boot, screenshot and temporary cleanup")


if __name__ == "__main__":
    main()
