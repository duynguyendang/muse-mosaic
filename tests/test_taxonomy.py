"""Taxonomy tests (CSD 3.5): versioned vocabulary, exact-match mapping only."""

from __future__ import annotations


def taxonomies():
    from taxonomies import load_taxonomies

    return load_taxonomies("data/mock/taxonomy")


def test_taxonomy_versions_load():
    from taxonomies import default_version

    all_tax = taxonomies()
    assert set(all_tax) == {"tax-user-2026-10-27-v1", "tax-product-2026-10-27-v1"}
    assert default_version(all_tax, "user") == "tax-user-2026-10-27-v1"
    assert default_version(all_tax, "product") == "tax-product-2026-10-27-v1"


def test_alias_mapping_never_guesses():
    from taxonomies import canonical_attribute

    tax = taxonomies()["tax-user-2026-10-27-v1"]
    canonical, mapped = canonical_attribute(tax, "customer_phone")
    assert (canonical, mapped) == ("phone", True)
    canonical, mapped = canonical_attribute(tax, "phone")
    assert (canonical, mapped) == ("phone", False)
    canonical, mapped = canonical_attribute(tax, "mystery_field")
    assert (canonical, mapped) == ("mystery_field", False)


def test_synonym_exact_match_only():
    from taxonomies import canonical_term, normalize_text

    tax = taxonomies()["tax-product-2026-10-27-v1"]
    assert normalize_text("Điện Thoại  ") == normalize_text("dienthoai")
    canonical, kind = canonical_term(tax, "category", "Điện thoại")
    assert (canonical, kind) == ("Smartphone", "synonym_exact")
    canonical, kind = canonical_term(tax, "category", "Smartphone")
    assert (canonical, kind) == ("Smartphone", "term_exact")
    # Near-miss is NOT mapped: it must escalate at the engine (T1), never
    # silently become a canonical term.
    canonical, kind = canonical_term(tax, "category", "Smart phone")
    assert (canonical, kind) == ("Smart phone", "unmapped")
    canonical, kind = canonical_term(tax, "category", "Phụ kiện điện thoại")
    assert kind == "unmapped"
