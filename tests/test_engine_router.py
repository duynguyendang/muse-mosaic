"""Deterministic engine router: a rule, never a benchmark result."""

from __future__ import annotations

from engine_router import route_dataset, route_entity


def _doc(entity_type: str, email: str | None = None, phone: str | None = None) -> dict:
    fields = {}
    if email:
        fields["email"] = {"value": email}
    if phone:
        fields["phone"] = {"value": phone}
    return {"document_id": f"d-{email}-{phone}", "entity_type": entity_type, "fields": fields}


def _docs(entity_type: str, n: int, coverage: float) -> list[dict]:
    with_id = int(n * coverage)
    docs = [_doc(entity_type, email=f"a{i}@x.invalid") for i in range(with_id)]
    docs += [_doc(entity_type) for _ in range(n - with_id)]
    return docs


def test_small_dataset_routes_exact() -> None:
    decision = route_entity(_docs("user", 50, 1.0), ["email"], "user")
    assert decision.engine == "exact" and decision.splink is False
    assert "200" in decision.reason or "EM" in decision.reason


def test_high_identifier_coverage_routes_exact() -> None:
    decision = route_entity(_docs("user", 5000, 0.9), ["email", "phone"], "user")
    assert decision.engine == "exact" and decision.splink is False


def test_sparse_identifiers_route_muse_and_keep_splink() -> None:
    decision = route_entity(_docs("user", 5000, 0.1), ["email", "phone"], "user")
    assert decision.engine == "muse" and decision.recommend_muse is True and decision.splink is True


def test_moderate_coverage_routes_splink() -> None:
    decision = route_entity(_docs("user", 5000, 0.5), ["email", "phone"], "user")
    assert decision.engine == "splink" and decision.splink is True and not decision.recommend_muse


def test_routing_is_deterministic() -> None:
    docs = _docs("user", 300, 0.5)
    first = route_dataset(docs, {"user": ["email"]})
    second = route_dataset(docs, {"user": ["email"]})
    assert first == second


def test_routing_ignores_goldens_and_labels() -> None:
    """The rule's inputs are documents and blocking lists. Two datasets with
    identical shape but different would-be goldens route identically — the
    router cannot engine-shop, because it has no labels to shop with."""
    shape_a = _docs("user", 300, 0.5)
    shape_b = [{**doc, "fields": {**doc["fields"], "golden": {"value": "x"}}} for doc in shape_a]
    assert route_dataset(shape_a, {"user": ["email"]}) == route_dataset(
        shape_b, {"user": ["email"]}
    )


def test_most_demanding_entity_decides() -> None:
    docs = _docs("product", 5000, 0.9) + _docs("user", 5000, 0.1)
    decision = route_dataset(docs, {"product": ["email"], "user": ["email", "phone"]})
    assert decision.engine == "muse"
    per_entity = {
        row["basis"]["entity_type"]: row["engine"] for row in decision.basis["per_entity"]
    }
    assert per_entity == {"product": "exact", "user": "muse"}


def test_router_decision_serialisation() -> None:
    decision = route_dataset(_docs("user", 10, 1.0), {"user": ["email"]})
    payload = decision.to_dict()
    assert set(payload) == {"engine", "splink", "recommend_muse", "reason", "basis"}
