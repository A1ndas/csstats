"""Command dispatch. Real commands arrive in Phase 1."""
import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="csstats")
    parser.add_argument("command", choices=["doctor"])
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.command == "doctor":
        print("doctor: not implemented (Phase 1)")
        return 1

    return 2