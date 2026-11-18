# Session 12 comparison — do not ship

The change flipped 2 observations to passing and 0 to failing; not enough to ship under the registered rule.

Rows: 26/120. Complete: False. Mode: live.
Pre-registration commit: `f0e783fbee08b81f4b67774ea0b7b1f4ae88ce5f`; protocol digest: `13158038a06ac56cc42fd1fef22146affaa376ec23efdc985c6e6e8ae2a18780`.
Primary gain: 15.4%; exact two-sided McNemar p=0.500000.
95% case-cluster Hoeffding interval: [-100.0%, 100.0%].
McNemar treats repeated outcomes as independent; correlated repeats and authored cases limit inference. The conservative interval uses case means. Neither supports generalization beyond this suite.

arm | success | unexpected | tools | requests | tokens | seconds | estimated USD
--- | --- | --- | --- | --- | --- | --- | ---
A | 10/13 | 0 | 31 | 43 | 56830 | 54.05 | 0.006849
B | 12/13 | 0 | 34 | 46 | 63555 | 56.80 | 0.008540

Token ratio B/A: 1.118; latency ratio B/A: 1.051.
Decision gates: {"complete": false, "heldout": true, "improvement": true, "known_cost": true, "latency": true, "live": true, "paired_test": false, "tokens": true, "unexpected": true}
Stop reasons: {"A": {"answer": 13}, "B": {"answer": 13}}
Returned models: {"A": {"deepseek-flash": 13}, "B": {"deepseek-flash": 13}}
Usage detail: {"A": {"completion_tokens": 3468, "errors": 0, "prompt_tokens": 53362}, "B": {"completion_tokens": 4318, "errors": 0, "prompt_tokens": 59237}}
Cost-uncertain rows: 0. Estimated preflight cost: $0.227893; ceiling: $0.50.

case | stage | both pass | both fail | A only | B only | incomplete | flaky arms
--- | --- | --- | --- | --- | --- | --- | ---
adjacent_ranges | development | 0 | 1 | 0 | 2 | 0 | B
already_correct | development | 3 | 0 | 0 | 0 | 0 | none
ambiguous_recovery | development | 3 | 0 | 0 | 0 | 0 | none
falsy_setting | development | 3 | 0 | 0 | 0 | 0 | none
find_discount | development | 1 | 0 | 0 | 0 | 0 | none
find_retry | development | 0 | 0 | 0 | 0 | 0 | none
limit_and_test | development | 0 | 0 | 0 | 0 | 0 | none
loud_comment | development | 0 | 0 | 0 | 0 | 0 | none
nonadjacent_anchor | development | 0 | 0 | 0 | 0 | 0 | none
outside_refusal | development | 0 | 0 | 0 | 0 | 0 | none
rename_import | development | 0 | 0 | 0 | 0 | 0 | none
stable_dedupe | development | 0 | 0 | 0 | 0 | 0 | none
subtraction | development | 0 | 0 | 0 | 0 | 0 | none
tail_window | development | 0 | 0 | 0 | 0 | 0 | none
twice_over | development | 0 | 0 | 0 | 0 | 0 | none
zero_window | development | 0 | 0 | 0 | 0 | 0 | none
atomic_transfer | heldout | 0 | 0 | 0 | 0 | 0 | none
copy_merge | heldout | 0 | 0 | 0 | 0 | 0 | none
literal_suffix | heldout | 0 | 0 | 0 | 0 | 0 | none
pagination_total | heldout | 0 | 0 | 0 | 0 | 0 | none

development: {"A_only": 0, "B_only": 2, "both_fail": 1, "both_pass": 10, "incomplete": 0}

heldout: {"A_only": 0, "B_only": 0, "both_fail": 0, "both_pass": 0, "incomplete": 0}

Errors and flaky outcomes remain in the denominator; no selective reruns. Partial or scripted suites cannot ship. No negative control was run. A null or saturated suite does not establish equivalence. Latency includes checks and local worker time; concurrent service latency must be measured separately.
