"""Policy hot-swap: re-run Stage 03+04 over cached Stage 02 clusters.

The pipeline's five stages are not equally expensive. Stage 01 (ingest) and
Stage 02 (Splink DuckDB partitioning into disjoint components) are the
probabilistic, data-dependent stages; Stage 03 (Manglekit Datalog) and Stage
04 (monotonic fixpoint) are pure functions of (facts, policy). Policy
switching therefore only ever re-executes the latter two -- the raw intake and
the clustering are never repeated.

Latency model, measured rather than assumed:

* ``mkit eval`` floors at **20.26 ms** per invocation (process spawn + policy
  parse + teardown) and scales **quadratically** with fact count (100 facts ->
  50 ms, 1000 -> 2192 ms). Batching all clusters into one call is therefore
  slower, not faster: it timed out beyond 120 s.
* The Datalog solver lives in manglekit's ``internal/engine``, which Go's
  ``internal/`` rule makes unimportable from the trust-engine, so the engine
  cannot be embedded to skip the spawn.
* ``mkit serve`` is an agent-governance HTTP API (``core.Envelope`` routed by
  ``next_step``), not an eval endpoint.

So per-cluster subprocess cost is irreducible, and the spec's "re-evaluate in
<= 15 ms" cannot be met by re-executing 247 clusters x 1 query (~15 s). The
design that does meet the *user-visible* goal -- instant switching -- is to
precompute every profile once when the Stage 02 cache is built, then make a
switch a cache lookup. Precompute costs ~55 s for three profiles over 247
clusters; every switch after that is a dictionary read.

Nothing in this module writes to the ledger. A policy preview must never
become a publish: the only path that publishes remains POST /build and
POST /jobs, which run the full publisher gate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from policy_profiles import (
    Profile,
    attestation_root,
    default_profile_id,
    get_profile,
    load_profiles,
    stable_hash,
)

logger = logging.getLogger("mosaic-api")


# A cached Stage 02 result: the disjoint components plus the fingerprint that
# tells us it is still valid. Fingerprinting the input means a new build with
# different documents invalidates the cache instead of silently serving
# metrics computed over yesterday's clusters.
@dataclass
class Stage02Cache:
    clusters: list[dict[str, Any]]
    fingerprint: str
    built_at: float = field(default_factory=time.time)
    source: str = "ledger"
    # Cheap fingerprint (row count + max rowid) used to skip the full fact
    # re-read on every switch. A policy switch must not pay for reading
    # 13k rows it already holds in memory.
    quick: str = ""


@dataclass
class ProfileResult:
    """One precomputed profile's metrics plus the attestation inputs."""

    profile_id: str
    policy_hash: str
    metrics: dict[str, Any]
    golden_facts_root: str
    cluster_graph_root: str
    merkle_root: str
    computed_at: float = field(default_factory=time.time)
    precompute_ms: float = 0.0


class PolicyController:
    """Owns the Stage 02 cache and the precomputed profile results."""

    def __init__(self) -> None:
        self._stage02: Stage02Cache | None = None
        self._results: dict[str, ProfileResult] = {}
        self._lock = asyncio.Lock()
        self._precomputing = False

    # ------------------------------------------------------------- Stage 02
    @property
    def stage02(self) -> Stage02Cache | None:
        return self._stage02

    @property
    def precomputing(self) -> bool:
        return self._precomputing

    def note_quick(self, quick: str) -> None:
        """Attach the cheap fingerprint to the current cache, if any."""
        if self._stage02 is not None:
            self._stage02.quick = quick

    def invalidate(self) -> None:
        """Drop everything: called when a new build changes the clusters."""
        self._stage02 = None
        self._results = {}

    def has_stage02(self) -> bool:
        return self._stage02 is not None

    # ------------------------------------------------------------ attestation
    @staticmethod
    def cluster_graph_root(clusters: list[dict[str, Any]]) -> str:
        """Hash of the Stage 02 partition itself.

        Only membership is hashed, not fact values: two runs that cluster the
        same documents identically produce the same root, which is what makes
        it a statement about the *graph* rather than about the data.
        """
        membership = sorted(
            (
                c.get("entity_type", ""),
                c.get("entity_id", ""),
                tuple(sorted(str(f.get("fact_id", "")) for f in c.get("candidate_facts", []))),
            )
            for c in clusters
        )
        return stable_hash(membership)

    @staticmethod
    def golden_facts_root(metrics: dict[str, Any]) -> str:
        """Hash of the published outcomes a profile would produce.

        Derived from the verdict rows rather than from ledger goldens, because
        a preview has not written anything: this is the root of what WOULD be
        published under that profile.
        """
        published = sorted(
            (c.get("entity_type", ""), c.get("entity_id", ""), attr)
            for c in metrics.get("clusters", [])
            for attr in c.get("published_attrs", [])
        )
        return stable_hash(published)

    # -------------------------------------------------------------- precompute
    async def ensure_ready(
        self,
        *,
        clusters: list[dict[str, Any]],
        fingerprint: str,
        quick: str = "",
        trust_url: str,
        taxonomy_version: str,
        profiles: dict[str, Profile] | None = None,
        concurrency: int = 4,
    ) -> dict[str, ProfileResult]:
        """Build the Stage 02 cache if needed, then precompute all profiles.

        Called on the first apply-policy request for a given cluster set. The
        lock means concurrent callers wait for one precompute rather than
        launching three.
        """
        async with self._lock:
            if self._stage02 is None or self._stage02.fingerprint != fingerprint:
                self._stage02 = Stage02Cache(
                    clusters=clusters,
                    fingerprint=fingerprint,
                    quick=quick,
                )
                self._results = {}
            if set(self._results) == set(profiles or load_profiles()):
                return dict(self._results)

            self._precomputing = True
            try:
                await self._precompute_all(
                    trust_url=trust_url,
                    taxonomy_version=taxonomy_version,
                    profiles=profiles or load_profiles(),
                    concurrency=concurrency,
                )
            finally:
                self._precomputing = False
            return dict(self._results)

    async def _precompute_all(
        self,
        *,
        trust_url: str,
        taxonomy_version: str,
        profiles: dict[str, Profile],
        concurrency: int,
    ) -> None:
        assert self._stage02 is not None
        graph_root = self.cluster_graph_root(self._stage02.clusters)
        for profile_id, profile in profiles.items():
            started = time.perf_counter()
            try:
                metrics = await self._evaluate(
                    profile=profile,
                    clusters=self._stage02.clusters,
                    trust_url=trust_url,
                    taxonomy_version=taxonomy_version,
                    concurrency=concurrency,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
                logger.error("precompute failed for profile %s: %s", profile_id, exc)
                raise
            golden_root = self.golden_facts_root(metrics)
            self._results[profile_id] = ProfileResult(
                profile_id=profile_id,
                policy_hash=profile.policy_hash,
                metrics=metrics,
                golden_facts_root=golden_root,
                cluster_graph_root=graph_root,
                merkle_root=attestation_root(golden_root, graph_root, profile.policy_hash),
                precompute_ms=(time.perf_counter() - started) * 1000,
            )
            logger.info(
                "precomputed profile %s in %.0f ms (goldens=%s quarantined=%s)",
                profile_id,
                (time.perf_counter() - started) * 1000,
                metrics.get("certified_goldens_count"),
                metrics.get("quarantined_clusters_count"),
            )

    async def _evaluate(
        self,
        *,
        profile: Profile,
        clusters: list[dict[str, Any]],
        trust_url: str,
        taxonomy_version: str,
        concurrency: int,
    ) -> dict[str, Any]:
        """Call the trust-engine's /metrics for one profile."""
        payload = {
            "policy_version": profile.policy_versions.get("user")
            or next(iter(profile.policy_versions.values())),
            "policy_versions": profile.policy_versions,
            "taxonomy_version": taxonomy_version,
            "clusters": clusters,
            "concurrency": concurrency,
        }
        url = trust_url.rsplit("/", 1)[0] + "/metrics"
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("errors"):
            # Partial results are still returned, but a non-empty error list
            # means the numbers do not cover every cluster -- the caller must
            # see that rather than trust a total that quietly dropped rows.
            logger.warning(
                "profile %s evaluated with %d cluster errors",
                profile.profile_id,
                len(data["errors"]),
            )
        return data

    # ----------------------------------------------------------------- lookup
    def get(self, profile_id: str) -> ProfileResult | None:
        return self._results.get(profile_id)

    def all_results(self) -> dict[str, ProfileResult]:
        return dict(self._results)

    def status(self) -> dict[str, Any]:
        return {
            "stage02_cached": self._stage02 is not None,
            "cluster_count": len(self._stage02.clusters) if self._stage02 else 0,
            "fingerprint": self._stage02.fingerprint if self._stage02 else None,
            "precomputing": self._precomputing,
            "profiles_ready": sorted(self._results),
        }


# Module-level singleton: one controller per process, shared by the routes.
controller = PolicyController()


def resolve_profile(profile_id: str | None) -> Profile:
    """Resolve a profile id, falling back to the default.

    An unknown id raises rather than silently degrading to the default: a
    caller that asked for ``retail_balanced`` and silently got the strict
    profile would see numbers that look authoritative and are wrong.
    """
    wanted = profile_id or default_profile_id()
    profile = get_profile(wanted)
    if profile is None:
        available = ", ".join(sorted(load_profiles()))
        raise KeyError(f"unknown policy profile {wanted!r}; available: {available}")
    return profile
