"""知識圖譜的寫入端。

Postgres 的 edges 表是真相，這裡只是投影 —— 所以 rebuild() 必須永遠可以
從零重建。測試預設用 FakeGraph，不需要起 Neo4j。
"""

from dataclasses import dataclass
from typing import Protocol

from neo4j import Driver, GraphDatabase

from devloop.core.config import get_settings


@dataclass(frozen=True)
class Node:
    id: str
    kind: str  # Card / Decision / Risk / Spec
    label: str
    summary: str = ""


@dataclass(frozen=True)
class Edge:
    from_id: str
    to_id: str
    kind: str


class GraphStore(Protocol):
    def upsert_node(self, node: Node) -> None: ...
    def upsert_edge(self, edge: Edge) -> None: ...
    def clear(self) -> None: ...
    def close(self) -> None: ...


class FakeGraph:
    """測試用。行為上與 Neo4jGraph 等價，資料放在記憶體。"""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: set[tuple[str, str, str]] = set()

    def upsert_node(self, node: Node) -> None:
        self.nodes[node.id] = node

    def upsert_edge(self, edge: Edge) -> None:
        self.edges.add((edge.from_id, edge.to_id, edge.kind))

    def clear(self) -> None:
        self.nodes.clear()
        self.edges.clear()

    def close(self) -> None:
        return None


class Neo4jGraph:
    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    @classmethod
    def from_settings(cls) -> "Neo4jGraph":
        s = get_settings()
        driver = GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
        return cls(driver)

    def upsert_node(self, node: Node) -> None:
        # 節點標籤不能參數化，所以只允許白名單裡的種類
        if node.kind not in _NODE_KINDS:
            raise ValueError(f"unknown node kind: {node.kind}")
        query = f"MERGE (n:{node.kind} {{id: $id}}) SET n.label = $label, n.summary = $summary"
        with self._driver.session() as session:
            session.run(query, id=node.id, label=node.label, summary=node.summary)

    def upsert_edge(self, edge: Edge) -> None:
        if edge.kind not in _EDGE_KINDS:
            raise ValueError(f"unknown edge kind: {edge.kind}")
        query = f"MATCH (a {{id: $from_id}}), (b {{id: $to_id}}) MERGE (a)-[:{edge.kind}]->(b)"
        with self._driver.session() as session:
            session.run(query, from_id=edge.from_id, to_id=edge.to_id)

    def clear(self) -> None:
        with self._driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")

    def close(self) -> None:
        self._driver.close()


_NODE_KINDS = frozenset({"Card", "Decision", "Risk", "Spec"})
_EDGE_KINDS = frozenset({"PRODUCED", "FEEDS", "RAISED", "REVISES"})
