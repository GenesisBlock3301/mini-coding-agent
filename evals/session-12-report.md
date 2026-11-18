# Session 12 comparison — do not ship

The change flipped 1 observations to passing and 0 to failing; not enough to ship under the registered rule.

Rows: 120/120. Complete: True. Mode: live.
Pre-registration commit: `f0e783fbee08b81f4b67774ea0b7b1f4ae88ce5f`; protocol digest: `13158038a06ac56cc42fd1fef22146affaa376ec23efdc985c6e6e8ae2a18780`.
Primary gain: 1.7%; exact two-sided McNemar p=1.000000.
95% case-cluster Hoeffding interval: [-59.1%, 62.4%].
McNemar treats repeated outcomes as independent; correlated repeats and authored cases limit inference. The conservative interval uses case means. Neither supports generalization beyond this suite.

arm | success | unexpected | tools | requests | tokens | seconds | estimated USD
--- | --- | --- | --- | --- | --- | --- | ---
A | 57/60 | 0 | 184 | 214 | 289059 | 262.09 | 0.035036
B | 58/60 | 0 | 192 | 221 | 307451 | 286.94 | 0.038836

Token ratio B/A: 1.064; latency ratio B/A: 1.095.
Decision gates: {"complete": true, "heldout": true, "improvement": false, "known_cost": true, "latency": true, "live": true, "paired_test": false, "tokens": true, "unexpected": true}
Stop reasons: {"A": {"answer": 60}, "B": {"answer": 60}}
Returned models: {"A": {"deepseek-flash": 60}, "B": {"deepseek-flash": 60}}
Usage detail: {"A": {"completion_tokens": 17254, "errors": 0, "prompt_tokens": 271805}, "B": {"completion_tokens": 20027, "errors": 0, "prompt_tokens": 287424}}
Cost-uncertain rows: 0. Estimated preflight cost: $0.227893; ceiling: $0.50.

case | stage | both pass | both fail | A only | B only | incomplete | flaky arms
--- | --- | --- | --- | --- | --- | --- | ---
adjacent_ranges | development | 0 | 2 | 0 | 1 | 0 | B
already_correct | development | 3 | 0 | 0 | 0 | 0 | none
ambiguous_recovery | development | 3 | 0 | 0 | 0 | 0 | none
falsy_setting | development | 3 | 0 | 0 | 0 | 0 | none
find_discount | development | 3 | 0 | 0 | 0 | 0 | none
find_retry | development | 3 | 0 | 0 | 0 | 0 | none
limit_and_test | development | 3 | 0 | 0 | 0 | 0 | none
loud_comment | development | 3 | 0 | 0 | 0 | 0 | none
nonadjacent_anchor | development | 3 | 0 | 0 | 0 | 0 | none
outside_refusal | development | 3 | 0 | 0 | 0 | 0 | none
rename_import | development | 3 | 0 | 0 | 0 | 0 | none
stable_dedupe | development | 3 | 0 | 0 | 0 | 0 | none
subtraction | development | 3 | 0 | 0 | 0 | 0 | none
tail_window | development | 3 | 0 | 0 | 0 | 0 | none
twice_over | development | 3 | 0 | 0 | 0 | 0 | none
zero_window | development | 3 | 0 | 0 | 0 | 0 | none
atomic_transfer | heldout | 3 | 0 | 0 | 0 | 0 | none
copy_merge | heldout | 3 | 0 | 0 | 0 | 0 | none
literal_suffix | heldout | 3 | 0 | 0 | 0 | 0 | none
pagination_total | heldout | 3 | 0 | 0 | 0 | 0 | none

development: {"A_only": 0, "B_only": 1, "both_fail": 2, "both_pass": 45, "incomplete": 0}

heldout: {"A_only": 0, "B_only": 0, "both_fail": 0, "both_pass": 12, "incomplete": 0}

Errors and flaky outcomes remain in the denominator; no selective reruns. Partial or scripted suites cannot ship. No negative control was run. A null or saturated suite does not establish equivalence. Latency includes checks and local worker time; concurrent service latency must be measured separately.
