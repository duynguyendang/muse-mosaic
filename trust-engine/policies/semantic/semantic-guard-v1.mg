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
