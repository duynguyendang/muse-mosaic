% Muse Mosaic trust policy - user entity, analytical_mesh profile.
% policy_version: pol-analytical-mesh-v1
% entity_type: user
%
% Profile: Analytical Mesh (Feature Store / AI Training Graph).
% Priority: non-destructive multi-value lineage. Every distinct observed
%   value is retained as a golden candidate; NOTHING is frozen or held for a
%   human. There are no quarantine (esc) rules and no deny (den) rules in
%   this profile at all, so zero-quarantine is guaranteed by construction --
%   not by hoping the rules do not fire.
%
% This is deliberately the opposite of strict_bcbs239: a feature store wants
%   the full observed value distribution (including the conflicting one) so a
%   model can learn the ambiguity, whereas a payments golden must never
%   contain two answers. Downstream consumers of this profile must treat the
%   result as OBSERVED CLAIMS, not as attested truth.
%
% The closed-program placeholders (__none__) exist because manglekit requires
% the declared predicates to be non-empty; they can never match a live value.
%
% STATUS: runnable manglekit Datalog. Evaluated by the trust-engine with
%   mkit eval -p <this file> -d <derive-request.json> -q 'outcome(A, V, X, R)'
%   mkit eval -p <this file> -d <derive-request.json> -q 'loser(A, F)'
%   mkit eval -p <this file> -d <derive-request.json> -q 'vlink(F, T, P)'.
%
% Input canonicalization is mechanical (trust-engine derive.go): event_time
%   -> event_min, expires_min = event_min + ttl/60, now_min = max event_min.
%   Evaluation time is hermetic: identical facts derive identical verdicts.

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

% No attribute is critical: this profile never blocks on confidence.
critical("__none__").
% No categorical attributes on user v1 (T1 vacuous; closed-program check).
categorical("__none__").

has_matrix(A) :- auth_rank(A, _, _).

fact(F, A, V, S, R, E, B) :- json_link("input", "facts", L), json_link(L, _, N), json_str(N, "fact_id", F), json_str(N, "attribute", A), json_str(N, "value", V), json_str(N, "source", S), json_num(N, "authority_rank", R), json_num(N, "expires_min", E), json_bool(N, "verified", B).
eval_time(Now) :- json_num("input", "now_min", Now).
requested(A) :- json_link("input", "attributes", L), json_str(L, _, A).

expired_fact(F) :- fact(F, _, _, _, _, E, _), eval_time(Now), E < Now.
live(F, A, V, S, R, E, B) :- fact(F, A, V, S, R, E, B), !expired_fact(F).
attr(A) :- live(_, A, _, _, _, _, _).

% Still expose a canonical winner so `loser(A, F)` can name the alternatives
% a consumer is NOT taking. Publishing does not depend on this: every live
% fact publishes regardless of whether it wins.
better(G, F) :- live(G, A, _, _, RG, _, _), live(F, A, _, _, RF, _, _), G != F, RG < RF.
better(G, F) :- live(G, A, _, _, R, _, "true"), live(F, A, _, _, R, _, "false"), G != F.
better(G, F) :- live(G, A, _, _, R, EG, B), live(F, A, _, _, R, EF, B), G != F, EG > EF.
beaten(F) :- live(G, _, _, _, _, _, _), live(F, _, _, _, _, _, _), better(G, F).
win(F) :- live(F, _, _, _, _, _, _), !beaten(F).

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

% ------------------------------------------------------------------- publish
% Multi-valued lineage: every distinct (attribute, value) pair observed in
% the cluster becomes a golden candidate. No esc/ and no den/ rules exist in
% this file, so nothing can ever be quarantined or denied -- which is exactly
% what "zero-quarantine guaranteed" means here.
pub(A, F) :- live(F, A, _, _, _, _, _).

% M1 (multi-valued lineage) is what publishes here, not R1: there is no
% authority contest in this profile, every distinct observed value simply
% becomes a golden candidate. Tagging the outcome M1 is what lets the
% dashboard's rule counter report M1 instead of leaving it "Idle".
outcome(A, "published", F, "M1") :- pub(A, F).

% The canonical winner is still the loser-set reference so a consumer can see
% which value a single-valued store would have chosen.
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
