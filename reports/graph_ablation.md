# Graph ablation — E3 vs E2 (PLAN §7.9)

Generated from `reports/experiments/E2.json`, `E3.json` and `E3b.json`. All figures are
on **valid**; the test split has not been read.

E3 uses E2's parameter set unchanged, so any difference is the graph and not a luckier
hyperparameter draw.

## The headline

| Exp | Features | PR-AUC | Precision | Recall | F1 | FPR | Alerts | VDR | VEL | ATO | CT | RING |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| E1 rules | — | 0.4181 | 0.622 | 0.651 | 0.636 | 0.0054 | 1.40% | 0.289 | 0.84 | 0.18 | 0.80 | 0.48 |
| E2 XGBoost | 30 hot | 0.9973 | 0.947 | 0.988 | 0.967 | 0.0007 | 1.39% | 0.993 | 0.94 | 1.00 | 1.00 | **1.00** |
| E3 XGBoost | 36 (hot + graph) | 0.9973 | 0.946 | 0.986 | 0.966 | 0.0008 | 1.39% | 0.991 | 0.93 | 1.00 | 1.00 | **1.00** |
| E3b XGBoost | 6 graph only | 0.3114 | **1.000** | 0.296 | 0.456 | 0.0000 | 0.39% | 0.199 | 0.00 | 0.00 | 0.00 | **1.00** |

**The graph adds nothing measurable on top of the behavioural features.** E3 matches E2 to
four decimal places on PR-AUC and is a hair lower on recall. Ring recall was already 1.00
at E2, so there was no headroom for it to claim.

This was predicted before the experiment ran, and it is worth being precise about why: it
is a property of this dataset, not a finding about graph features. The simulator's rings
share devices *and* differ behaviourally — young accounts, colluding merchants — so the
behavioural features alone already separate them.

## What the ablation does show

Two results make the graph layer's value measurable despite the saturated recall.

**1. The model prefers the graph features when it has them.**

| Graph feature | Rank of 36 | Importance |
|---|---|---|
| `community_shared_devices` | **1** | 0.4694 |
| `graph_clustering` | 11 | 0.0109 |
| `community_size` | 13 | — |
| `community_young_share` | 17 | — |
| `graph_degree` | 23 | — |
| `ppr_risk` | 30 | — |

The six graph features carry **50.1% of total importance**, and
`community_shared_devices` is the single most important feature in the model, ahead of
every behavioural one. Given free choice the model routes ring detection through the
graph and leaves behaviour to the other three patterns.

**2. The graph features alone are a precise, pure ring detector.**

E3b ablates every behavioural feature and keeps only the six graph ones. It catches
**100% of rings at 100% precision** and **0% of velocity, ATO and card testing**. It
alerts on 0.39% of transactions with a false-positive rate of 0.0000.

That is exactly the designed behaviour. The graph carries ring structure and nothing
else, which is why it was worth building and why it belongs on the "what does this add?"
list even though it does not move the headline number here.

## Honest conclusion for the README

On this dataset the graph features are **redundant, not useless**. They duplicate ring
signal the behavioural features already capture, so removing them costs nothing
measurable. They are also the cleanest available expression of that signal: the model
gives them half its importance, and on their own they identify every ring without a
single false positive.

The reason the ablation cannot show a recall gain is that E2 already reaches 1.00 on
rings. PLAN §4.8 permits one simulator revision and it has been spent (`sim-v2`,
reasoning in `sim_realism_review.md`), so this is where the evidence stops. A dataset
where rings were behaviourally ordinary — which is what §4.5 describes and what a real
mule network looks like — is the condition under which this ablation would separate the
two models, and that is the claim the README should make rather than a recall delta it
cannot support.
