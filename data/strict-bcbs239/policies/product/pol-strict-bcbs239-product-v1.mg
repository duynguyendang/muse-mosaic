% Muse Mosaic trust policy - Kaggle catalog product entity (demo import: single catalog source rank 1).
% policy_version: pol-strict-bcbs239-product-v1
% entity_type: product
%
% STATUS: runnable manglekit Datalog. Same rule core as the user policy
% (R1 authority wins, R2 escalate critical ambiguity, R3 expiry is missing,
% R4 deny weak provenance, L1 link proof, C1 cluster uniqueness, C2
% weakest-link chain, T1 taxonomy membership, I1 determinism). Only the
% authority matrix, the critical set, and the categorical attributes differ.
% See the user policy header for input canonicalization and evaluation-time
% semantics.
%
% Product note: PIM rank 1 beats ERP rank 2 beats marketplace rank 3.
% Frequency never beats authority: two marketplace facts for one label do not
% outrank one PIM fact.
% No critical attributes are defined for products in v1: a weak category claim
% is denied (R4), never auto-created. A non-term category escalates (T1).

auth_rank("category", "catalog", 1).

categorical("category").

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
esc(A, "WINNER_TIE_AMBIGUOUS", "R2") :- attr(A), has_matrix(A), winners_disagree(A), mixed_tie(A).
esc(A, "CLUSTER_CONFLICT", "C1") :- attr(A), has_matrix(A), winners_disagree(A), !mixed_tie(A).
esc(A, "CHAIN_BROKEN", "C2") :- attr(A), has_matrix(A), detached_winner(A).
esc(A, "TAXONOMY_UNKNOWN_TERM", "T1") :- attr(A), has_matrix(A), categorical(A), win(F), live(F, A, V, _, _, _, _), !tax_term(V), !denied_attr(A).
den(A, "WEAK_PROVENANCE", "R4") :- attr(A), has_matrix(A), !has_verified(A).

escalated(A) :- esc(A, W, Rule).
denied_attr(A) :- den(A, W, Rule).
pub(A, F) :- win(F), live(F, A, _, _, _, _, _), !escalated(A), !denied_attr(A), !winners_disagree(A).

outcome(A, "published", F, "R1") :- pub(A, F).
outcome(A, "escalated", Why, Rule) :- esc(A, Why, Rule).
outcome(A, "denied", Why, Rule) :- den(A, Why, Rule).

loser(A, F) :- pub(A, W), live(F, A, V, _, _, _, _), live(W, A, VW, _, _, _, _), V != VW, F != W.

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
