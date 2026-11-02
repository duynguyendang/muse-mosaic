// Metrics evaluation: re-run Stage 03 (Datalog derive) over an already
// clustered fact set under a chosen policy profile, without re-running
// Stage 01 ingestion or Stage 02 Splink partitioning.
//
// This exists because policy switching must not pay for the two probabilistic
// stages. It deliberately runs ONE manglekit query per cluster (`outcome`)
// rather than the three that /derive runs (`outcome`, `loser`, `vlink`):
// metrics only need the verdict per attribute, and loser/vlink are only
// required when materialising golden facts with full lineage. That is a 3x
// reduction in subprocess spawns, which is the dominant cost (mkit floors at
// ~20ms per invocation of pure spawn + parse + teardown).
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"sync"
	"time"
)

// MetricsRequest is the body of POST /metrics.
//
// Clusters carry their entity type because one evaluation may span user and
// product entities, each pinned to its own policy version.
type MetricsRequest struct {
	PolicyVersion   string         `json:"policy_version"`
	TaxonomyVersion string         `json:"taxonomy_version"`
	Clusters        []ClusterInput `json:"clusters"`
	Concurrency     int            `json:"concurrency,omitempty"`
	// PolicyVersions pins a policy version per entity type for evaluations
	// that span user and product in one call. Falls back to PolicyVersion.
	PolicyVersions map[string]string `json:"policy_versions,omitempty"`
}

// ClusterInput is one cached Stage 02 subgraph: an entity id plus the facts
// that belong to it. Facts and links use the same shapes /derive accepts.
//
// Links are required for fidelity, not decoration: without them the `L1`
// (verified hop) and `C2` (weakest-link chain) rules can never fire, so a
// preview would report fewer quarantines than a real rebuild of the same
// cluster. The build pipeline always passes links.
type ClusterInput struct {
	EntityType     string          `json:"entity_type"`
	EntityID       string          `json:"entity_id"`
	CandidateFacts []CandidateFact `json:"candidate_facts"`
	Links          []linkInput     `json:"links,omitempty"`
}

// ClusterMetrics is the per-cluster roll-up returned for one entity.
type ClusterMetrics struct {
	EntityType     string   `json:"entity_type"`
	EntityID       string   `json:"entity_id"`
	Verdict        string   `json:"verdict"`
	PublishedAttrs []string `json:"published_attrs"`
	EscalatedAttrs []string `json:"escalated_attrs"`
	StewardAttrs   []string `json:"steward_attrs"`
	RulesFired     []string `json:"rules_fired"`
	// RuleCounts is how many rows each rule produced in this cluster (the
	// honest meaning of "N Fired"). RulesFired is the distinct set, kept for
	// the cluster row itself.
	RuleCounts     map[string]int `json:"rule_counts,omitempty"`
	AttributeCount int            `json:"attribute_count"`
	// LinksEvaluated is how many Stage 02 links reached manglekit for this
	// cluster. Zero means the L1/C2 rules had no graph to walk and their
	// counters are not meaningful for this cluster.
	LinksEvaluated int `json:"links_evaluated"`
	// FactsFiltered counts facts dropped because their attribute is not in
	// the pinned policy's allowed_attributes. The build pipeline applies the
	// same filter upstream in document_to_fragments(..., allowed); mirroring
	// it here keeps metrics consistent with a real rebuild instead of
	// failing the whole cluster on an attribute the policy never governs.
	FactsFiltered int `json:"facts_filtered"`
}

// MetricsResponse aggregates a full profile evaluation.
type MetricsResponse struct {
	PolicyVersion     string            `json:"policy_version"`
	PolicyHash        string            `json:"policy_hash"`
	PolicyHashes      map[string]string `json:"policy_hashes"`
	TaxonomyVersion   string            `json:"taxonomy_version"`
	ClustersEvaluated int               `json:"clusters_evaluated"`
	FactsEvaluated    int               `json:"facts_evaluated"`
	FactsFiltered     int               `json:"facts_filtered"`
	LinksEvaluated    int               `json:"links_evaluated"`
	// Cluster verdicts are DISJOINT, so they always sum to ClustersEvaluated
	// (minus per-cluster errors): certified + quarantined + denied.
	//   certified   = at least one attribute published and none escalated
	//   quarantined = at least one attribute escalated (held for a steward)
	//   denied      = nothing published, nothing escalated
	// PartialGoldensCount is a SUBSET of quarantined: held clusters that still
	// published some attributes. It is reported separately so those attributes
	// are visible without being counted as certified.
	CertifiedGoldensCount    int              `json:"certified_goldens_count"`
	QuarantinedClustersCount int              `json:"quarantined_clusters_count"`
	DeniedClustersCount      int              `json:"denied_clusters_count"`
	PartialGoldensCount      int              `json:"partial_goldens_count"`
	StewardsTicketsCount     int              `json:"stewards_tickets_count"`
	ProofTreeDepth           int              `json:"proof_tree_depth"`
	ExecutionLatencyMs       float64          `json:"execution_latency_ms"`
	VerdictCounts            map[string]int   `json:"verdict_counts"`
	ReasonCounts             map[string]int   `json:"reason_counts"`
	Clusters                 []ClusterMetrics `json:"clusters"`
	Errors                   []ClusterError   `json:"errors,omitempty"`
}

// ClusterError records a cluster that could not be evaluated. It is returned
// rather than swallowed: a metrics number computed over a silently truncated
// input would be a fabricated proof.
type ClusterError struct {
	EntityType string `json:"entity_type"`
	EntityID   string `json:"entity_id"`
	Reason     string `json:"reason"`
}

// PolicyHash returns SHA-256 over the Horn clause text of a policy .mg.
// The same value is folded into the attestation root, so a policy edit
// changes the root even when no fact changes.
func (e *Engine) PolicyHash(entityType, policyVersion string) (string, error) {
	path, err := e.resolvePolicyFile(entityType, policyVersion)
	if err != nil {
		return "", err
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", &RequestError{Code: "INTERNAL_ERROR", Message: "cannot read policy file: " + err.Error()}
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

// EvaluateMetrics runs Stage 03+04 over cached clusters under one policy.
//
// It reuses canonicalize() so the input handed to manglekit is byte-identical
// to what /derive would produce for the same facts: identical facts derive
// identical verdicts, which is what makes a precomputed profile trustworthy
// as a stand-in for a full rebuild.
func (e *Engine) EvaluateMetrics(req MetricsRequest) (*MetricsResponse, error) {
	if len(req.Clusters) == 0 {
		return nil, &RequestError{Code: "INVALID_REQUEST", Message: "clusters must not be empty; fail closed"}
	}
	if req.PolicyVersion == "" {
		return nil, &RequestError{Code: "INVALID_REQUEST", Message: "policy_version is required; fail closed"}
	}
	taxonomyVersion := req.TaxonomyVersion
	if taxonomyVersion == "" {
		return nil, &RequestError{Code: "INVALID_REQUEST", Message: "taxonomy_version is required; fail closed"}
	}
	taxonomy, ok := e.Taxonomy(taxonomyVersion)
	if !ok {
		return nil, &RequestError{Code: "UNKNOWN_TAXONOMY_VERSION", Message: "no taxonomy " + taxonomyVersion + "; fail closed"}
	}

	// Resolve and hash every (entity type, policy version) pair this
	// evaluation will actually use, BEFORE spending subprocess time. A
	// user/product evaluation pins two different .mg files, so the profile
	// hash is the digest over both -- one value for the attestation root.
	policyVersions := req.resolvedPolicyVersions(req.Clusters)
	policyHashes := make(map[string]string, len(policyVersions))
	digestInput := ""
	for _, entityType := range sortedStringKeys(policyVersions) {
		version := policyVersions[entityType]
		hash, err := e.PolicyHash(entityType, version)
		if err != nil {
			return nil, err
		}
		policyHashes[entityType] = hash
		digestInput += entityType + "=" + hash + "\n"
	}
	combined := sha256.Sum256([]byte(digestInput))
	policyHash := hex.EncodeToString(combined[:])

	started := time.Now()
	workers := req.Concurrency
	if workers <= 0 {
		workers = defaultMetricWorkers()
	}
	if workers > len(req.Clusters) {
		workers = len(req.Clusters)
	}

	results := make([]ClusterMetrics, len(req.Clusters))
	errs := make([]ClusterError, len(req.Clusters))
	factTotal := 0
	for _, c := range req.Clusters {
		factTotal += len(c.CandidateFacts)
	}

	type job struct {
		idx     int
		cluster ClusterInput
	}
	jobs := make(chan job)
	var wg sync.WaitGroup
	var mu sync.Mutex
	var policyErr error

	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := range jobs {
				cm, cerr, perr := e.evaluateCluster(j.cluster, req, taxonomy)
				if perr != nil {
					mu.Lock()
					if policyErr == nil {
						policyErr = perr
					}
					mu.Unlock()
					continue
				}
				results[j.idx] = cm
				errs[j.idx] = cerr
			}
		}()
	}
	for i, c := range req.Clusters {
		jobs <- job{idx: i, cluster: c}
	}
	close(jobs)
	wg.Wait()

	if policyErr != nil {
		return nil, policyErr
	}

	resp := &MetricsResponse{
		PolicyVersion:      req.PolicyVersion,
		PolicyHash:         policyHash,
		PolicyHashes:       policyHashes,
		TaxonomyVersion:    taxonomyVersion,
		ClustersEvaluated:  len(req.Clusters),
		FactsEvaluated:     factTotal,
		VerdictCounts:      map[string]int{},
		ReasonCounts:       map[string]int{},
		ExecutionLatencyMs: float64(time.Since(started).Microseconds()) / 1000.0,
	}
	maxDepth := 0
	for _, cm := range results {
		if cm.EntityID == "" && cm.Verdict == "" {
			continue
		}
		resp.FactsFiltered += cm.FactsFiltered
		resp.LinksEvaluated += cm.LinksEvaluated
		resp.Clusters = append(resp.Clusters, cm)
		resp.tallyCluster(cm)
		// Row counts, not cluster counts: "Rule L1: 6102 Fired" means the
		// rule produced 6102 rows, which is what the dashboard label claims.
		for rule, n := range cm.RuleCounts {
			resp.ReasonCounts[rule] += n
		}
		// Proof tree depth = the longest chain of distinct rules that had to
		// fire before a verdict was reached. It is a bound on derivation depth,
		// not a wall-clock measure.
		if d := len(cm.RulesFired); d > maxDepth {
			maxDepth = d
		}
	}
	for _, ce := range errs {
		if ce.EntityID != "" {
			resp.Errors = append(resp.Errors, ce)
		}
	}
	resp.ProofTreeDepth = maxDepth
	sort.Slice(resp.Clusters, func(i, j int) bool {
		if resp.Clusters[i].EntityType != resp.Clusters[j].EntityType {
			return resp.Clusters[i].EntityType < resp.Clusters[j].EntityType
		}
		return resp.Clusters[i].EntityID < resp.Clusters[j].EntityID
	})
	return resp, nil
}

// tallyCluster puts one cluster in exactly one verdict bucket. Verdict
// precedence (escalated > published > denied) is decided in evaluateCluster;
// counting by Verdict, not by "has any published attribute", is what keeps the
// buckets disjoint.
func (r *MetricsResponse) tallyCluster(cm ClusterMetrics) {
	r.VerdictCounts[cm.Verdict]++
	switch cm.Verdict {
	case "escalated":
		r.QuarantinedClustersCount++
		r.StewardsTicketsCount += len(cm.StewardAttrs)
		if len(cm.PublishedAttrs) > 0 {
			r.PartialGoldensCount++
		}
	case "published":
		r.CertifiedGoldensCount++
	default:
		r.DeniedClustersCount++
	}
}

// evaluateCluster derives verdicts for one cached subgraph using only the
// `outcome` query.
func (e *Engine) evaluateCluster(cluster ClusterInput, req MetricsRequest, taxonomy Taxonomy) (ClusterMetrics, ClusterError, error) {
	var ce ClusterError
	cm := ClusterMetrics{
		EntityType: cluster.EntityType,
		EntityID:   cluster.EntityID,
	}
	if cluster.EntityType == "" || cluster.EntityID == "" {
		ce = ClusterError{EntityType: cluster.EntityType, EntityID: cluster.EntityID, Reason: "entity_type and entity_id are required"}
		return cm, ce, nil
	}
	if len(cluster.CandidateFacts) == 0 {
		ce = ClusterError{EntityType: cluster.EntityType, EntityID: cluster.EntityID, Reason: "cluster has no facts"}
		return cm, ce, nil
	}

	entityType := cluster.EntityType
	// Mirror the build pipeline's allowed_attributes filter (routes/build.py
	// passes policy.allowed_attributes into document_to_fragments). Without
	// it, a cluster carrying attributes from a different dataset (e.g. Olist
	// product_category inside a Kaggle user cluster) fails validation and the
	// whole cluster would be dropped from the metrics.
	policy := e.policyFor(entityType, req.PolicyVersionFor(entityType))
	allowed := map[string]bool{}
	for _, a := range policy.AllowedAttributes {
		allowed[a] = true
	}
	facts := cluster.CandidateFacts
	if len(allowed) > 0 {
		kept := make([]CandidateFact, 0, len(facts))
		for _, f := range facts {
			if allowed[f.Attribute] {
				kept = append(kept, f)
			} else {
				cm.FactsFiltered++
			}
		}
		facts = kept
	}
	if len(facts) == 0 {
		ce = ClusterError{EntityType: entityType, EntityID: cluster.EntityID, Reason: "no facts survive the policy's allowed_attributes filter"}
		return cm, ce, nil
	}
	// A link whose endpoint was filtered out is no longer a link between
	// candidate facts; canonicalize would reject it as INVALID_LINK and fail
	// the whole cluster. Drop it here, counting it, so the filter is visible.
	keptIDs := make(map[string]bool, len(facts))
	for _, f := range facts {
		keptIDs[f.FactID] = true
	}
	links := make([]linkInput, 0, len(cluster.Links))
	for _, link := range cluster.Links {
		if keptIDs[link.FromFactID] && keptIDs[link.ToFactID] {
			links = append(links, link)
		} else {
			cm.FactsFiltered++
		}
	}

	request := DeriveRequest{
		DecisionID:      "metrics-" + cluster.EntityID,
		EntityType:      entityType,
		EntityID:        cluster.EntityID,
		IdempotencyKey:  "metrics-" + cluster.EntityID,
		PayloadHash:     "metrics",
		PolicyVersion:   req.PolicyVersion,
		TaxonomyVersion: req.TaxonomyVersion,
		CandidateFacts:  facts,
		Links:           links,
	}
	// Per-cluster policy pinning: a mixed user/product evaluation must use the
	// policy that belongs to each entity type, not the first cluster's.
	if pinned := req.PolicyVersions[entityType]; pinned != "" {
		request.PolicyVersion = pinned
	}
	if err := e.ValidateDerive(request); err != nil {
		ce = ClusterError{EntityType: entityType, EntityID: cluster.EntityID, Reason: err.Error()}
		return cm, ce, nil
	}
	if err := checkRankCoherence(e.policyFor(entityType, request.PolicyVersion), request.CandidateFacts); err != nil {
		ce = ClusterError{EntityType: entityType, EntityID: cluster.EntityID, Reason: err.Error()}
		return cm, ce, nil
	}

	input, _, err := canonicalize(request, taxonomy)
	if err != nil {
		ce = ClusterError{EntityType: entityType, EntityID: cluster.EntityID, Reason: err.Error()}
		return cm, ce, nil
	}

	outcomes, vlinks, err := e.runMetricQuery(request, input)
	if err != nil {
		return cm, ce, err // policy-level failure: abort the whole evaluation
	}

	published := map[string]bool{}
	escalated := map[string]bool{}
	rules := map[string]bool{}
	ruleCounts := map[string]int{}
	for _, row := range outcomes {
		switch row.V {
		case "published":
			published[row.A] = true
		case "escalated":
			escalated[row.A] = true
			cm.StewardAttrs = append(cm.StewardAttrs, row.A)
		}
		if row.R != "" {
			rules[row.R] = true
			ruleCounts[row.R]++
		}
	}
	cm.PublishedAttrs = sortedKeys(published)
	cm.EscalatedAttrs = sortedKeys(escalated)
	// Every verified hop the engine accepted is an L1 firing. Counting them
	// here is what makes "Rule L1" show a number instead of "Idle" when a
	// profile's links are present.
	if len(vlinks) > 0 {
		rules["L1"] = true
		ruleCounts["L1"] += len(vlinks)
	}
	cm.RulesFired = sortedKeys(rules)
	cm.RuleCounts = ruleCounts
	cm.AttributeCount = len(outcomes)
	cm.LinksEvaluated = len(vlinks)

	switch {
	case len(cm.StewardAttrs) > 0:
		cm.Verdict = "escalated"
	case len(cm.PublishedAttrs) > 0:
		cm.Verdict = "published"
	default:
		cm.Verdict = "denied"
	}
	return cm, ce, nil
}

// metricRow is one row of `metric(Tag, A, V, X, R)`.

// Tag "out" carries an outcome row (A = attribute, V = verdict, X = reason,
// R = rule). Tag "lnk" carries a verified link (A = from, V = to, X = proof,
// R = "L1"). Both result sets come back from ONE subprocess, which matters
// because process spawn is ~20ms and dominates cluster evaluation.
type metricRow struct {
	Tag string `json:"Tag"`
	A   string `json:"A"`
	V   string `json:"V"`
	X   string `json:"X"`
	R   string `json:"R"`
}

// runMetricQuery stages the canonicalized input and runs the single
// `metric` query. One subprocess per cluster, returning verdicts + links.
func (e *Engine) runMetricQuery(request DeriveRequest, input deriveInput) ([]outcomeRow, []verifiedLinkRow, error) {
	mkitBin := mkitBinary()
	policyFile, err := e.resolvePolicyFile(request.EntityType, request.PolicyVersion)
	if err != nil {
		return nil, nil, err
	}
	payload, err := json.Marshal(input)
	if err != nil {
		return nil, nil, &RequestError{Code: "INTERNAL_ERROR", Message: "cannot encode derive input"}
	}
	dir, err := os.MkdirTemp("", "mosaic-metrics-")
	if err != nil {
		return nil, nil, &RequestError{Code: "INTERNAL_ERROR", Message: "cannot stage derive input"}
	}
	defer os.RemoveAll(dir)
	dataFile := dir + string(os.PathSeparator) + "input.json"
	if err := os.WriteFile(dataFile, payload, 0o600); err != nil {
		return nil, nil, &RequestError{Code: "INTERNAL_ERROR", Message: "cannot stage derive input"}
	}
	raw, err := runEval(mkitBin, policyFile, dataFile, "metric(Tag, A, V, X, R)")
	if err != nil {
		return nil, nil, err
	}
	var rows []metricRow
	if err := json.Unmarshal(raw, &rows); err != nil {
		return nil, nil, &RequestError{Code: "INTERNAL_ERROR", Message: "cannot parse manglekit metric: " + err.Error()}
	}
	var outcomes []outcomeRow
	var links []verifiedLinkRow
	for _, row := range rows {
		switch row.Tag {
		case "out":
			outcomes = append(outcomes, outcomeRow{A: row.A, V: row.V, X: row.X, R: row.R})
		case "lnk":
			// verifiedLinkRow carries only the endpoints and proof; the rule
			// ("L1") is folded into the cluster's rules_fired by the caller.
			links = append(links, verifiedLinkRow{F: row.A, T: row.V, P: row.X})
		}
	}
	return outcomes, links, nil
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// defaultMetricWorkers bounds subprocess fan-out. Each worker spawns one mkit
// process; beyond a handful the machine spends its time in process churn
// rather than evaluation (measured: 8-way concurrency raised per-call latency
// from ~35ms to 2-6s).
func defaultMetricWorkers() int {
	n := 4
	if v := os.Getenv("METRIC_WORKERS"); v != "" {
		var parsed int
		if _, err := fmt.Sscanf(v, "%d", &parsed); err == nil && parsed > 0 {
			n = parsed
		}
	}
	return n
}

// policyFor resolves the loaded Policy for an entity type + version, so rank
// coherence can be checked without re-reading files per cluster.
func (e *Engine) policyFor(entityType, policyVersion string) Policy {
	e.mu.RLock()
	defer e.mu.RUnlock()
	if byVersion, ok := e.policies[entityType]; ok {
		if p, ok := byVersion[policyVersion]; ok {
			return p
		}
	}
	return Policy{}
}

// PolicyVersionFor returns the pinned policy version for one entity type,
// falling back to the batch-wide version.
func (r MetricsRequest) PolicyVersionFor(entityType string) string {
	if v := r.PolicyVersions[entityType]; v != "" {
		return v
	}
	return r.PolicyVersion
}

// resolvedPolicyVersions maps each entity type in the batch to the policy
// version that will be used for it: an explicit per-type pin wins, otherwise
// the batch-wide version applies.
func (r MetricsRequest) resolvedPolicyVersions(clusters []ClusterInput) map[string]string {
	out := map[string]string{}
	for _, c := range clusters {
		et := c.EntityType
		if et == "" {
			continue
		}
		if v, ok := out[et]; ok && v != "" {
			continue
		}
		if pinned := r.PolicyVersions[et]; pinned != "" {
			out[et] = pinned
		} else {
			out[et] = r.PolicyVersion
		}
	}
	return out
}

func sortedStringKeys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
