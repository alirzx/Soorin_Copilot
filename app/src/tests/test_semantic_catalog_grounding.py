"""Regression tests for active-catalog semantic class grounding."""

from __future__ import annotations

from src.core.context.semantic_catalog import SemanticCatalogProvider
from src.core.graph.structured import AssetPredicate, StructuredQuerySpec


class _FakeGraphService:
    version = "graph-v1"
    catalog = {
        "suggested_type": (
            "Linux Server",
            "Windows Server",
            "Windows Workstation",
            "Database Server",
            "Domain Controller",
            "Firewall",
        ),
        "role": (
            "Linux Server",
            "Windows Server",
            "Windows Workstation",
            "Database Server",
            "Domain Controller",
            "Firewall",
        ),
        "roles": (
            "Linux Server",
            "Windows Server",
            "Windows Workstation",
            "Database Server",
            "Domain Controller",
            "Firewall",
        ),
        "vendor": ("Microsoft", "VMware"),
        "product": ("FortiGate",),
        "tag": (),
        "sub_tag": (),
        "status": ("CONFIRMED",),
        "enrichment_status": ("success",),
    }

    def semantic_catalog_version(self) -> str:
        return self.version

    def semantic_catalog_values(self, _version: str, *, per_field_limit: int):
        return {
            field: tuple(values[:per_field_limit])
            for field, values in self.catalog.items()
        }

    def canonicalize_semantic_values(self, _version: str, requested):
        resolved: dict[str, dict[str, str]] = {}
        for field, values in requested.items():
            canonical = {
                value.casefold(): value
                for value in self.catalog.get(field, ())
            }
            for value in values:
                match = canonical.get(str(value).casefold())
                if match is not None:
                    resolved.setdefault(field, {})[str(value).casefold()] = match
        return resolved


def _generic_class_query(term: str) -> StructuredQuerySpec:
    return StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {
            "predicate": {
                "any": [
                    {"field": "suggested_type", "operator": "eq", "value": term},
                    {"field": "role", "operator": "eq", "value": term},
                    {"field": "roles", "operator": "member_eq", "value": term},
                ]
            }
        },
        "semantic_class": term,
        "class_mapping_mode": "generic_asset_class",
        "class_selector_fields": ["suggested_type", "role", "roles"],
    })


def _leaf_pairs(predicate: AssetPredicate | None) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    if predicate is None:
        return pairs
    for child in (*predicate.all, *predicate.any):
        pairs.update(_leaf_pairs(child))
    if predicate.not_ is not None:
        pairs.update(_leaf_pairs(predicate.not_))
    if predicate.field is not None and predicate.value is not None:
        pairs.add((predicate.field.value, str(predicate.value)))
    for value in predicate.values:
        if predicate.field is not None:
            pairs.add((predicate.field.value, str(value)))
    return pairs


def test_linux_broad_concept_expands_to_current_linux_server_value() -> None:
    provider = SemanticCatalogProvider(_FakeGraphService())
    query = provider.canonicalize_query(_generic_class_query("Linux"))

    assert query.class_mapping_mode == "generic_asset_class_catalog_expansion"
    pairs = _leaf_pairs(query.filters.predicate)
    assert {value for _, value in pairs} == {"Linux Server"}
    assert {field for field, _ in pairs} == {"suggested_type", "role", "roles"}


def test_database_broad_concept_expands_without_static_alias_table() -> None:
    provider = SemanticCatalogProvider(_FakeGraphService())
    query = provider.canonicalize_query(_generic_class_query("database"))

    assert {value for _, value in _leaf_pairs(query.filters.predicate)} == {
        "Database Server"
    }


def test_windows_broad_concept_intentionally_covers_all_current_windows_variants() -> None:
    provider = SemanticCatalogProvider(_FakeGraphService())
    query = provider.canonicalize_query(_generic_class_query("Windows"))

    values = {value for _, value in _leaf_pairs(query.filters.predicate)}
    assert values == {"Windows Server", "Windows Workstation"}


def test_domain_abbreviation_can_resolve_by_catalog_initialism() -> None:
    provider = SemanticCatalogProvider(_FakeGraphService())
    query = provider.canonicalize_query(_generic_class_query("DC"))

    assert {value for _, value in _leaf_pairs(query.filters.predicate)} == {
        "Domain Controller"
    }


def test_exact_class_keeps_exact_canonical_value_without_broadening() -> None:
    provider = SemanticCatalogProvider(_FakeGraphService())
    query = provider.canonicalize_query(_generic_class_query("firewall"))

    assert query.class_mapping_mode == "generic_asset_class"
    assert {value for _, value in _leaf_pairs(query.filters.predicate)} == {"Firewall"}


def test_partial_matching_is_not_applied_to_arbitrary_vendor_selector() -> None:
    provider = SemanticCatalogProvider(_FakeGraphService())
    original = StructuredQuerySpec.model_validate({
        "mode": "search",
        "filters": {"vendor": "Micro"},
    })
    query = provider.canonicalize_query(original)

    assert query.filters.vendor == "Micro"
    assert query.semantic_class is None
