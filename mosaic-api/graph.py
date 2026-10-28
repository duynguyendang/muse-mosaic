"""Data Graph — first-class, persisted, queryable (CSD 3.2).

Source Graph (raw facts, never overwritten) + Derived Graph (proposed
clusters, proposed links, golden nodes, analyser facts; wiped and re-derived
per build, never hand-edited). SQLite tables graph_nodes/graph_edges; a
triple view over the facts table is the query interface, not a second DB.

Services in v1, minimal: Pathfinder (shortest proof chain, edge weights by
proof strength: exact identifier lowest cost, fuzzy higher, vector-only
highest), Impact/Blast Radius (what a golden-field change affects), and three
analysers — Orphan (fragment in no cluster), Stale (fragment past TTL,
taxonomy term long unused), Duplicate (near-duplicate not provable enough to
merge) — materialised as derived facts for steward visibility.
"""

from __future__ import annotations

from collections import deque
from typing import Any

import ledger
from schemas import CandidateFact, GraphEdge, GraphNode, ProposedLink, VerifiedLink

EDGE_WEIGHT = {
    "exact_email": 1,
    "exact_phone": 1,
    "exact_order": 1,
    "same_fragment": 1,
    "exact_address": 3,
    "fuzzy_name_address": 3,
    "vector_only": 5,
}


def node_id(kind: str, *parts: str) -> str:
    """Deterministic node ids so rebuilds converge."""
    return "nd-" + "-".join([kind, *[str(part) for part in parts]])


def edge_id(edge_type: str, from_id: str, to_id: str, proof: str | None = None) -> str:
    base = f"eg-{edge_type}-{from_id}-{to_id}"
    return base if not proof else f"{base}-{proof}"


def plan_graph(
    *,
    entity_type: str,
    entity_id: str,
    facts: list[CandidateFact],
    proposed_links: list[ProposedLink],
    verified_links: list[VerifiedLink],
    golden_version: int | None,
    decision_id: str,
    policy_version: str,
    taxonomy_version: str | None,
    created_at: str,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Pure graph planning (no writes): nodes + edges with provenance."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    for fact in facts:
        fragment_node = node_id("fragment", entity_type, entity_id, fact.source_id)
        value_node = node_id("value", entity_type, entity_id, fact.fact_id)
        nodes.append(GraphNode(
            node_id=fragment_node, node_type="Fragment",
            entity_type=entity_type, entity_id=entity_id,
            source=fact.source, created_at=created_at, decision_id=decision_id,
        ))
        nodes.append(GraphNode(
            node_id=value_node, node_type="Fragment",
            entity_type=entity_type, entity_id=entity_id,
            source=fact.source, created_at=created_at, decision_id=decision_id,
        ))
        edges.append(GraphEdge(
            edge_id=edge_id("has_attribute", fragment_node, value_node, fact.attribute),
            edge_type="has_attribute",
            from_node_id=fragment_node, to_node_id=value_node,
            proof=fact.attribute, proposer=fact.source,
            rule_fired=None, policy_version=policy_version,
            taxonomy_version=taxonomy_version, decision_id=decision_id,
        ))

    fact_nodes = {fact.fact_id: node_id("value", entity_type, entity_id, fact.fact_id)
                  for fact in facts}
    for link in proposed_links:
        if link.from_fact_id not in fact_nodes or link.to_fact_id not in fact_nodes:
            continue
        edges.append(GraphEdge(
            edge_id=edge_id("proposed_link", link.from_fact_id, link.to_fact_id, link.proof),
            edge_type="proposed_link",
            from_node_id=fact_nodes[link.from_fact_id],
            to_node_id=fact_nodes[link.to_fact_id],
            proof=link.proof, proposer=link.proposer,
            rule_fired=None, policy_version=policy_version,
            taxonomy_version=taxonomy_version, decision_id=decision_id,
        ))
    for link in verified_links:
        if link.from_fact_id not in fact_nodes or link.to_fact_id not in fact_nodes:
            continue
        edges.append(GraphEdge(
            edge_id=edge_id("verified_link", link.from_fact_id, link.to_fact_id, link.proof),
            edge_type="verified_link",
            from_node_id=fact_nodes[link.from_fact_id],
            to_node_id=fact_nodes[link.to_fact_id],
            proof=link.proof, proposer="manglekit",
            rule_fired=link.rule_fired, policy_version=link.policy_version,
            taxonomy_version=link.taxonomy_version or taxonomy_version,
            decision_id=decision_id,
        ))

    entity_node = node_id("entity", entity_type, entity_id)
    nodes.append(GraphNode(
        node_id=entity_node, node_type="Entity",
        entity_type=entity_type, entity_id=entity_id,
        source=None, created_at=created_at, decision_id=decision_id,
    ))
    if golden_version is not None:
        golden_node = node_id("golden", entity_type, entity_id, str(golden_version))
        nodes.append(GraphNode(
            node_id=golden_node, node_type="Golden",
            entity_type=entity_type, entity_id=entity_id,
            source=None, created_at=created_at, decision_id=decision_id,
        ))

    return nodes, edges


def insert_graph_rows(
    database_path_or_connection: Any,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    decision_id: str,
) -> None:
    """Insert graph rows inside the caller's transaction (publisher gate).

    Accepts a database path (own transaction) or an open sqlite3 connection
    (joins the caller's transaction: no commit here).
    """
    if isinstance(database_path_or_connection, (str, bytes)) or hasattr(
        database_path_or_connection, "__fspath__"
    ):
        _persist_plan(str(database_path_or_connection), nodes, edges, decision_id)
        return
    _insert_rows(database_path_or_connection, nodes, edges, decision_id)


def _persist_plan(database_path: str, nodes, edges, decision_id: str) -> None:
    connection = ledger.connect(database_path)
    try:
        with connection:
            _insert_rows(connection, nodes, edges, decision_id)
    finally:
        connection.close()


def _insert_rows(connection, nodes, edges, decision_id) -> None:
    for node in nodes:
        if node.node_type == "Fragment":
            connection.execute(
                """
                INSERT OR IGNORE INTO graph_nodes
                  (node_id, node_type, entity_type, entity_id, source,
                   created_at, decision_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (node.node_id, node.node_type, node.entity_type,
                 node.entity_id, node.source, node.created_at, node.decision_id),
            )
        else:
            connection.execute(
                """
                INSERT OR REPLACE INTO graph_nodes
                  (node_id, node_type, entity_type, entity_id, source,
                   created_at, decision_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (node.node_id, node.node_type, node.entity_type,
                 node.entity_id, node.source, node.created_at, node.decision_id),
            )
    # Derived edges are wiped and re-derived per decision; source
    # has_attribute rows persist (proposer provenance is history).
    connection.execute(
        "DELETE FROM graph_edges WHERE decision_id = ? AND edge_type != 'has_attribute'",
        (decision_id,),
    )
    for edge in edges:
        if edge.edge_type == "has_attribute":
            connection.execute(
                "INSERT OR IGNORE INTO graph_edges VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _edge_row(edge),
            )
        else:
            connection.execute(
                "INSERT OR REPLACE INTO graph_edges VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _edge_row(edge),
            )


def _edge_row(edge: GraphEdge) -> tuple:
    return (
        edge.edge_id, edge.edge_type, edge.from_node_id, edge.to_node_id,
        edge.proof, edge.proposer, edge.rule_fired, edge.policy_version,
        edge.taxonomy_version, edge.decision_id,
    )


def fetch_graph(database_path: str, entity_type: str, entity_id: str) -> tuple[list[dict], list[dict]]:
    """Read graph rows for one entity (canvas + Explain render this)."""
    connection = ledger.connect(database_path)
    try:
        node_rows = connection.execute(
            "SELECT * FROM graph_nodes WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        ).fetchall()
        edge_rows = connection.execute(
            """SELECT e.* FROM graph_edges e JOIN graph_nodes n
               ON n.node_id IN (e.from_node_id, e.to_node_id)
               WHERE n.entity_type = ? AND n.entity_id = ?""",
            (entity_type, entity_id),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in node_rows], [dict(row) for row in edge_rows]


def pathfinder(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
    from_fact_value: str, to_fact_value: str,
) -> list[str]:
    """Shortest proof chain between two fact nodes (Dijkstra by proof weight).

    Only proposed_link/verified_link edges are traversable; has_attribute
    edges are not paths. Returns node ids, empty when disconnected.
    """
    _ = nodes
    adjacency: dict[str, list[tuple[str, int, str]]] = {}
    for edge in edges:
        if edge["edge_type"] not in ("proposed_link", "verified_link"):
            continue
        weight = EDGE_WEIGHT.get(edge.get("proof") or "", 9)
        adjacency.setdefault(edge["from_node_id"], []).append(
            (edge["to_node_id"], weight, edge["edge_id"]))
        adjacency.setdefault(edge["to_node_id"], []).append(
            (edge["from_node_id"], weight, edge["edge_id"]))
    import heapq

    frontier: list[tuple[int, str, list[str]]] = [(0, from_fact_value, [from_fact_value])]
    best: dict[str, int] = {}
    while frontier:
        cost, node, path = heapq.heappop(frontier)
        if node in best and best[node] <= cost:
            continue
        best[node] = cost
        if node == to_fact_value:
            return path
        for neighbour, weight, _ in adjacency.get(node, []):
            heapq.heappush(frontier, (cost + weight, neighbour, path + [neighbour]))
    return []


def impact(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]], golden_field: str
) -> dict[str, Any]:
    """Blast radius of a golden field: downstream nodes/edges it touches."""
    downstream_nodes: set[str] = set()
    downstream_edges: list[str] = []
    queue: deque[str] = deque()
    for edge in edges:
        if edge.get("proof") == golden_field or golden_field in (edge.get("from_node_id") or ""):
            queue.append(edge["to_node_id"])
            downstream_edges.append(edge["edge_id"])
    seen = set(queue)
    while queue:
        node = queue.popleft()
        downstream_nodes.add(node)
        for edge in edges:
            if edge["from_node_id"] == node and edge["to_node_id"] not in seen:
                seen.add(edge["to_node_id"])
                downstream_edges.append(edge["edge_id"])
                queue.append(edge["to_node_id"])
    return {
        "field": golden_field,
        "downstream_nodes": sorted(downstream_nodes),
        "downstream_edges": downstream_edges,
    }


def analyse_orphans(facts: list[CandidateFact], links: list[ProposedLink]) -> list[str]:
    """Fragments in no cluster: fact ids touched by no link at all."""
    touched = {link.from_fact_id for link in links} | {link.to_fact_id for link in links}
    return sorted({fact.fact_id for fact in facts} - touched)


def analyse_stale(facts: list[CandidateFact], now_min: int) -> list[str]:
    """Fragments past TTL relative to evaluation time (mirror of R3)."""
    import datetime

    base = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=7)))
    stale = []
    for fact in facts:
        event_min = int((fact.event_time.timestamp() - base.timestamp()) // 60)
        if event_min + fact.ttl_seconds // 60 < now_min:
            stale.append(fact.fact_id)
    return sorted(stale)


def analyse_duplicates(facts: list[CandidateFact]) -> list[list[str]]:
    """Near-duplicate values not provable enough to merge (same attribute,
    equal after lowercasing, but different sources and neither verified)."""
    groups: dict[tuple[str, str], list[str]] = {}
    for fact in facts:
        if fact.verified:
            continue
        groups.setdefault((fact.attribute, fact.value.lower()), []).append(fact.fact_id)
    return sorted(
        [sorted(ids) for ids in groups.values() if len(ids) > 1],
        key=lambda ids: ids[0],
    )


def derivation_tree(
    verified_links: list[VerifiedLink] | list[dict[str, Any]],
    winner_fact_id: str | None,
) -> list[str]:
    """Explain drawer: human-readable derivation tree lines for the hops."""
    lines = []
    for link in verified_links:
        if isinstance(link, dict):
            frm, to, proof = link["from_fact_id"], link["to_fact_id"], link["proof"]
        else:
            frm, to, proof = link.from_fact_id, link.to_fact_id, link.proof
        lines.append(f"{frm} --{proof}--> {to} (verified, L1)")
    if winner_fact_id:
        lines.append(f"golden <= {winner_fact_id} (winner by R1)")
    return sorted(lines)


def derive_purchased_edges(
    documents: list[dict[str, Any]],
    product_goldens: dict[str, dict[str, Any]],
    *,
    decision_id: str,
    policy_version: str,
    taxonomy_version: str | None,
    created_at: str,
) -> tuple[list[GraphEdge], list[GraphNode]]:
    """Verify purchased edges (graph analyser, not engine derivation).

    A purchased edge user -> product verifies when one document carries both
    a product_ref and an order_id, and at least one other document shares that
    exact order_id (exact proof on the user side), AND the target product has
    a published golden. Otherwise the edge stays proposed with its reason.
    Only verified edges are assembled into nested goldens by the publisher.
    """
    order_docs: dict[str, list[str]] = {}
    for document in documents:
        order_id = (document.get("fields", {}).get("order_id") or {})
        order_value = order_id.get("value") if isinstance(order_id, dict) else order_id
        if order_value:
            order_docs.setdefault(str(order_value), []).append(document["document_id"])
    edges: list[GraphEdge] = []
    nodes: list[GraphNode] = []
    for document in documents:
        fields = document.get("fields", {})
        product_ref = fields.get("product_ref")
        product_id = product_ref.get("value") if isinstance(product_ref, dict) else product_ref
        order_field = fields.get("order_id")
        order_value = order_field.get("value") if isinstance(order_field, dict) else order_field
        if not product_id or not order_value:
            continue
        shared = [doc for doc in order_docs.get(str(order_value), []) if doc != document["document_id"]]
        from_node = node_id("fragment", document.get("entity_type", "user"), document.get("entity_id", ""), document["document_id"])
        golden = product_goldens.get(str(product_id))
        if shared and golden is not None:
            to_node = node_id("golden", "product", str(product_id), str(golden.get("version", 1)))
            edges.append(GraphEdge(
                edge_id=edge_id("purchased", from_node, to_node, "exact_order"),
                edge_type="purchased",
                from_node_id=from_node,
                to_node_id=to_node,
                proof="exact_order",
                proposer="graph-analyser",
                rule_fired="L1",
                policy_version=policy_version,
                taxonomy_version=taxonomy_version,
                decision_id=decision_id,
            ))
        else:
            to_node = node_id("entity", "product", str(product_id))
            edges.append(GraphEdge(
                edge_id=edge_id("proposed_link", from_node, to_node, "order_only"),
                edge_type="proposed_link",
                from_node_id=from_node,
                to_node_id=to_node,
                proof="order_only",
                proposer="graph-analyser",
                rule_fired=None,
                policy_version=policy_version,
                taxonomy_version=taxonomy_version,
                decision_id=decision_id,
            ))
            nodes.append(GraphNode(
                node_id=to_node, node_type="Entity",
                entity_type="product", entity_id=str(product_id),
                source=None, created_at=created_at, decision_id=decision_id,
            ))
    return edges, nodes
