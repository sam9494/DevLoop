"""維運指令。`uv run devloop <指令>`，或 make 的對應目標。"""

import argparse
import sys

from devloop.db.session import session_scope
from devloop.graph.client import Neo4jGraph
from devloop.graph.rebuild import rebuild


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devloop")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("rebuild-graph", help="清空 Neo4j 並從 Postgres 的 edges 表重建")

    args = parser.parse_args(argv)

    if args.command == "rebuild-graph":
        graph = Neo4jGraph.from_settings()
        try:
            with session_scope() as session:
                nodes, edges = rebuild(session, graph)
        finally:
            graph.close()
        print(f"重建完成：{nodes} 個節點、{edges} 條邊")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
