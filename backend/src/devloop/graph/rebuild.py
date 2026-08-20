"""從 Postgres 重建整張圖。

Neo4j 是 `edges` 表的投影，不是真相 —— 所以任何時候砍掉都要能一行指令長回來。
凍結時的同步失敗、換 Neo4j、圖被誤刪，補救方式都是這一支。
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from devloop.db.models import Card, Decision, Edge
from devloop.graph.client import Edge as GraphEdge
from devloop.graph.client import GraphStore, Node


def rebuild(session: Session, graph: GraphStore) -> tuple[int, int]:
    """回傳 (節點數, 邊數)。會先清空 —— 這是重建不是合併。"""
    graph.clear()

    nodes = 0
    for card in session.scalars(select(Card)).all():
        graph.upsert_node(Node(id=str(card.id), kind="Card", label=card.key, summary=card.title))
        nodes += 1

    for decision in session.scalars(select(Decision)).all():
        graph.upsert_node(
            Node(
                id=str(decision.id),
                kind="Decision",
                label=decision.slug,
                summary=decision.text,
            )
        )
        nodes += 1

    edges = 0
    for edge in session.scalars(select(Edge)).all():
        graph.upsert_edge(
            GraphEdge(from_id=str(edge.from_id), to_id=str(edge.to_id), kind=edge.kind)
        )
        edges += 1

    return nodes, edges
