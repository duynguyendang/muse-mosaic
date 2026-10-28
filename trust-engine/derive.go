package main

// Derive path: canonicalize the validated derive request, evaluate the
// runnable .mg policy for the entity type with the pinned manglekit binary,
// and map the outcome rows back to golden facts, conflicts and steward items.
//
// Input canonicalization is mechanical, not derivation: event_time (RFC3339)
// becomes whole minutes since timeBase, expires_min adds the TTL, and
// evaluation time is the max event_min across the request (hermetic and
// replayable: identical facts always derive identical verdicts).
//
// The rules themselves (R1 authority wins, R2 escalate ambiguity, R3 expiry
// is missing, R4 deny weak provenance) live in the versioned .mg policy and
// run inside manglekit. This file never picks a winner.

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// timeBase anchors minute canonicalization. Chosen so v1 demo dates land at
// small magnitudes (Oct 2026 is ~430000 minutes after base), inside the
// mangle number bound. Facts outside the bound are rejected, not wrapped.
var timeBase = time.Date(2026, time.January, 1, 0, 0, 0, 0, vietnamZone())

func vietnamZone() *time.Location {
	return time.FixedZone("+07:00", 7*3600)
}

// deriveFact is the canonical per-fact input sent to mkit eval.
type deriveFact struct {
	FactID        string `json:"fact_id"`
	Attribute     string `json:"attribute"`
	Value         string `json:"value"`
	Source        string `json:"source"`
	AuthorityRank int    `json:"authority_rank"`
	EventMin      int64  `json:"event_min"`
	ExpiresMin    int64  `json:"expires_min"`
	Verified      bool   `json:"verified"`
}

// deriveLink is one Linker-proposed link. The engine verifies sole proofs
// (L1) and ignores the rest for grounding; weak/conflict proofs never verify.
type deriveLink struct {
	From     string `json:"from_fact"`
	To       string `json:"to_fact"`
	Proof    string `json:"proof"`
	Proposer string `json:"proposer"`
}

// linkProofs is the closed vocabulary Linker may use (CSD 12.4).
var linkProofs = map[string]bool{
	"exact_email": true, "exact_phone": true, "exact_order": true,
	"exact_address": true, "fuzzy_name_address": true, "vector_only": true,
	"same_email_different_phone": true, "same_phone_different_email": true,
	"same_order_two_users": true, "same_fragment": true,
}

// deriveTaxonomy carries the canonical terms of the requested taxonomy
// version. The engine checks membership (T1); mapping synonyms to terms is
// the Taxonomist's claim, made mosaic-side before derivation.
type deriveTaxonomy struct {
	Version string   `json:"version"`
	Terms   []string `json:"terms"`
}

// deriveInput is the JSON document evaluated by the .mg policy.
type deriveInput struct {
	Facts      []deriveFact   `json:"facts"`
	Attributes []string       `json:"attributes"`
	Links      []deriveLink   `json:"links"`
	AnchorFact string         `json:"anchor_fact"`
	Taxonomy   deriveTaxonomy `json:"taxonomy"`
	NowMin     int64          `json:"now_min"`
}

// GoldenFact is a derived value with its proof attached.
type GoldenFact struct {
	EntityType      string   `json:"entity_type"`
	EntityID        string   `json:"entity_id"`
	Attribute       string   `json:"attribute"`
	Value           string   `json:"value"`
	WinnerFactID    string   `json:"winner_fact_id"`
	LoserFactIDs    []string `json:"loser_fact_ids"`
	RuleFired       string   `json:"rule_fired"`
	PolicyVersion   string   `json:"policy_version"`
	TaxonomyVersion string   `json:"taxonomy_version"`
	Lineage         string   `json:"lineage"`
}

// ConflictFact records a per-attribute escalate/deny with its reason.
type ConflictFact struct {
	EntityType      string   `json:"entity_type"`
	EntityID        string   `json:"entity_id"`
	Attribute       string   `json:"attribute"`
	ReasonCode      string   `json:"reason_code"`
	RuleFired       string   `json:"rule_fired"`
	FactIDs         []string `json:"fact_ids"`
	PolicyVersion   string   `json:"policy_version"`
	TaxonomyVersion string   `json:"taxonomy_version"`
}

// StewardItem is an escalated attribute waiting for a human decision.
type StewardItem struct {
	StewardID  string `json:"steward_id"`
	EntityType string `json:"entity_type"`
	EntityID   string `json:"entity_id"`
	Attribute  string `json:"attribute"`
	Owner      string `json:"owner"`
	Deputy     string `json:"deputy"`
	Deadline   string `json:"deadline"`
	Fallback   string `json:"fallback"`
	Status     string `json:"status"`
	DecisionID string `json:"decision_id"`
}

// VerifiedLink is an engine-verified link (sole proof checked, L1).
type VerifiedLink struct {
	FromFactID      string `json:"from_fact_id"`
	ToFactID        string `json:"to_fact_id"`
	Proof           string `json:"proof"`
	RuleFired       string `json:"rule_fired"`
	PolicyVersion   string `json:"policy_version"`
	TaxonomyVersion string `json:"taxonomy_version"`
}

// DeriveOutput is the successful /derive response body.
type DeriveOutput struct {
	DecisionID      string         `json:"decision_id"`
	PolicyVersion   string         `json:"policy_version"`
	TaxonomyVersion string         `json:"taxonomy_version"`
	Verdict         string         `json:"verdict"`
	RuleFired       string         `json:"rule_fired"`
	GoldenFacts     []GoldenFact   `json:"golden_facts"`
	Conflicts       []ConflictFact `json:"conflicts"`
	StewardQueue    []StewardItem  `json:"steward_queue"`
	VerifiedLinks   []VerifiedLink `json:"verified_links"`
}

// outcomeRow is one row of the outcome(A, V, X, R) query.
type outcomeRow struct {
	A string `json:"A"`
	V string `json:"V"`
	X string `json:"X"`
	R string `json:"R"`
}

// loserRow is one row of the loser(A, F) query.
type loserRow struct {
	A string `json:"A"`
	F string `json:"F"`
}

// verifiedLinkRow is one row of the vlink(F, T, P) query.
type verifiedLinkRow struct {
	F string `json:"F"`
	T string `json:"T"`
	P string `json:"P"`
}

// linkInput is a Linker-proposed link, validated then evaluated (L1).
// It arrives as a top-level "links" array on the derive request envelope.
type linkInput struct {
	FromFactID string `json:"from_fact_id"`
	ToFactID   string `json:"to_fact_id"`
	Proof      string `json:"proof"`
	Proposer   string `json:"proposer"`
}

// Derive validates, canonicalizes, evaluates and maps one derive request.
// On any error it returns a *RequestError (fail closed, no golden facts).
func (e *Engine) Derive(request DeriveRequest) (*DeriveOutput, error) {
	if err := e.ValidateDerive(request); err != nil {
		return nil, err
	}
	e.mu.RLock()
	policy, ok := e.policies[request.EntityType][request.PolicyVersion]
	e.mu.RUnlock()
	if !ok {
		return nil, &RequestError{Code: "UNKNOWN_POLICY_VERSION", Message: "policy vanished after validation"}
	}
	taxonomy, ok := e.Taxonomy(request.TaxonomyVersion)
	if !ok {
		return nil, &RequestError{
			Code:    "UNKNOWN_TAXONOMY_VERSION",
			Message: fmt.Sprintf("no taxonomy %q; fail closed", request.TaxonomyVersion),
		}
	}
	if err := checkRankCoherence(policy, request.CandidateFacts); err != nil {
		return nil, err
	}
	input, factsByID, err := canonicalize(request, taxonomy)
	if err != nil {
		return nil, err
	}
	outcomes, losers, verified, err := e.evaluate(request, input)
	if err != nil {
		return nil, err
	}
	return mapOutput(request, input, factsByID, outcomes, losers, verified), nil
}

// checkRankCoherence pins listed sources to their matrix rank. A source the
// matrix lists must arrive with exactly that rank, so a caller cannot claim
// rank 1 for a rank-3 source. A source the matrix does not list (for example
// a future steward-approval source) is accepted with its provided rank; the
// .mg policy still requires an authority matrix entry for the attribute
// itself, so unlisted sources never create authority out of nothing.
func checkRankCoherence(policy Policy, facts []CandidateFact) error {
	for i, fact := range facts {
		ranks, listed := policy.AuthorityMatrix[fact.Attribute]
		if !listed {
			continue
		}
		want, known := ranks[fact.Source]
		if !known {
			continue
		}
		if fact.AuthorityRank != want {
			return &RequestError{
				Code: "INVALID_RANK_MISMATCH",
				Message: fmt.Sprintf(
					"candidate_facts[%d].authority_rank %d contradicts policy matrix rank %d for %q/%q; fail closed",
					i, fact.AuthorityRank, want, fact.Attribute, fact.Source,
				),
			}
		}
	}
	return nil
}

// canonicalize converts event times to whole minutes and computes evaluation
// time as the max event_min. ttl_seconds must be whole minutes; sub-minute
// TTLs cannot be represented inside the mangle number bound, so they are
// rejected rather than silently coarsened. Links are validated (known
// endpoints, closed proof vocabulary, no self-links) and the anchor is the
// smallest fact id, so grounding is deterministic and replayable.
func canonicalize(request DeriveRequest, taxonomy Taxonomy) (deriveInput, map[string]CandidateFact, error) {
	factsByID := make(map[string]CandidateFact, len(request.CandidateFacts))
	seen := make(map[string]struct{}, len(request.CandidateFacts))
	input := deriveInput{}
	attrSet := map[string]struct{}{}

	var nowMin int64
	first := true
	for _, fact := range request.CandidateFacts {
		if _, dup := seen[fact.FactID]; dup {
			continue
		}
		seen[fact.FactID] = struct{}{}
		factsByID[fact.FactID] = fact

		eventTime, err := time.Parse(time.RFC3339Nano, fact.EventTime)
		if err != nil {
			return input, nil, &RequestError{Code: "INVALID_REQUEST", Message: "event_time must be RFC3339 with a timezone offset"}
		}
		if fact.TTLSeconds%60 != 0 {
			return input, nil, &RequestError{
				Code:    "INVALID_TTL_RESOLUTION",
				Message: fmt.Sprintf("fact %q ttl_seconds %d is not whole minutes; fail closed", fact.FactID, fact.TTLSeconds),
			}
		}
		eventMin := floorMinutes(eventTime.Unix() - timeBase.Unix())
		expiresMin := eventMin + int64(fact.TTLSeconds/60)
		for _, v := range []int64{eventMin, expiresMin} {
			if v <= -mangleNumberBound || v >= mangleNumberBound {
				return input, nil, &RequestError{
					Code:    "INVALID_NUMERIC_RANGE",
					Message: fmt.Sprintf("fact %q time %d is outside the mangle number bound; fail closed", fact.FactID, v),
				}
			}
		}
		if first || eventMin > nowMin {
			nowMin = eventMin
			first = false
		}
		input.Facts = append(input.Facts, deriveFact{
			FactID:        fact.FactID,
			Attribute:     fact.Attribute,
			Value:         fact.Value,
			Source:        fact.Source,
			AuthorityRank: fact.AuthorityRank,
			EventMin:      eventMin,
			ExpiresMin:    expiresMin,
			Verified:      fact.Verified != nil && *fact.Verified,
		})
		attrSet[fact.Attribute] = struct{}{}
	}
	for a := range attrSet {
		input.Attributes = append(input.Attributes, a)
	}
	sort.Strings(input.Attributes)
	sort.Slice(input.Facts, func(i, j int) bool { return input.Facts[i].FactID < input.Facts[j].FactID })
	input.NowMin = nowMin
	if len(input.Facts) > 0 {
		input.AnchorFact = input.Facts[0].FactID
	}
	for i, link := range request.Links {
		if strings.TrimSpace(link.FromFactID) == "" || strings.TrimSpace(link.ToFactID) == "" {
			return input, nil, &RequestError{Code: "INVALID_LINK", Message: fmt.Sprintf("links[%d] needs from_fact_id and to_fact_id; fail closed", i)}
		}
		if link.FromFactID == link.ToFactID {
			return input, nil, &RequestError{Code: "INVALID_LINK", Message: fmt.Sprintf("links[%d] is a self-link; fail closed", i)}
		}
		if _, ok := factsByID[link.FromFactID]; !ok {
			return input, nil, &RequestError{Code: "INVALID_LINK", Message: fmt.Sprintf("links[%d] from_fact_id %q is not a candidate fact; fail closed", i, link.FromFactID)}
		}
		if _, ok := factsByID[link.ToFactID]; !ok {
			return input, nil, &RequestError{Code: "INVALID_LINK", Message: fmt.Sprintf("links[%d] to_fact_id %q is not a candidate fact; fail closed", i, link.ToFactID)}
		}
		if !linkProofs[link.Proof] {
			return input, nil, &RequestError{Code: "INVALID_LINK_PROOF", Message: fmt.Sprintf("links[%d] proof %q is not in the closed vocabulary; fail closed", i, link.Proof)}
		}
		if strings.TrimSpace(link.Proposer) == "" {
			return input, nil, &RequestError{Code: "INVALID_LINK", Message: fmt.Sprintf("links[%d] needs a proposer; fail closed", i)}
		}
		input.Links = append(input.Links, deriveLink{
			From: link.FromFactID, To: link.ToFactID, Proof: link.Proof, Proposer: link.Proposer,
		})
	}
	sort.Slice(input.Links, func(i, j int) bool {
		if input.Links[i].From != input.Links[j].From {
			return input.Links[i].From < input.Links[j].From
		}
		if input.Links[i].To != input.Links[j].To {
			return input.Links[i].To < input.Links[j].To
		}
		return input.Links[i].Proof < input.Links[j].Proof
	})
	input.Taxonomy = deriveTaxonomy{Version: taxonomy.TaxonomyVersion}
	for _, term := range taxonomy.Terms {
		input.Taxonomy.Terms = append(input.Taxonomy.Terms, term.Term)
	}
	sort.Strings(input.Taxonomy.Terms)
	return input, factsByID, nil
}

func floorMinutes(diffSeconds int64) int64 {
	q := diffSeconds / 60
	if diffSeconds < 0 && diffSeconds%60 != 0 {
		q--
	}
	return q
}

// evaluate runs the three derive queries against the entity policy.
func (e *Engine) evaluate(request DeriveRequest, input deriveInput) ([]outcomeRow, []loserRow, []verifiedLinkRow, error) {
	mkitBin := os.Getenv("MANGLEKIT_BIN")
	if mkitBin == "" {
		mkitBin = "mkit"
	}
	policyFile := filepath.Join(e.policyDir, request.EntityType, request.PolicyVersion+".mg")
	if _, err := os.Stat(policyFile); err != nil {
		return nil, nil, nil, &RequestError{Code: "UNKNOWN_POLICY_VERSION", Message: fmt.Sprintf("policy file %s is missing; fail closed", policyFile)}
	}

	payload, err := json.Marshal(input)
	if err != nil {
		return nil, nil, nil, &RequestError{Code: "INTERNAL_ERROR", Message: "cannot encode derive input"}
	}
	dir, err := os.MkdirTemp("", "mosaic-derive-")
	if err != nil {
		return nil, nil, nil, &RequestError{Code: "INTERNAL_ERROR", Message: "cannot stage derive input"}
	}
	defer os.RemoveAll(dir)
	dataFile := filepath.Join(dir, "input.json")
	if err := os.WriteFile(dataFile, payload, 0o600); err != nil {
		return nil, nil, nil, &RequestError{Code: "INTERNAL_ERROR", Message: "cannot stage derive input"}
	}

	outcomes, err := runEval(mkitBin, policyFile, dataFile, "outcome(A, V, X, R)")
	if err != nil {
		return nil, nil, nil, err
	}
	var outcomeRows []outcomeRow
	if err := json.Unmarshal(outcomes, &outcomeRows); err != nil {
		return nil, nil, nil, &RequestError{Code: "INTERNAL_ERROR", Message: "cannot parse manglekit outcome: " + err.Error()}
	}
	losersRaw, err := runEval(mkitBin, policyFile, dataFile, "loser(A, F)")
	if err != nil {
		return nil, nil, nil, err
	}
	var loserRows []loserRow
	if err := json.Unmarshal(losersRaw, &loserRows); err != nil {
		return nil, nil, nil, &RequestError{Code: "INTERNAL_ERROR", Message: "cannot parse manglekit losers: " + err.Error()}
	}
	linksRaw, err := runEval(mkitBin, policyFile, dataFile, "vlink(F, T, P)")
	if err != nil {
		return nil, nil, nil, err
	}
	var verifiedRows []verifiedLinkRow
	if err := json.Unmarshal(linksRaw, &verifiedRows); err != nil {
		return nil, nil, nil, &RequestError{Code: "INTERNAL_ERROR", Message: "cannot parse manglekit links: " + err.Error()}
	}
	return outcomeRows, loserRows, verifiedRows, nil
}

func runEval(mkitBin, policyFile, dataFile, query string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), deriveTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, mkitBin, "eval", "-p", policyFile, "-d", dataFile, "-q", query)
	out, err := cmd.Output()
	if ctx.Err() == context.DeadlineExceeded {
		return nil, &RequestError{Code: "ENGINE_TIMEOUT", Message: "manglekit eval timed out; fail closed, no golden facts"}
	}
	if err != nil {
		msg := strings.TrimSpace(string(out))
		if exitErr, ok := err.(*exec.ExitError); ok {
			msg = strings.TrimSpace(string(exitErr.Stderr))
		}
		if len(msg) > 500 {
			msg = msg[:500]
		}
		return nil, &RequestError{Code: "ENGINE_ERROR", Message: "manglekit eval failed; fail closed, no golden facts: " + msg}
	}
	trimmed := json.RawMessage(strings.TrimSpace(string(out)))
	if string(trimmed) == "" || string(trimmed) == "null" {
		return []byte("[]"), nil
	}
	if strings.HasPrefix(string(trimmed), "No results") {
		return []byte("[]"), nil
	}
	return trimmed, nil
}

// fallbackFor maps an attribute to the downstream action a steward must avoid
// while the attribute waits in the queue.
func fallbackFor(attribute string) string {
	switch attribute {
	case "phone":
		return "do not use the new phone for OTP or payment notification"
	case "email":
		return "do not send notifications to the new email"
	case "address":
		return "do not ship to the new address"
	case "category":
		return "do not list under the new category"
	default:
		return "do not use the new value downstream"
	}
}

// mapOutput converts manglekit rows to golden facts, conflicts, steward
// items and verified links. Escalated attributes never produce golden facts.
func mapOutput(request DeriveRequest, input deriveInput, factsByID map[string]CandidateFact, outcomes []outcomeRow, losers []loserRow, verified []verifiedLinkRow) *DeriveOutput {
	losersByAttr := map[string][]string{}
	for _, row := range losers {
		losersByAttr[row.A] = append(losersByAttr[row.A], row.F)
	}
	for _, ids := range losersByAttr {
		sort.Strings(ids)
	}

	// Evaluation time in +07:00 for steward deadlines (eval time + 24h).
	evalTime := timeBase.Add(time.Duration(input.NowMin) * time.Minute)
	deadline := evalTime.Add(24 * time.Hour).Format(time.RFC3339)

	out := &DeriveOutput{
		DecisionID:      request.DecisionID,
		PolicyVersion:   request.PolicyVersion,
		TaxonomyVersion: request.TaxonomyVersion,
		GoldenFacts:     []GoldenFact{},
		Conflicts:       []ConflictFact{},
		StewardQueue:    []StewardItem{},
		VerifiedLinks:   []VerifiedLink{},
	}

	sort.Slice(outcomes, func(i, j int) bool {
		if outcomes[i].A != outcomes[j].A {
			return outcomes[i].A < outcomes[j].A
		}
		return outcomes[i].V < outcomes[j].V
	})
	for _, row := range outcomes {
		switch row.V {
		case "published":
			winner, ok := factsByID[row.X]
			if !ok {
				continue
			}
			loserIDs := losersByAttr[row.A]
			if loserIDs == nil {
				loserIDs = []string{}
			}
			out.GoldenFacts = append(out.GoldenFacts, GoldenFact{
				EntityType:      request.EntityType,
				EntityID:        request.EntityID,
				Attribute:       row.A,
				Value:           winner.Value,
				WinnerFactID:    winner.FactID,
				LoserFactIDs:    loserIDs,
				RuleFired:       row.R,
				PolicyVersion:   request.PolicyVersion,
				TaxonomyVersion: request.TaxonomyVersion,
				Lineage:         fmt.Sprintf("%s/%s -> %s -> published", winner.Source, winner.SourceID, row.R),
			})
		case "escalated", "denied":
			factIDs := []string{}
			for _, f := range input.Facts {
				if f.Attribute == row.A {
					factIDs = append(factIDs, f.FactID)
				}
			}
			sort.Strings(factIDs)
			out.Conflicts = append(out.Conflicts, ConflictFact{
				EntityType:      request.EntityType,
				EntityID:        request.EntityID,
				Attribute:       row.A,
				ReasonCode:      row.X,
				RuleFired:       row.R,
				FactIDs:         factIDs,
				PolicyVersion:   request.PolicyVersion,
				TaxonomyVersion: request.TaxonomyVersion,
			})
			if row.V == "escalated" {
				out.StewardQueue = append(out.StewardQueue, StewardItem{
					StewardID:  fmt.Sprintf("st-%s-%s", request.DecisionID, row.A),
					EntityType: request.EntityType,
					EntityID:   request.EntityID,
					Attribute:  row.A,
					Owner:      "steward-001",
					Deputy:     "steward-002",
					Deadline:   deadline,
					Fallback:   fallbackFor(row.A),
					Status:     "pending",
					DecisionID: request.DecisionID,
				})
			}
		}
	}

	sort.Slice(out.GoldenFacts, func(i, j int) bool { return out.GoldenFacts[i].Attribute < out.GoldenFacts[j].Attribute })
	sort.Slice(out.Conflicts, func(i, j int) bool {
		if out.Conflicts[i].Attribute != out.Conflicts[j].Attribute {
			return out.Conflicts[i].Attribute < out.Conflicts[j].Attribute
		}
		return out.Conflicts[i].ReasonCode < out.Conflicts[j].ReasonCode
	})
	sort.Slice(out.StewardQueue, func(i, j int) bool { return out.StewardQueue[i].Attribute < out.StewardQueue[j].Attribute })

	for _, row := range verified {
		out.VerifiedLinks = append(out.VerifiedLinks, VerifiedLink{
			FromFactID:      row.F,
			ToFactID:        row.T,
			Proof:           row.P,
			RuleFired:       "L1",
			PolicyVersion:   request.PolicyVersion,
			TaxonomyVersion: request.TaxonomyVersion,
		})
	}
	sort.Slice(out.VerifiedLinks, func(i, j int) bool {
		if out.VerifiedLinks[i].FromFactID != out.VerifiedLinks[j].FromFactID {
			return out.VerifiedLinks[i].FromFactID < out.VerifiedLinks[j].FromFactID
		}
		if out.VerifiedLinks[i].ToFactID != out.VerifiedLinks[j].ToFactID {
			return out.VerifiedLinks[i].ToFactID < out.VerifiedLinks[j].ToFactID
		}
		return out.VerifiedLinks[i].Proof < out.VerifiedLinks[j].Proof
	})

	switch {
	case len(out.StewardQueue) > 0:
		out.Verdict = "escalated"
	case len(out.GoldenFacts) > 0:
		out.Verdict = "published"
	default:
		out.Verdict = "denied"
	}
	out.RuleFired = primaryRule(outcomes)
	return out
}

// primaryRule picks the single rule recorded on the decision: the most severe
// escalation rule present, else the first denial, else R1.
func primaryRule(outcomes []outcomeRow) string {
	severity := map[string]int{
		"R2": 0, "R3": 1, "L1": 2, "C1": 3, "C2": 4, "T1": 5, "R4": 6, "R1": 7,
	}
	best, bestSev := "R1", 7
	for _, row := range outcomes {
		if sev, ok := severity[row.R]; ok && sev < bestSev {
			best, bestSev = row.R, sev
		}
	}
	return best
}
