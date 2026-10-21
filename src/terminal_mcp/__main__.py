"""Console entry point; stdio server wiring is implemented in the MCP task."""

import sys

from .config import load_config, parse_cli
from .contracts import ToolError


def main() -> int:
    args = parse_cli()
    try:
        load_config(**vars(args))
    except ToolError as exc:
        print(f"{exc.code}: {exc.message} {exc.hint}", file=sys.stderr)
        return 2
    print("The stdio server is not implemented yet; use --help to inspect configuration options.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
