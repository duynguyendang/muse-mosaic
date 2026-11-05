// Package main contains the Muse Mosaic trust-engine scaffold.
//
// The trust-engine is the only component allowed to derive a golden value.
// It loads and validates the versioned policies, validates the derive
// contract, canonicalizes fact times, and evaluates the runnable .mg policy
// for the requested entity type with the pinned manglekit binary.
// Any failure fails closed: no golden facts.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	// AdapterState is surfaced on /health so callers cannot mistake this
	// scaffold for a working deriver.
	AdapterState = "manglekit-subprocess"

	entityTypeUser    = "user"
	entityTypeProduct = "product"

	// ManglekitVersion is the pinned manglekit release the derive path shells
	// out to. /health reports it so callers can verify the exact binary the
	// policy adapter was verified against.
	ManglekitVersion = "v0.0.0-20260330150740-81e7572ffb31"

	// deriveTimeout bounds one derive request (two mkit eval calls).
	deriveTimeout = 15 * time.Second

	// mangleNumberBound documents a hard limit of the mangle number lexer:
	// it cannot parse scientific notation, and the JSON flattener prints
	// numbers with %g, so any numeric input with abs(value) >= 1e6 arrives
	// unparseable. Requests outside the bound are rejected fail-closed.
	mangleNumberBound = 1000000
)

var attributePattern = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)

// Policy is the externalized, versioned policy file shape (CSD 12.7, v2.3 rule tiers).
type Policy struct {
	PolicyVersion      string                    `json:"policy_version"`
	EntityType         string                    `json:"entity_type"`
	AllowedAttributes  []string                  `json:"allowed_attributes"`
	GoldenAttributes   []string                  `json:"golden_attributes"`
	AuthorityMatrix    map[string]map[string]int `json:"authority_matrix"`
	CriticalAttributes []string                  `json:"critical_attributes"`
	Rules              []string                  `json:"rules"`
	Thresholds         map[string]any            `json:"thresholds"`
	TTLsSeconds        map[string]int            `json:"ttls_seconds"`
	Splink             SplinkSettings            `json:"splink"`
}

// SplinkSettings carries per-type candidate settings (blocking + threshold,
// demo parameters). Splink proposes only; the threshold never decides.
type SplinkSettings struct {
	Threshold float64  `json:"threshold"`
	Blocking  []string `json:"blocking"`
}

// TaxonomyTerm is one canonical term with its exact-match synonyms.
type TaxonomyTerm struct {
	Term     string   `json:"term"`
	Synonyms []string `json:"synonyms"`
}

// Taxonomy is the versioned canonical vocabulary (CSD 3.5). Baked into the
// image; a steward Accept creates a new version file (see mosaic-api).
// Synthetic/Notice are demo labels, tolerated but never derivation inputs.
type Taxonomy struct {
	Synthetic bool   `json:"synthetic"`
	Notice    string `json:"notice"`
	// ParentVersion and CreatedAt order derived versions (mosaic-api picks the
	// newest as the default); the engine never uses them in derivation.
	ParentVersion     string              `json:"parent_version,omitempty"`
	CreatedAt         string              `json:"created_at,omitempty"`
	TaxonomyVersion   string              `json:"taxonomy_version"`
	EntityType        string              `json:"entity_type"`
	AttributeAliases  map[string][]string `json:"attribute_aliases"`
	Terms             []TaxonomyTerm      `json:"terms"`
	loadedFingerprint string
}

// CandidateFact is the derivation input. It has no embedding field: an
// embedding sent to this endpoint is rejected by DisallowUnknownFields rather
// than silently becoming a rule input.
type CandidateFact struct {
	FactID        string `json:"fact_id"`
	EntityType    string `json:"entity_type"`
	EntityID      string `json:"entity_id"`
	Attribute     string `json:"attribute"`
	Value         string `json:"value"`
	Source        string `json:"source"`
	SourceID      string `json:"source_id"`
	AuthorityRank int    `json:"authority_rank"`
	EventTime     string `json:"event_time"`
	Verified      *bool  `json:"verified"`
	TTLSeconds    int    `json:"ttl_seconds"`
}

// DeriveRequest matches POST /derive in CSD 12.4 (v2.3: + taxonomy_version).
type DeriveRequest struct {
	DecisionID      string          `json:"decision_id"`
	EntityType      string          `json:"entity_type"`
	EntityID        string          `json:"entity_id"`
	IdempotencyKey  string          `json:"idempotency_key"`
	PayloadHash     string          `json:"payload_hash"`
	PolicyVersion   string          `json:"policy_version"`
	TaxonomyVersion string          `json:"taxonomy_version"`
	CandidateFacts  []CandidateFact `json:"candidate_facts"`
	Links           []linkInput     `json:"links"`
}

// RequestError is a contract/policy validation failure. It always maps to a
// fail-closed response: no golden facts are returned.
type RequestError struct {
	Code    string
	Message string
}

func (e *RequestError) Error() string {
	return e.Code + ": " + e.Message
}

// Engine holds the loaded policies and taxonomies. Policies reload on
// SIGHUP (see ReloadPolicies); taxonomies hot-reload on access when the
// directory changes (new steward-accepted versions). mu guards both swaps
// so a reload never tears a concurrent derive.
type Engine struct {
	mu             sync.RWMutex
	policyDir      string
	taxonomyDir    string
	policies       map[string]map[string]Policy
	activeVersion  map[string]string
	taxonomies     map[string]Taxonomy
	taxFingerprint string
}

// loadPolicies reads and validates every policy JSON for the v1 entity
// types from policyDir. It builds fresh maps and never mutates the Engine,
// so a failed reload can keep serving the previous policies.
func loadPolicies(policyDir string) (map[string]map[string]Policy, map[string]string, error) {
	policies := make(map[string]map[string]Policy)
	activeVersion := make(map[string]string)

	entityTypes, err := entityDirs(policyDir)
	if err != nil {
		return nil, nil, err
	}
	if len(entityTypes) == 0 {
		return nil, nil, fmt.Errorf("no policy JSON for any entity type under %s", policyDir)
	}
	for _, entityType := range entityTypes {
		files, err := filepath.Glob(filepath.Join(policyDir, entityType, "*.json"))
		if err != nil {
			return nil, nil, fmt.Errorf("glob policies for %s: %w", entityType, err)
		}
		if len(files) == 0 {
			continue // entityDirs already filtered this; stay fail-closed
		}
		sort.Strings(files)

		policies[entityType] = make(map[string]Policy)
		versions := make([]string, 0, len(files))
		for _, path := range files {
			raw, err := os.ReadFile(path)
			if err != nil {
				return nil, nil, fmt.Errorf("read policy %s: %w", path, err)
			}
			decoder := json.NewDecoder(bytes.NewReader(raw))
			decoder.DisallowUnknownFields()
			var policy Policy
			if err := decoder.Decode(&policy); err != nil {
				return nil, nil, fmt.Errorf("parse policy %s: %w", path, err)
			}
			if err := validatePolicy(entityType, policy); err != nil {
				return nil, nil, fmt.Errorf("invalid policy %s: %w", path, err)
			}
			if _, exists := policies[entityType][policy.PolicyVersion]; exists {
				return nil, nil, fmt.Errorf("duplicate policy version %q for %s", policy.PolicyVersion, entityType)
			}
			policies[entityType][policy.PolicyVersion] = policy
			versions = append(versions, policy.PolicyVersion)
		}
		sort.Strings(versions)
		// v1 keeps one policy version per entity type. If several are present,
		// /health reports the lexicographically latest; /derive still requires
		// the exact version in the request.
		activeVersion[entityType] = versions[len(versions)-1]
	}

	return policies, activeVersion, nil
}

var entitySlug = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)

// entityDirs lists the policy root's immediate subdirectories that look like
// entity types (lower-case slug, at least one .json under it). No entity is
// privileged: user, product and publication are peers.
func entityDirs(policyDir string) ([]string, error) {
	entries, err := os.ReadDir(policyDir)
	if err != nil {
		return nil, fmt.Errorf("read policy dir %s: %w", policyDir, err)
	}
	var out []string
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		name := entry.Name()
		if !entitySlug.MatchString(name) {
			continue
		}
		files, err := filepath.Glob(filepath.Join(policyDir, name, "*.json"))
		if err != nil || len(files) == 0 {
			continue
		}
		out = append(out, name)
	}
	sort.Strings(out)
	return out, nil
}

// NewEngine loads every policy JSON for the v1 entity types plus every
// taxonomy JSON, and fails fast on a malformed file.
func NewEngine(policyDir, taxonomyDir string) (*Engine, error) {
	policies, activeVersion, err := loadPolicies(policyDir)
	if err != nil {
		return nil, err
	}
	engine := &Engine{
		policyDir:     policyDir,
		taxonomyDir:   taxonomyDir,
		policies:      policies,
		activeVersion: activeVersion,
		taxonomies:    make(map[string]Taxonomy),
	}

	if err := engine.reloadTaxonomies(); err != nil {
		return nil, err
	}

	return engine, nil
}

// ReloadPolicies re-reads the policy directory (invoked on SIGHUP). The new
// maps are fully built and validated before the swap; on any error the
// previous policies stay in force and the error is returned for logging.
func (e *Engine) ReloadPolicies() error {
	policies, activeVersion, err := loadPolicies(e.policyDir)
	if err != nil {
		return err
	}
	e.mu.Lock()
	e.policies = policies
	e.activeVersion = activeVersion
	e.mu.Unlock()
	return nil
}

// taxonomyFingerprint snapshots the taxonomy dir (names + mtimes) so derive
// can hot-reload steward-accepted versions without a restart.
func taxonomyFingerprint(taxonomyDir string) (string, error) {
	files, err := filepath.Glob(filepath.Join(taxonomyDir, "*.json"))
	if err != nil {
		return "", err
	}
	sort.Strings(files)
	var parts []string
	for _, path := range files {
		info, err := os.Stat(path)
		if err != nil {
			return "", err
		}
		parts = append(parts, fmt.Sprintf("%s:%d", path, info.ModTime().UnixNano()))
	}
	return strings.Join(parts, "|"), nil
}

// reloadTaxonomies reloads taxonomy files when the directory changed.
func (e *Engine) reloadTaxonomies() error {
	fingerprint, err := taxonomyFingerprint(e.taxonomyDir)
	if err != nil {
		return fmt.Errorf("scan taxonomy dir %s: %w", e.taxonomyDir, err)
	}
	if fingerprint == e.taxFingerprint && len(e.taxonomies) > 0 {
		return nil
	}
	files, err := filepath.Glob(filepath.Join(e.taxonomyDir, "*.json"))
	if err != nil {
		return fmt.Errorf("glob taxonomies under %s: %w", e.taxonomyDir, err)
	}
	if len(files) == 0 {
		return fmt.Errorf("no taxonomy JSON found under %s", e.taxonomyDir)
	}
	loaded := make(map[string]Taxonomy)
	for _, path := range files {
		raw, err := os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("read taxonomy %s: %w", path, err)
		}
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		var taxonomy Taxonomy
		if err := decoder.Decode(&taxonomy); err != nil {
			return fmt.Errorf("parse taxonomy %s: %w", path, err)
		}
		if err := validateTaxonomy(taxonomy); err != nil {
			return fmt.Errorf("invalid taxonomy %s: %w", path, err)
		}
		if _, exists := loaded[taxonomy.TaxonomyVersion]; exists {
			return fmt.Errorf("duplicate taxonomy version %q", taxonomy.TaxonomyVersion)
		}
		loaded[taxonomy.TaxonomyVersion] = taxonomy
	}
	e.taxonomies = loaded
	e.taxFingerprint = fingerprint
	return nil
}

func validateTaxonomy(taxonomy Taxonomy) error {
	if strings.TrimSpace(taxonomy.TaxonomyVersion) == "" {
		return fmt.Errorf("taxonomy_version is required")
	}
	if !attributePattern.MatchString(taxonomy.EntityType) {
		return fmt.Errorf("invalid taxonomy entity_type %q", taxonomy.EntityType)
	}
	for canonical, aliases := range taxonomy.AttributeAliases {
		if !attributePattern.MatchString(canonical) {
			return fmt.Errorf("invalid taxonomy alias target %q", canonical)
		}
		for _, alias := range aliases {
			if strings.TrimSpace(alias) == "" {
				return fmt.Errorf("empty taxonomy alias for %q", canonical)
			}
		}
	}
	seen := map[string]struct{}{}
	for _, term := range taxonomy.Terms {
		if strings.TrimSpace(term.Term) == "" {
			return fmt.Errorf("taxonomy term must not be empty")
		}
		lowered := strings.ToLower(term.Term)
		if _, dup := seen[lowered]; dup {
			return fmt.Errorf("duplicate taxonomy term %q", term.Term)
		}
		seen[lowered] = struct{}{}
	}
	return nil
}

// Taxonomy returns the loaded taxonomy for a version, hot-reloading first.
func (e *Engine) Taxonomy(version string) (Taxonomy, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := e.reloadTaxonomies(); err != nil {
		return Taxonomy{}, false
	}
	taxonomy, ok := e.taxonomies[version]
	return taxonomy, ok
}

func validatePolicy(directoryEntityType string, policy Policy) error {
	if strings.TrimSpace(policy.PolicyVersion) == "" {
		return fmt.Errorf("policy_version is required")
	}
	if policy.EntityType != directoryEntityType {
		return fmt.Errorf("entity_type %q does not match policy directory %q", policy.EntityType, directoryEntityType)
	}
	if len(policy.AllowedAttributes) == 0 {
		return fmt.Errorf("allowed_attributes must not be empty")
	}
	allowed := make(map[string]struct{}, len(policy.AllowedAttributes))
	for _, attribute := range policy.AllowedAttributes {
		if !attributePattern.MatchString(attribute) {
			return fmt.Errorf("invalid allowed attribute %q", attribute)
		}
		if _, duplicate := allowed[attribute]; duplicate {
			return fmt.Errorf("duplicate allowed attribute %q", attribute)
		}
		allowed[attribute] = struct{}{}
	}
	for attribute, sources := range policy.AuthorityMatrix {
		if _, ok := allowed[attribute]; !ok {
			return fmt.Errorf("authority_matrix attribute %q is not allowed", attribute)
		}
		for source, rank := range sources {
			if strings.TrimSpace(source) == "" {
				return fmt.Errorf("authority_matrix source for %q is empty", attribute)
			}
			if rank < 1 {
				return fmt.Errorf("authority rank for %q/%q must be >= 1", attribute, source)
			}
		}
	}
	for _, attribute := range policy.CriticalAttributes {
		if _, ok := allowed[attribute]; !ok {
			return fmt.Errorf("critical attribute %q is not allowed", attribute)
		}
	}
	for _, attribute := range policy.GoldenAttributes {
		if _, ok := allowed[attribute]; !ok {
			return fmt.Errorf("golden attribute %q is not allowed", attribute)
		}
	}
	if len(policy.Rules) == 0 {
		return fmt.Errorf("rules must not be empty")
	}
	for window, seconds := range policy.TTLsSeconds {
		if seconds <= 0 {
			return fmt.Errorf("ttl_seconds for %q must be > 0", window)
		}
	}
	return nil
}

// PolicyVersions returns the active policy version per entity type.
func (e *Engine) PolicyVersions() map[string]string {
	e.mu.RLock()
	defer e.mu.RUnlock()
	out := make(map[string]string, len(e.activeVersion))
	for entityType, version := range e.activeVersion {
		out[entityType] = version
	}
	return out
}

// ValidateDerive enforces the shape needed before any derivation. Unknown
// entity types or attributes are rejected, never guessed. A fact that fails
// provenance validation never reaches a rule.
func (e *Engine) ValidateDerive(request DeriveRequest) error {
	required := []struct {
		name  string
		value string
	}{
		{"decision_id", request.DecisionID},
		{"entity_type", request.EntityType},
		{"entity_id", request.EntityID},
		{"idempotency_key", request.IdempotencyKey},
		{"payload_hash", request.PayloadHash},
		{"policy_version", request.PolicyVersion},
		{"taxonomy_version", request.TaxonomyVersion},
	}
	for _, field := range required {
		if strings.TrimSpace(field.value) == "" {
			return &RequestError{Code: "INVALID_REQUEST", Message: field.name + " is required"}
		}
	}

	e.mu.RLock()
	byVersion, knownEntity := e.policies[request.EntityType]
	e.mu.RUnlock()
	if !knownEntity {
		return &RequestError{
			Code:    "UNKNOWN_ENTITY_TYPE",
			Message: fmt.Sprintf("no policy for entity_type %q; fail closed", request.EntityType),
		}
	}
	policy, knownVersion := byVersion[request.PolicyVersion]
	if !knownVersion {
		return &RequestError{
			Code:    "UNKNOWN_POLICY_VERSION",
			Message: fmt.Sprintf("no policy %q for entity_type %q; fail closed", request.PolicyVersion, request.EntityType),
		}
	}
	if len(request.CandidateFacts) == 0 {
		return &RequestError{Code: "INVALID_REQUEST", Message: "candidate_facts must not be empty"}
	}

	allowed := make(map[string]struct{}, len(policy.AllowedAttributes))
	for _, attribute := range policy.AllowedAttributes {
		allowed[attribute] = struct{}{}
	}

	seenFactIDs := make(map[string]struct{}, len(request.CandidateFacts))
	for index, fact := range request.CandidateFacts {
		prefix := fmt.Sprintf("candidate_facts[%d]", index)
		if strings.TrimSpace(fact.FactID) == "" {
			return &RequestError{Code: "INVALID_REQUEST", Message: prefix + ".fact_id is required"}
		}
		if _, duplicate := seenFactIDs[fact.FactID]; duplicate {
			return &RequestError{
				Code:    "INVALID_REQUEST",
				Message: fmt.Sprintf("duplicate fact_id %q", fact.FactID),
			}
		}
		seenFactIDs[fact.FactID] = struct{}{}

		if fact.EntityType != request.EntityType {
			return &RequestError{Code: "INVALID_REQUEST", Message: prefix + ".entity_type does not match the envelope"}
		}
		if fact.EntityID != request.EntityID {
			return &RequestError{Code: "INVALID_REQUEST", Message: prefix + ".entity_id does not match the envelope"}
		}
		if !attributePattern.MatchString(fact.Attribute) {
			return &RequestError{Code: "INVALID_REQUEST", Message: prefix + ".attribute is invalid"}
		}
		if _, ok := allowed[fact.Attribute]; !ok {
			return &RequestError{
				Code:    "ATTRIBUTE_NOT_ALLOWED",
				Message: fmt.Sprintf("attribute %q is not allowed by policy %s; fail closed", fact.Attribute, policy.PolicyVersion),
			}
		}
		if strings.TrimSpace(fact.Value) == "" {
			return &RequestError{Code: "INVALID_REQUEST", Message: prefix + ".value is required"}
		}
		if strings.TrimSpace(fact.Source) == "" {
			return &RequestError{Code: "INVALID_REQUEST", Message: prefix + ".source is required"}
		}
		if strings.TrimSpace(fact.SourceID) == "" {
			return &RequestError{Code: "INVALID_REQUEST", Message: prefix + ".source_id is required"}
		}
		if fact.AuthorityRank < 1 {
			return &RequestError{Code: "INVALID_REQUEST", Message: prefix + ".authority_rank must be >= 1"}
		}
		if fact.Verified == nil {
			return &RequestError{Code: "INVALID_REQUEST", Message: prefix + ".verified is required"}
		}
		if fact.TTLSeconds <= 0 {
			return &RequestError{Code: "INVALID_REQUEST", Message: prefix + ".ttl_seconds must be > 0"}
		}
		if _, err := time.Parse(time.RFC3339Nano, fact.EventTime); err != nil {
			return &RequestError{
				Code:    "INVALID_REQUEST",
				Message: prefix + ".event_time must be RFC3339 with a timezone offset",
			}
		}
	}

	return nil
}
