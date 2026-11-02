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
