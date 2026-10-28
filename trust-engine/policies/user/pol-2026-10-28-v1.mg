% Muse Mosaic trust policy - user entity.
% policy_version: pol-2026-10-28-v1
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
