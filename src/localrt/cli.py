"""``python -m localrt`` : list, inspect, pull and delete local GGUF models.

Works on the models directory directly (``--dir`` or ``COREAI_MODELS_DIR``),
so it needs no running gateway.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from .registry import ModelRegistry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="localrt", description="Manage local GGUF models."
    )
    ap.add_argument("--dir", default=os.environ.get("COREAI_MODELS_DIR", "models"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("hardware")
    for name in ("inspect", "delete"):
        sub.add_parser(name).add_argument("model")
    sub.add_parser("pull").add_argument("model", help="org/repo:QUANT")
    args = ap.parse_args(argv)

    directory = Path(args.dir)
    if args.cmd == "hardware":
        from . import hardware

        print(json.dumps(hardware.detect().__dict__))
        return 0
    if args.cmd == "pull":
        from .download import DownloadError, pull

        directory.mkdir(parents=True, exist_ok=True)
        try:
            print(pull(args.model, directory))
        except DownloadError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    reg = ModelRegistry(directory)
    if args.cmd == "list":
        for m in reg.list():
            print(json.dumps(m))
        return 0
    if args.cmd == "inspect":
        info = reg.get(args.model)
        if info is None:
            print(f"error: no such model: {args.model}", file=sys.stderr)
            return 1
        print(json.dumps(info, indent=2, default=str))
        return 0
    if not reg.delete(args.model):
        print(f"error: no such model: {args.model}", file=sys.stderr)
        return 1
    print(f"deleted {args.model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
