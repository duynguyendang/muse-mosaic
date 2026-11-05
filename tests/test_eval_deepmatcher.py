"""DeepMatcher benchmark harness: metric maths and raw-data provenance.

The scoring functions are the instrument, so they are checked against a
hand-computed case with no data files involved. The provenance checks only
run when the gitignored raw zips have been fetched locally, and they pin the
published DeepMatcher counts so a stale or wrong download cannot silently
become a "benchmark".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_deepmatcher as ed  # noqa: E402

# Two truth entities: {A-1,B-1,B-2} and {A-2,B-3}; A-3 and B-4 are singletons.
DOCS = [
    {"document_id": "A-1", "entity_type": "product", "source": "amazon", "fields": {}},
    {"document_id": "B-1", "entity_type": "product", "source": "google", "fields": {}},
    {"document_id": "B-2", "entity_type": "product", "source": "google", "fields": {}},
    {"document_id": "A-2", "entity_type": "product", "source": "amazon", "fields": {}},
    {"document_id": "B-3", "entity_type": "product", "source": "google", "fields": {}},
    {"document_id": "A-3", "entity_type": "product", "source": "amazon", "fields": {}},
    {"document_id": "B-4", "entity_type": "product", "source": "google", "fields": {}},
]
TRUTH = {
    "A-1": "t1",
    "B-1": "t1",
    "B-2": "t1",
    "A-2": "t2",
    "B-3": "t2",
    "A-3": "solo-a3",
    "B-4": "solo-b4",
}


def test_pairwise_scoring_is_hand_checkable():
    """Predicted {A-1,B-1,B-2,A-2} vs truth {t1:3, t2:2} -> tp=3, pred=6, true=4."""
    clusters = [["A-1", "B-1", "B-2", "A-2"], ["A-3"], ["B-4"]]
    row = ed.score(clusters, TRUTH)
    # true pairs: t1 -> 3 choose 2 = 3, t2 -> 1, singletons -> 0
    assert row["true_pairs"] == 4
    # predicted pairs: 4 choose 2 = 6, plus two singletons
    assert row["predicted_pairs"] == 6
    # same-truth pairs inside the predicted cluster: A-1/B-1, A-1/B-2, B-1/B-2 = 3
    assert row["true_positives"] == 3
    assert row["precision"] == pytest.approx(0.5)
    assert row["recall"] == pytest.approx(0.75)
    assert row["f1"] == pytest.approx(0.6)
    assert row["clusters"] == 3
    # the cluster spans t1 and t2, so it is a false merge
    assert row["impure_clusters"] == 1


def test_perfect_clustering_scores_one_and_no_impurity():
    clusters = [["A-1", "B-1", "B-2"], ["A-2", "B-3"], ["A-3"], ["B-4"]]
    row = ed.score(clusters, TRUTH)
    assert (row["precision"], row["recall"], row["f1"]) == (1.0, 1.0, 1.0)
    assert row["impure_clusters"] == 0


def test_candidate_recall_measures_the_closure_not_the_labelled_pairs():
    """A 3-row truth cluster contributes 3 golden pairs from 2 labelled ones."""
    candidates = {("A-1", "B-1"), ("A-1", "B-2")}  # misses B-1/B-2
    recall, found, golden = ed.candidate_recall(DOCS, candidates, TRUTH)
    assert golden == 4  # 3 pairs inside t1 + 1 inside t2
    assert found == 2
    assert recall == pytest.approx(0.5)


def test_exact_blocking_strategies_agree_on_a_shared_title():
    docs = [
        {"document_id": "A-1", "fields": {"name": {"value": "Widget Pro"}}},
        {"document_id": "B-1", "fields": {"name": {"value": "widget pro"}}},
        {"document_id": "B-2", "fields": {"name": {"value": "Other Thing"}}},
    ]
    assert ed.blocking_candidates(docs, "exact_raw") == set()  # case differs
    assert ed.blocking_candidates(docs, "exact_norm") == {("A-1", "B-1")}


def test_hot_tokens_block_nothing_like_the_pipeline_guard():
    """A token shared by more than 50 clusters must not create candidates."""
    docs = [
        {"document_id": f"D-{i}", "fields": {"name": {"value": f"common {i}"}}} for i in range(60)
    ]
    assert ed.blocking_candidates(docs, "tokens_any") == set()


@pytest.mark.parametrize(
    "dataset,rows_a,rows_b,labelled,positives",
    [
        ("amazon-google", 1363, 3226, 11460, 1167),
        ("dblp-acm-dirty", 2616, 2294, 12363, 2220),
    ],
)
def test_raw_data_matches_published_deepmatcher_counts(
    dataset, rows_a, rows_b, labelled, positives
):
    """Only when the gitignored zips are present; pins the published numbers."""
    spec = ed.DATASETS[dataset]
    if not (ed.DATA / spec["zip"]).is_file():
        pytest.skip(f"{spec['zip']} not fetched locally (data/deepmatcher/ is gitignored)")
    _docs, _truth, stats = ed.load(spec)
    assert (stats["rows_a"], stats["rows_b"]) == (rows_a, rows_b)
    assert stats["pairs_labelled"] == labelled
    assert stats["pairs_positive"] == positives
