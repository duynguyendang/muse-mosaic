% Muse Mosaic trust policy - user entity.
% policy_version: pol-2026-11-02-v1
% v3 change: adds the semantic-evidence block (S1 semantic_phone, S2 semantic_email)
% so a Muse semantic link can be VERIFIED by the engine. All v2 rules untouched.
% v2 change: display_name authority ranks added (sap 1, salesforce 2,
% ciam 3, ticket 3); agreeing display_name now publishes R1 instead of
% escalating R4 NO_AUTHORITY_RANKS. No other rule touched.
% entity_type: user
%
% STATUS: runnable manglekit Datalog. Evaluated by the trust-engine with
%   mkit eval -p <this file> -d <derive-request.json> -q 'outcome(A, V, X, R)'
%   mkit eval -p <this file> -d <derive-request.json> -q 'loser(A, F)'
%   mkit eval -p <this file> -d <derive-request.json> -q 'vlink(F, T, P)'.
%
% Input canonicalization (trust-engine adapter.go + derive.go, mechanical, not
% derivation): event_time -> event_min, expires_min, now_min; links validated
% against the closed proof vocabulary; anchor_fact = smallest fact id;
% taxonomy terms injected from the requested taxonomy version.
%
% Rule summary:
%   R3: expired fact treated as missing.
%   R1: authority wins - lowest rank, then verified, then latest expiry.
%   R2: escalate, never guess - critical winner unverified, or mixed tie.
%   R4: deny weak provenance / no authority ranks.
%   L1: link proof - a sole-proof hop (exact email/phone/order) verifies only
%       if the endpoint live facts actually share that exact value.
%   C1: cluster uniqueness - verified rank-1 exact tie (e.g. trap with a
%       one-digit-different phone) is a conflict, never a merge.
%   C2: weakest-link chain - a winner cut off from the anchor by unverified
%       hops is escalated, not silently published.
%   T1: taxonomy membership for categorical attributes (none on user v1).
%   I1: idempotency - same input derives same output; the decision id and
%       idempotency key echo through every row (no rule needed, by construction).

auth_rank("phone", "sap-like", 1).
auth_rank("phone", "salesforce-like", 2).
auth_rank("phone", "ciam-like", 3).
auth_rank("phone", "ticket", 3).
auth_rank("email", "ciam-like", 1).
auth_rank("email", "salesforce-like", 2).
auth_rank("email", "sap-like", 3).
auth_rank("email", "ticket", 3).
auth_rank("address", "ticket-verified", 1).
auth_rank("address", "salesforce-like", 2).
auth_rank("address", "sap-like", 3).
auth_rank("address", "ciam-like", 3).
auth_rank("order_id", "order-like", 1).
auth_rank("order_id", "marketplace-like", 2).
auth_rank("order_id", "ticket", 3).
auth_rank("display_name", "sap-like", 1).
auth_rank("display_name", "salesforce-like", 2).
auth_rank("display_name", "ciam-like", 3).
auth_rank("display_name", "ticket", 3).

critical("phone").

% No categorical attributes on user v1 (T1 is vacuous here but the rule core
% is shared; the fact below only satisfies the closed-program check and can
% never match a live attribute).
categorical("__none__").

has_matrix(A) :- auth_rank(A, _, _).

fact(F, A, V, S, R, E, B) :- json_link("input", "facts", L), json_link(L, _, N), json_str(N, "fact_id", F), json_str(N, "attribute", A), json_str(N, "value", V), json_str(N, "source", S), json_num(N, "authority_rank", R), json_num(N, "expires_min", E), json_bool(N, "verified", B).
eval_time(Now) :- json_num("input", "now_min", Now).
requested(A) :- json_link("input", "attributes", L), json_str(L, _, A).

expired_fact(F) :- fact(F, _, _, _, _, E, _), eval_time(Now), E < Now.
live(F, A, V, S, R, E, B) :- fact(F, A, V, S, R, E, B), !expired_fact(F).
attr(A) :- live(_, A, _, _, _, _, _).

better(G, F) :- live(G, A, _, _, RG, _, _), live(F, A, _, _, RF, _, _), G != F, RG < RF.
better(G, F) :- live(G, A, _, _, R, _, "true"), live(F, A, _, _, R, _, "false"), G != F.
better(G, F) :- live(G, A, _, _, R, EG, B), live(F, A, _, _, R, EF, B), G != F, EG > EF.
beaten(F) :- live(G, _, _, _, _, _, _), live(F, _, _, _, _, _, _), better(G, F).
win(F) :- live(F, _, _, _, _, _, _), !beaten(F).

has_verified(A) :- live(_, A, _, _, _, _, "true").
winners_disagree(A) :- win(F1), win(F2), F1 != F2, live(F1, A, V1, _, _, _, _), live(F2, A, V2, _, _, _, _), V1 != V2.
mixed_tie(A) :- win(F), live(F, A, _, _, R, _, _), R > 1.
mixed_tie(A) :- win(F), live(F, A, _, _, _, _, "false").

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
% Co-location is structural membership, not a value claim: it traverses
% expired facts too (an old observation still links its fragment).
hop(F, T) :- link(F, T, "same_fragment"), fact(F, _, _, _, _, _, _), fact(T, _, _, _, _, _, _), F != T.
hop(F, T) :- link(T, F, "same_fragment"), fact(F, _, _, _, _, _, _), fact(T, _, _, _, _, _, _), F != T.
grounded(F) :- anchor(F).
grounded(G) :- grounded(F), hop(F, G).
has_links("yes") :- link(_, _, _).
detached(F) :- live(F, _, _, _, _, _, _), has_links("yes"), !grounded(F).
detached_winner(A) :- win(F), live(F, A, _, _, _, _, _), detached(F).

tax_term(V) :- json_link("input", "taxonomy", T), json_link(T, "terms", L), json_str(L, _, V).

esc(A, "MISSING_OR_EXPIRED", "R3") :- requested(A), !attr(A).
esc(A, "NO_AUTHORITY_RANKS", "R4") :- attr(A), !has_matrix(A).
esc(A, "CRITICAL_LOW_CONFIDENCE", "R2") :- attr(A), has_matrix(A), critical(A), win(F), live(F, A, _, _, _, _, "false").
esc(A, "WINNER_TIE_AMBIGUOUS", "R2") :- attr(A), has_matrix(A), winners_disagree(A), mixed_tie(A).
esc(A, "CLUSTER_CONFLICT", "C1") :- attr(A), has_matrix(A), winners_disagree(A), !mixed_tie(A).
esc(A, "CHAIN_BROKEN", "C2") :- attr(A), detached_winner(A).
esc(A, "TAXONOMY_UNKNOWN_TERM", "T1") :- attr(A), has_matrix(A), categorical(A), win(F), live(F, A, V, _, _, _, _), !tax_term(V), !denied_attr(A).
den(A, "WEAK_PROVENANCE", "R4") :- attr(A), has_matrix(A), !critical(A), !has_verified(A).

escalated(A) :- esc(A, W, Rule).
denied_attr(A) :- den(A, W, Rule).
pub(A, F) :- win(F), live(F, A, _, _, _, _, _), !escalated(A), !denied_attr(A), !winners_disagree(A).

outcome(A, "published", F, "R1") :- pub(A, F).
outcome(A, "escalated", Why, Rule) :- esc(A, Why, Rule).
outcome(A, "denied", Why, Rule) :- den(A, Why, Rule).

loser(A, F) :- pub(A, W), live(F, A, V, _, _, _, _), live(W, A, VW, _, _, _, _), V != VW, F != W.

vlink(F, T, P) :- verified_hop(F, T, P).

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
