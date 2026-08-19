from devloop.graph.client import Edge, FakeGraph, Node


def test_fake_graph_upsert_is_idempotent() -> None:
    g = FakeGraph()
    g.upsert_node(Node(id="a", kind="Card", label="KAN-15"))
    g.upsert_node(Node(id="a", kind="Card", label="KAN-15", summary="改過"))
    g.upsert_edge(Edge(from_id="a", to_id="b", kind="PRODUCED"))
    g.upsert_edge(Edge(from_id="a", to_id="b", kind="PRODUCED"))

    assert len(g.nodes) == 1
    assert g.nodes["a"].summary == "改過"
    assert len(g.edges) == 1


def test_clear_empties_everything() -> None:
    g = FakeGraph()
    g.upsert_node(Node(id="a", kind="Card", label="KAN-15"))
    g.upsert_edge(Edge(from_id="a", to_id="b", kind="FEEDS"))
    g.clear()
    assert not g.nodes and not g.edges
