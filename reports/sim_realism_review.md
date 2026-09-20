# Simulator realism review — E2 triggers the §4.8 threshold

`make experiments` on 2026-09-20 produced a validation PR-AUC of **0.9995** for E2, above
the 0.995 line PLAN §4.8 calls "suspiciously perfect". That section allows exactly one
simulator revision (`sim-v2`) in this situation. **Nothing has been changed.** This file
is the evidence for the decision.

## The numbers

| | PR-AUC | Precision | Recall | F1 | FPR | Alert rate | VDR | VEL | ATO | CT | RING |
|---|---|---|---|---|---|---|---|---|---|---|---|
| E1 rules | 0.6305 | 0.928 | 0.673 | 0.780 | 0.0008 | 1.04% | 0.272 | 0.84 | 0.18 | 0.80 | 0.48 |
| E2 XGBoost | **0.9995** | 0.953 | 0.996 | 0.974 | 0.0007 | 1.50% | 0.997 | 0.99 | 1.00 | 1.00 | 1.00 |

Every pattern is caught at essentially 100%. All 20 search trials scored between 0.9979
and 0.9995, so the result is not a lucky hyperparameter draw: the task itself is trivial.

## This is not leakage

Checked before concluding:

- **No single feature separates.** The best is `dev_accts_30d` at 0.53 PR-AUC against a
  1.43% base rate. The model is combining signals, not reading an answer key.
- No identifier reaches the model: `feature_matrix` selects by the spec (L8), and
  `test_splits.py` proves an added `attack_id` column is dropped.
- Features use only events strictly before `t` (L1), enforced by the half-open windows in
  `test_features_windows.py`.
- The label file is separate and joined only by evaluation code (L4).

## It is that the hard negatives never reach the fraud region

Measured on `valid`, 65,604 legitimate rows:

| Fraud shape | Legitimate rows that look like it |
|---|---|
| `dev_accts_30d >= 5` (ring: accounts sharing a device) | **0 of 65,604** |
| `geo_speed_kmh >= 900 AND new_device` (ATO) | **0 of 65,604** |
| `acct_cnt_5m >= 5` (velocity) | **1 of 65,604** |

Rings hit `dev_accts_30d >= 5` on 28.9% of their rows. No legitimate account ever shares a
device with five others, so the feature is close to a perfect ring detector on its own.

The hard negatives exist and are individually correct — they were all verified in
`reports/sim_report.md` — but each one stops short of the region the matching fraud
pattern occupies:

1. **Device sharing is capped too low.** Households are 2-4 accounts
   (`devices.family_size_max: 4`), while a ring puts 6-15 accounts on 2-4 devices. There
   is no legitimate population in the 5+ band at all. Carrier NAT creates shared *IPs*,
   not shared devices, so it does not fill the gap.
2. **Travel never coincides with a new device.** Trips enforce realistic gaps (>= 2 h
   domestic, >= 6 h international), so legitimate `geo_speed_kmh` stays low, and a
   traveller always keeps their own phone. A real traveller sometimes buys a local
   handset, which is exactly the ATO shape.
3. **VPN use is too tidy.** It produces a foreign country on the account's own device,
   whereas ATO is foreign *and* a new device. The two never overlap.
4. **Shopping sprees are too slow.** 4-8 extra transactions spread over 90 minutes rarely
   reach `acct_cnt_5m >= 5`, while fraud velocity is 10-30 within 3-15 minutes.

## What a sim-v2 would change

Config-only, no structural rewrite, and all inside the existing knobs:

- raise `devices.family_size_max` and add a small population of genuinely
  wide-shared devices (shared household tablets, second-hand handsets, small-business
  point-of-sale phones) so the 5-15 band is legitimately occupied;
- let a fraction of trips involve a device new to the account;
- let VPN sessions sometimes come from a new device;
- tighten a fraction of shopping sprees into a 5-10 minute burst.

Each one makes a hard negative overlap the pattern it is meant to imitate, which is what
§4.4 intends. The expected effect is a lower, more defensible PR-AUC and a much more
interesting graph ablation in Phase 4, because ring detection would then have to rely on
structure rather than on a device count no legitimate account ever reaches.

## The cost of doing it

- `sim-v2` is the **only** revision §4.8 permits. Spending it now means the simulator is
  fixed for the rest of the project.
- Everything downstream regenerates: `make data`, `make features`, then E1 and E2 again.
  About 4 minutes of compute, plus re-running the Phase 1 checks.
- The reason must be written into the README, which this file provides.

## The cost of not doing it

- A README reporting 0.9995 PR-AUC invites exactly one question in an interview, and the
  honest answer is that the synthetic negatives were too easy.
- The Phase 4 graph ablation (E3 vs E2) is the project's headline differentiator, and it
  cannot show anything: E2 already catches 100% of rings, so there is no headroom for
  graph features to demonstrate value.

That second point is the substantive one. The ablation is the reason the graph layer
exists, and at E2 = 1.00 ring recall there is nothing left for it to prove.
