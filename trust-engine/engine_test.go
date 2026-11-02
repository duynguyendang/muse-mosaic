package main

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func validPolicy() Policy {
	return Policy{
		PolicyVersion:      "pol-test-v1",
		EntityType:         "user",
		AllowedAttributes:  []string{"phone", "email"},
		GoldenAttributes:   []string{"phone"},
		CriticalAttributes: []string{"phone"},
		AuthorityMatrix:    map[string]map[string]int{"phone": {"sap": 1, "web": 2}},
		Rules:              []string{"R1"},
		TTLsSeconds:        map[string]int{"phone": 3600},
	}
}

func TestFloorMinutes(t *testing.T) {
	cases := []struct{ in, want int64 }{
		{0, 0}, {59, 0}, {60, 1}, {119, 1}, {-1, -1}, {-60, -1}, {-61, -2},
	}
	for _, c := range cases {
		if got := floorMinutes(c.in); got != c.want {
			t.Errorf("floorMinutes(%d) = %d, want %d", c.in, got, c.want)
		}
	}
}

func TestFallbackForKnownAndDefault(t *testing.T) {
	for _, attr := range []string{"phone", "email", "address", "category"} {
		if got := fallbackFor(attr); got == fallbackFor("unknown_attr") {
			t.Errorf("fallbackFor(%q) fell through to the default", attr)
		}
	}
	if fallbackFor("zzz") == "" {
		t.Error("default fallback must never be empty (steward needs a stop instruction)")
	}
}

func TestValidatePolicyAcceptsValid(t *testing.T) {
	if err := validatePolicy("user", validPolicy()); err != nil {
		t.Fatalf("valid policy rejected: %v", err)
	}
}

func TestValidatePolicyRejects(t *testing.T) {
	cases := map[string]func(*Policy){
		"missing version":      func(p *Policy) { p.PolicyVersion = " " },
		"entity mismatch":      func(p *Policy) { p.EntityType = "product" },
		"no allowed":           func(p *Policy) { p.AllowedAttributes = nil },
		"bad attribute name":   func(p *Policy) { p.AllowedAttributes = []string{"Phone"} },
		"duplicate attribute":  func(p *Policy) { p.AllowedAttributes = []string{"phone", "phone"} },
		"matrix not allowed":   func(p *Policy) { p.AuthorityMatrix["fax"] = map[string]int{"sap": 1} },
		"rank below one":       func(p *Policy) { p.AuthorityMatrix["phone"]["sap"] = 0 },
		"empty source":         func(p *Policy) { p.AuthorityMatrix["phone"][" "] = 1 },
		"critical not allowed": func(p *Policy) { p.CriticalAttributes = []string{"fax"} },
		"golden not allowed":   func(p *Policy) { p.GoldenAttributes = []string{"fax"} },
		"no rules":             func(p *Policy) { p.Rules = nil },
		"non-positive ttl":     func(p *Policy) { p.TTLsSeconds["phone"] = 0 },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			p := validPolicy()
			mutate(&p)
			if err := validatePolicy("user", p); err == nil {
				t.Fatal("expected validation error, got nil")
			}
		})
	}
}

func TestCheckRankCoherence(t *testing.T) {
	p := validPolicy()
	ok := []CandidateFact{
		{Attribute: "phone", Source: "sap", AuthorityRank: 1},
		{Attribute: "phone", Source: "unlisted-source", AuthorityRank: 9}, // unknown source: not checked here
		{Attribute: "email", Source: "sap", AuthorityRank: 7},             // attr not in matrix
	}
	if err := checkRankCoherence(p, ok); err != nil {
		t.Fatalf("coherent facts rejected: %v", err)
	}
	bad := []CandidateFact{{Attribute: "phone", Source: "sap", AuthorityRank: 2}}
	err := checkRankCoherence(p, bad)
	var reqErr *RequestError
	if !errors.As(err, &reqErr) || reqErr.Code != "INVALID_RANK_MISMATCH" {
		t.Fatalf("want INVALID_RANK_MISMATCH, got %v", err)
	}
}

func TestSortedKeysAreDeterministic(t *testing.T) {
	got := sortedKeys(map[string]bool{"b": true, "a": true, "c": false})
	if !reflect.DeepEqual(got, []string{"a", "b", "c"}) {
		t.Fatalf("got %v", got)
	}
	gotS := sortedStringKeys(map[string]string{"z": "1", "m": "2"})
	if !reflect.DeepEqual(gotS, []string{"m", "z"}) {
		t.Fatalf("got %v", gotS)
	}
}

func TestDefaultMetricWorkers(t *testing.T) {
	t.Setenv("METRIC_WORKERS", "")
	if got := defaultMetricWorkers(); got != 4 {
		t.Errorf("default = %d, want 4", got)
	}
	t.Setenv("METRIC_WORKERS", "7")
	if got := defaultMetricWorkers(); got != 7 {
		t.Errorf("override = %d, want 7", got)
	}
	for _, bad := range []string{"0", "-3", "abc"} {
		t.Setenv("METRIC_WORKERS", bad)
		if got := defaultMetricWorkers(); got != 4 {
			t.Errorf("METRIC_WORKERS=%q gave %d, want fallback 4", bad, got)
		}
	}
}

func TestPolicyVersionForPinWinsOverBatch(t *testing.T) {
	r := MetricsRequest{PolicyVersion: "batch-v1", PolicyVersions: map[string]string{"user": "pin-v2"}}
	if got := r.PolicyVersionFor("user"); got != "pin-v2" {
		t.Errorf("pinned: got %q", got)
	}
	if got := r.PolicyVersionFor("product"); got != "batch-v1" {
		t.Errorf("fallback: got %q", got)
	}
}

func TestResolvedPolicyVersions(t *testing.T) {
	r := MetricsRequest{PolicyVersion: "batch-v1", PolicyVersions: map[string]string{"user": "pin-v2"}}
	got := r.resolvedPolicyVersions([]ClusterInput{
		{EntityType: "user"}, {EntityType: "product"}, {EntityType: ""},
	})
	want := map[string]string{"user": "pin-v2", "product": "batch-v1"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
}

// repoPolicyDir points at the committed policies; loading them proves every
// shipped policy JSON still satisfies validatePolicy (CI catches a bad edit).
func repoPolicyDir(t *testing.T) string {
	t.Helper()
	dir, err := filepath.Abs("policies")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Skipf("no policies dir: %v", err)
	}
	return dir
}

func TestLoadShippedPolicies(t *testing.T) {
	policies, active, err := loadPolicies(repoPolicyDir(t))
	if err != nil {
		t.Fatalf("shipped policies failed to load: %v", err)
	}
	for _, et := range []string{entityTypeUser, entityTypeProduct} {
		if len(policies[et]) == 0 || active[et] == "" {
			t.Errorf("no policy / active version for %s", et)
		}
	}
}

func TestLoadPoliciesRejectsInvalidActivePolicy(t *testing.T) {
	root := t.TempDir()
	for _, et := range []string{entityTypeUser, entityTypeProduct} {
		if err := os.MkdirAll(filepath.Join(root, et), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	bad := `{"policy_version":"v1","entity_type":"user","allowed_attributes":["phone"],` +
		`"authority_matrix":{"phone":{"sap":0}},"rules":["R1"]}`
	if err := os.WriteFile(filepath.Join(root, "user", "bad.json"), []byte(bad), 0o644); err != nil {
		t.Fatal(err)
	}
	_, _, err := loadPolicies(root)
	if err == nil || !strings.Contains(err.Error(), "must be >= 1") {
		t.Fatalf("want rank validation failure, got %v", err)
	}
}

func TestLoadPoliciesRejectsUnknownFields(t *testing.T) {
	root := t.TempDir()
	for _, et := range []string{entityTypeUser, entityTypeProduct} {
		if err := os.MkdirAll(filepath.Join(root, et), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	bad := `{"policy_version":"v1","entity_type":"user","surprise":1}`
	if err := os.WriteFile(filepath.Join(root, "user", "x.json"), []byte(bad), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, _, err := loadPolicies(root); err == nil {
		t.Fatal("unknown policy field must be rejected (DisallowUnknownFields)")
	}
}

func TestTallyClusterBucketsAreDisjointAndSumToTotal(t *testing.T) {
	resp := &MetricsResponse{VerdictCounts: map[string]int{}}
	clusters := []ClusterMetrics{
		{Verdict: "published", PublishedAttrs: []string{"email", "phone"}},
		// Partial: email published, phone held. Must count ONCE, as held.
		{Verdict: "escalated", PublishedAttrs: []string{"email"}, StewardAttrs: []string{"phone"}},
		{Verdict: "escalated", StewardAttrs: []string{"phone", "address"}},
		{Verdict: "denied"},
	}
	for _, cm := range clusters {
		resp.tallyCluster(cm)
	}
	if resp.CertifiedGoldensCount != 1 || resp.QuarantinedClustersCount != 2 || resp.DeniedClustersCount != 1 {
		t.Fatalf("got certified=%d quarantined=%d denied=%d",
			resp.CertifiedGoldensCount, resp.QuarantinedClustersCount, resp.DeniedClustersCount)
	}
	if sum := resp.CertifiedGoldensCount + resp.QuarantinedClustersCount + resp.DeniedClustersCount; sum != len(clusters) {
		t.Fatalf("buckets sum to %d, want %d (a cluster was double counted)", sum, len(clusters))
	}
	if resp.PartialGoldensCount != 1 {
		t.Errorf("partial = %d, want 1", resp.PartialGoldensCount)
	}
	if resp.StewardsTicketsCount != 3 {
		t.Errorf("tickets = %d, want 3 (attribute-level)", resp.StewardsTicketsCount)
	}
}

func TestPhoneKey(t *testing.T) {
	cases := map[string]string{
		"4499771729":       "499771729",
		"+1 449-977-1729":  "499771729", // country code variant keys the same
		"(449) 977-1729":   "499771729",
		"449.977.1729x282": "499771729", // extension dropped
		"+84 90 123 4567":  "901234567",
		"0901234567":       "901234567", // VN national vs international
		"12345":            "",          // too short to be evidence
		"":                 "",
	}
	for in, want := range cases {
		if got := phoneKey(in); got != want {
			t.Errorf("phoneKey(%q) = %q, want %q", in, got, want)
		}
	}
}

func share(a, b []string) bool {
	set := map[string]bool{}
	for _, k := range a {
		set[k] = true
	}
	for _, k := range b {
		if set[k] {
			return true
		}
	}
	return false
}

func TestEmailDeletionKeysDetectEditDistanceOne(t *testing.T) {
	base := emailDeletionKeys("maria.day@example.com")
	near := map[string]string{
		"substitution": "maria.dqy@example.com",
		"insertion":    "maria.daay@example.com",
		"deletion":     "maria.dy@example.com",
		"case":         "MARIA.DAY@EXAMPLE.COM",
	}
	for name, e := range near {
		if !share(base, emailDeletionKeys(e)) {
			t.Errorf("%s: %q should be within edit distance 1 of the base", name, e)
		}
	}
	far := map[string]string{
		"two edits":        "maria.dqq@example.com",
		"different domain": "maria.day@example.org",
		"different person": "linh.tranx@example.com",
	}
	for name, e := range far {
		if share(base, emailDeletionKeys(e)) {
			t.Errorf("%s: %q must NOT match the base", name, e)
		}
	}
}

func TestEmailKeysRefuseShortOrMalformedLocals(t *testing.T) {
	for _, e := range []string{"a@b.com", "short@x.com", "noatsign", "@x.com", "name@", ""} {
		if keys := emailDeletionKeys(e); keys != nil {
			t.Errorf("emailDeletionKeys(%q) = %v, want nil (too weak to be evidence)", e, keys)
		}
	}
}

func TestSemanticProofsAreInTheClosedVocabulary(t *testing.T) {
	for _, p := range []string{"semantic_phone", "semantic_email", "exact_phone", "fuzzy_name_address"} {
		if !linkProofs[p] {
			t.Errorf("%q missing from linkProofs", p)
		}
	}
	if linkProofs["semantic_name"] {
		t.Error("an unknown proof must stay outside the closed vocabulary")
	}
}
