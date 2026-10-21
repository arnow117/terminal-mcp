"""Console entry point for the local MCP stdio server."""

import asyncio
import sys

from .config import load_config, parse_cli
from .contracts import ToolError
from .server import run_stdio


def main() -> int:
    args = parse_cli()
    try:
        config = load_config(**vars(args))
        asyncio.run(run_stdio(config))
    except ToolError as exc:
        print(f"{exc.code}: {exc.message} {exc.hint}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
