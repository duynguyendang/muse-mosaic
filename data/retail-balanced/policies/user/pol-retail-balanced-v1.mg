% Muse Mosaic trust policy - user entity, retail_balanced profile.
% policy_version: pol-retail-balanced-v1
% entity_type: user
%
% Profile: Enterprise Commercial (Retail Balanced).
% Priority: maximise zero-touch golden creation using a source hierarchy
%   plus verified transaction freshness. Compliance quarantine is narrowed:
%   a value conflict only stops publication when it happens INSIDE the
%   winning tier. Cross-tier disagreement resolves to the higher tier
%   (authority beats recency), it does not escalate.
%
% Source hierarchy (weight): ERP master 3 > verified order/OTP 2 > CRM claim 1.
%   source_tier(Src, W) below encodes that hierarchy for every source this
%   dataset can emit; an unlisted source defaults to weight 1 so a novel feed
%   never silently outranks the ERP master.
%
% Multi-valued attributes: order_id is a repeatable business key (a customer
%   HAS many orders). It is declared multivalued so it is never treated as a
%   uniqueness constraint: no C1 quarantine and no C1 escalation is derived
%   for it. That single declaration is what removes the mass quarantine the
%   strict profile produced on this dataset.
%
% STATUS: runnable manglekit Datalog. Evaluated by the trust-engine with
%   mkit eval -p <this file> -d <derive-request.json> -q 'outcome(A, V, X, R)'
%   mkit eval -p <this file> -d <derive-request.json> -q 'loser(A, F)'
%   mkit eval -p <this file> -d <derive-request.json> -q 'vlink(F, T, P)'.
%
% Input canonicalization is mechanical (trust-engine derive.go): event_time
%   -> event_min, expires_min = event_min + ttl/60, now_min = max event_min.
%   Evaluation time is hermetic: identical facts derive identical verdicts.

% ---------------------------------------------------------------- hierarchy
source_tier("sap-like", 3).
source_tier("erp-like", 3).
source_tier("orders", 2).
source_tier("order-like", 2).
source_tier("ticket-verified", 2).
source_tier("crm", 1).
source_tier("salesforce-like", 1).
source_tier("ciam-like", 1).
source_tier("ticket", 1).
source_tier("marketplace-like", 1).
source_tier("catalog", 1).
source_tier("pim-like", 1).

% Attribute authority matrix: the rank that a source claims for an attribute.
auth_rank("phone", "crm", 1).
auth_rank("email", "crm", 1).
auth_rank("address", "crm", 1).
auth_rank("display_name", "crm", 1).
auth_rank("order_id", "orders", 1).
auth_rank("phone", "sap-like", 1).
auth_rank("email", "sap-like", 1).
auth_rank("address", "sap-like", 1).
auth_rank("display_name", "sap-like", 1).
auth_rank("phone", "salesforce-like", 2).
auth_rank("email", "salesforce-like", 2).
auth_rank("address", "salesforce-like", 2).
auth_rank("display_name", "salesforce-like", 2).
auth_rank("phone", "ciam-like", 3).
auth_rank("email", "ciam-like", 1).
auth_rank("address", "ciam-like", 3).
auth_rank("display_name", "ciam-like", 3).
auth_rank("phone", "ticket", 3).
auth_rank("email", "ticket", 3).
auth_rank("address", "ticket-verified", 1).
auth_rank("display_name", "ticket", 3).
auth_rank("order_id", "marketplace-like", 2).
auth_rank("order_id", "ticket", 3).

% Repeated business keys: never uniqueness-constrained, never quarantined.
multivalued("order_id").

% Retail has no payment-gating attribute: a weak claim is repaired from a
% higher tier rather than held for a human, so nothing is critical here.
critical("__none__").

% No categorical attributes on user v1 (T1 is vacuous; the fact below only
% satisfies the closed-program check and can never match a live attribute).
categorical("__none__").

known_source(S) :- source_tier(S, _).

has_matrix(A) :- auth_rank(A, _, _).

fact(F, A, V, S, R, E, B) :- json_link("input", "facts", L), json_link(L, _, N), json_str(N, "fact_id", F), json_str(N, "attribute", A), json_str(N, "value", V), json_str(N, "source", S), json_num(N, "authority_rank", R), json_num(N, "expires_min", E), json_bool(N, "verified", B).
eval_time(Now) :- json_num("input", "now_min", Now).
requested(A) :- json_link("input", "attributes", L), json_str(L, _, A).

expired_fact(F) :- fact(F, _, _, _, _, E, _), eval_time(Now), E < Now.
live(F, A, V, S, R, E, B) :- fact(F, A, V, S, R, E, B), !expired_fact(F).
attr(A) :- live(_, A, _, _, _, _, _).

% ------------------------------------------------- tier-aware winner selection
% Weight of a fact: declared source tier, else 1 for an unknown source.
weight(F, 1) :- fact(F, _, _, S, _, _, _), !known_source(S).
weight(F, W) :- fact(F, _, _, S, _, _, _), source_tier(S, W).

% Highest tier present for an attribute. The three tiers are enumerated as
% constants because manglekit requires comparison operands to be bound
% (a head variable in `W2 > W` is rejected by the analyzer); the hierarchy
% has exactly three levels, so unrolling it is both correct and explicit.
higher_tier_exists(A, 2) :- live(F, A, _, _, _, _, _), weight(F, 3).
higher_tier_exists(A, 1) :- live(F, A, _, _, _, _, _), weight(F, 3).
higher_tier_exists(A, 1) :- live(F, A, _, _, _, _, _), weight(F, 2).

best_tier(A, 3) :- live(F, A, _, _, _, _, _), weight(F, 3).
best_tier(A, 2) :- live(F, A, _, _, _, _, _), weight(F, 2), !higher_tier_exists(A, 2).
best_tier(A, 1) :- live(F, A, _, _, _, _, _), weight(F, 1), !higher_tier_exists(A, 1).

% Facts competing for an attribute: live AND already at the winning tier.
rival(F, A, V, S, R, E, B) :- live(F, A, V, S, R, E, B), weight(F, W), best_tier(A, W).

% Freshness/verification tie-break INSIDE the winning tier.
better(G, F) :- rival(G, A, _, _, RG, _, _), rival(F, A, _, _, RF, _, _), G != F, RG < RF.
better(G, F) :- rival(G, A, _, _, R, _, "true"), rival(F, A, _, _, R, _, "false"), G != F.
better(G, F) :- rival(G, A, _, _, R, EG, B), rival(F, A, _, _, R, EF, B), G != F, EG > EF.
beaten(F) :- rival(G, _, _, _, _, _, _), rival(F, _, _, _, _, _, _), better(G, F).
win(F) :- rival(F, _, _, _, _, _, _), !beaten(F).

has_verified(A) :- live(_, A, _, _, _, _, "true").
winners_disagree(A) :- win(F1), win(F2), F1 != F2, rival(F1, A, V1, _, _, _, _), rival(F2, A, V2, _, _, _, _), V1 != V2.
mixed_tie(A) :- win(F), rival(F, A, _, _, R, _, _), R > 1.
mixed_tie(A) :- win(F), rival(F, A, _, _, _, _, "false").

% Where the disagreement comes from decides whether it is a TRUST conflict.
% Two different systems asserting two different values is a real cross-system
% dispute -> quarantine (C1). One system handing us two rows for the same
% attribute is a duplicate inside that system's own feed: retail resolves it
% by hierarchy and freshness instead of holding the customer for a human,
% which is the whole point of this profile. Note this dataset's freshness is
% deliberately flat (one import-batch timestamp), so freshness cannot break
% these ties -- that is exactly why the duplicate case must not escalate.
cross_source(A) :- win(F1), win(F2), F1 != F2, rival(F1, A, V1, S1, _, _, _), rival(F2, A, V2, S2, _, _, _), V1 != V2, S1 != S2.

% ------------------------------------------------------------------- linkage
link(F, T, P) :- json_link("input", "links", L), json_link(L, _, N), json_str(N, "from_fact", F), json_str(N, "to_fact", T), json_str(N, "proof", P).
anchor(A) :- json_str("input", "anchor_fact", A).
sole_proof("exact_email").
sole_proof("exact_phone").
sole_proof("exact_order").
verified_hop(F, T, "exact_email") :- link(F, T, "exact_email"), live(F, "email", V, _, _, _, _), live(T, "email", V, _, _, _, _), F != T.
verified_hop(F, T, "exact_phone") :- link(F, T, "exact_phone"), live(F, "phone", V, _, _, _, _), live(T, "phone", V, _, _, _, _), F != T.
verified_hop(F, T, "exact_order") :- link(F, T, "exact_order"), live(F, "order_id", V, _, _, _, _), live(T, "order_id", V, _, _, _, _), F != T.
hop(F, T) :- link(F, T, P), sole_proof(P), verified_hop(F, T, P).
hop(F, T) :- link(T, F, P), sole_proof(P), verified_hop(T, F, P).
hop(F, T) :- link(F, T, "same_fragment"), fact(F, _, _, _, _, _, _), fact(T, _, _, _, _, _, _), F != T.
hop(F, T) :- link(T, F, "same_fragment"), fact(F, _, _, _, _, _, _), fact(T, _, _, _, _, _, _), F != T.
grounded(F) :- anchor(F).
grounded(G) :- grounded(F), hop(F, G).
has_links("yes") :- link(_, _, _).
detached(F) :- live(F, _, _, _, _, _, _), has_links("yes"), !grounded(F).
detached_winner(A) :- win(F), live(F, A, _, _, _, _, _), detached(F).

tax_term(V) :- json_link("input", "taxonomy", T), json_link(T, "terms", L), json_str(L, _, V).

% ---------------------------------------------------------------- escalations
% C2 (weakest-link chain) is narrowed to UNVERIFIED winners only. Retail's
% stated priority is maximising zero-touch golden creation: a verified rank-1
% value from its own system is publishable even when it is not hop-reachable
% from the anchor fact, because the anchor is only the smallest fact id in
% the input -- an ordering artifact, not a trust statement. Requiring grounding
% for every winner quarantined 142 of 209 clusters (523 CHAIN_BROKEN) on this
% dataset and contradicted the profile's own ruleset, which lists no C2.
% Narrowed to same-tier ambiguity only. Cross-tier disagreement is resolved
% by hierarchy, not escalated; multivalued keys never escalate at all.
esc(A, "MISSING_OR_EXPIRED", "R3") :- requested(A), !attr(A), !multivalued(A).
esc(A, "NO_AUTHORITY_RANKS", "R4") :- attr(A), !has_matrix(A), !multivalued(A).
esc(A, "WINNER_TIE_AMBIGUOUS", "R2") :- attr(A), has_matrix(A), !multivalued(A), winners_disagree(A), mixed_tie(A).
esc(A, "CLUSTER_CONFLICT", "C1") :- attr(A), has_matrix(A), !multivalued(A), cross_source(A), !mixed_tie(A).
esc(A, "CHAIN_BROKEN", "C2") :- attr(A), has_matrix(A), !multivalued(A), win(F), live(F, A, _, _, _, _, "false"), detached(F).
esc(A, "TAXONOMY_UNKNOWN_TERM", "T1") :- attr(A), has_matrix(A), categorical(A), !multivalued(A), win(F), live(F, A, V, _, _, _, _), !tax_term(V), !denied_attr(A).

% Only an attribute with no verified claim at all is held back, and never a
% repeated business key (order_id has no verification semantics here).
den(A, "WEAK_PROVENANCE", "R4") :- attr(A), has_matrix(A), !critical(A), !multivalued(A), !has_verified(A).

escalated(A) :- esc(A, W, Rule).
denied_attr(A) :- den(A, W, Rule).
% A same-source duplicate set publishes: every tied winner becomes a golden
% candidate and the non-winners are recorded as losers in lineage. The
% publisher collapses the set to one golden value deterministically (outcomes
% arrive sorted by attribute then value), so the choice is reproducible and
% auditable rather than random.
pub(A, F) :- win(F), rival(F, A, _, _, _, _, _), !escalated(A), !denied_attr(A).

outcome(A, "published", F, "R1") :- pub(A, F).
outcome(A, "escalated", Why, Rule) :- esc(A, Why, Rule).
outcome(A, "denied", Why, Rule) :- den(A, Why, Rule).

loser(A, F) :- pub(A, W), rival(F, A, V, _, _, _, _), rival(W, A, VW, _, _, _, _), V != VW, F != W.

vlink(F, T, P) :- verified_hop(F, T, P).

% ---- metrics union: one mkit call returns verdicts AND verified links ----
% A subprocess spawn costs ~20ms and is the dominant cost of evaluating a
% cluster, so the dashboard cannot afford two separate queries (outcome +
% vlink) per cluster. These two clauses put both result sets behind one
% predicate with a tag, so the metrics endpoint asks once:
%
%   metric(Tag, A, V, X, R)
%
% Tag "out" -> (A = attribute, V = verdict, X = reason/fact id, R = rule)
% Tag "lnk" -> (A = from_fact, V = to_fact, X = proof, R = rule "L1")
%
% The clauses reference only predicates this profile already defines, so a
% profile that never defines vlink simply contributes no "lnk" rows.
metric("out", A, V, X, R) :- outcome(A, V, X, R).
metric("lnk", F, T, P, "L1") :- vlink(F, T, P).

% >>> SEMANTIC-BLOCK v1 (managed by scripts/apply_semantic_block.py)
% ---- SEMANTIC EVIDENCE (S1, S2) -- canonical block, shared by every user policy.
% Do not edit a copy by hand: edit this file and run scripts/apply_semantic_block.py.
%
% A Muse semantic link claims two facts are the same identifier written
% differently. This block lets the engine VERIFY the claim rather than trust it.
% The trust-engine computes mechanical features per fact (semantic.go):
%   phone_key  = last 9 digits of the phone, extension dropped
%   email_dels = the email, plus the local part with each one character deleted
% and Datalog decides:
%   S1 semantic_phone: both facts are phones, same phone_key.
%   S2 semantic_email: both facts are emails, share a deletion key, i.e. same
%      domain and edit distance <= 1; locals under 8 characters emit no keys.
% A verified semantic hop grounds the chain exactly like an exact hop. The claim
% itself is only a request: with no matching evidence there is no hop, and a
% fuzzy_name_address link (names alone) never verifies.
phone_key(F, K) :- json_link("input", "facts", L), json_link(L, _, N), json_str(N, "fact_id", F), json_str(N, "phone_key", K).
email_del(F, K) :- json_link("input", "facts", L), json_link(L, _, N), json_str(N, "fact_id", F), json_link(N, "email_dels", D), json_str(D, _, K).
sole_proof("semantic_phone").
sole_proof("semantic_email").
verified_hop(F, T, "semantic_phone") :- link(F, T, "semantic_phone"), live(F, "phone", _, _, _, _, _), live(T, "phone", _, _, _, _, _), phone_key(F, K), phone_key(T, K), F != T.
verified_hop(F, T, "semantic_email") :- link(F, T, "semantic_email"), live(F, "email", _, _, _, _, _), live(T, "email", _, _, _, _, _), email_del(F, K), email_del(T, K), F != T.
% ---- END SEMANTIC EVIDENCE
% <<< SEMANTIC-BLOCK v1

% >>> SEMANTIC-GUARD v1 (managed by scripts/apply_semantic_block.py)
% ---- SEMANTIC GUARD (S3) -- canonical block, for policies that can escalate.
% Do not edit a copy by hand: edit this file and run scripts/apply_semantic_block.py.
%
% S3: a merge claimed on names alone must never certify a value. A Muse
% fuzzy_name_address link is not a hop, so the facts it was meant to join stay
% cut off from the anchor (detached). When a cluster carries such a claim, a
% winner that is still detached is escalated instead of published, unless an
% engine-verified semantic hop (S1/S2) or an exact hop has grounded it.
% Not installed on analytical_mesh: that profile has no escalation rules by design.
has_merge_claim("yes") :- link(_, _, "fuzzy_name_address").
esc(A, "SEMANTIC_MERGE_UNVERIFIED", "S3") :- attr(A), has_matrix(A), has_merge_claim("yes"), win(F), live(F, A, _, _, _, _, _), detached(F).
% ---- END SEMANTIC GUARD
% <<< SEMANTIC-GUARD v1
