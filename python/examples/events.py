"""Schedule and confirm button input using the built-in ARCS ROM."""
import argparse
from pathlib import Path

from lisem import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with Client(data_dir=args.output / 'library') as client:
        with client.temporary_instance(board='arcs-mini',
                                       evidence_dir=args.output / 'evidence') as device:
            with device.start(max_virtual_seconds=10, timeout=30, download=True) as run:
                task = run.events({
                    'trigger': {'at_ns': int((run.status()['seconds'] + 1) * 1e9)},
                    'actions': [{'after_ms': 0, 'kind': 'button', 'button': 'function',
                                 'count': 1, 'hold_ms': 80, 'gap_ms': 0}],
                })
                result = task.wait_inputs(timeout=5)
                print(result.status, result.raw)


if __name__ == '__main__':
    main()
