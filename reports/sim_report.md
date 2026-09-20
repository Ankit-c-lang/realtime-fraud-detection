# Simulator validation report

Generated 2026-09-20 12:14 UTC by `python -m fraud.sim.checks`.
Every number below comes from that command (PLAN §0.4).

## Run

| | |
|---|---|
| Config | `configs/sim.yaml` |
| **sha256** | `97404e57e5da143b1f1b47c11caac96d315ce5fe85da1f47feba5cda9976863f` |
| Seed | 42 |
| Generator version | 1 |
| Events | 494,156 |
| Accounts | 22,470 |
| Merchants | 2,000 |
| Fraud rate | 1.687% |
| Attacks | 275 across 45 rings |
| Generation time | 74.33 s |

The sha256 above is what PLAN §4.8's freeze rule pins. It is recorded in every model's
metadata, and metrics are never improved by editing the config behind it.

## Checks

**All checks passed.**

| Result | Check | Plan | Detail |
|---|---|---|---|
| PASS | events.parquet has exactly the §4.2 columns | §4.2 | `txn_id, event_time, account_id, merchant_id, merchant_category, amount, channel, device_id, ip, lat, lon, city, country, status` |
| PASS | no label column leaks into events | §4.2, invariant 4 | labels live only in labels.parquet, keyed by txn_id |
| PASS | accounts and merchants match §4.2 | §4.2 | 22,470 accounts, 2,000 merchants |
| PASS | txn_id is unique | §4.8 | 494,156 events |
| PASS | events are sorted by event_time | §4.8 | 2026-01-01 00:00:39 to 2026-03-31 23:59:50 |
| PASS | no event precedes its account's creation | §4.8 | 0 violations |
| PASS | one label row per event | §4.6 | 494,156 label rows |
| PASS | fraud prevalence within [1.0%, 2.0%] | §4.8 | 1.687% |
| PASS | total volume within tolerance of target | §4.3 | 494,156 vs target 500,000 (-1.17%) |
| PASS | VELOCITY within ±30% of target | §4.8 | 1,174 vs 1,500 (-21.7%) |
| PASS | ATO within ±30% of target | §4.8 | 668 vs 600 (+11.3%) |
| PASS | CARD_TESTING within ±30% of target | §4.8 | 2,210 vs 2,300 (-3.9%) |
| PASS | RING within ±30% of target | §4.8 | 4,282 vs 4,250 (+0.8%) |
| PASS | every card-testing device sees >= 20 accounts in 60 min | §4.8 | worst attack reaches 20 accounts across 50 attacks |
| PASS | ATO: >= 90% of attacks exceed 900 km/h at the first fraud event | §4.8 | 100.0% of 120 attacks; median 6,226 km/h |
| PASS | >= 12 rings start inside the test window | §4.5 | 12 of 45 rings |
| PASS | >= 4 test rings reuse a device | §4.5 | 4 test rings; 18 rings reuse overall (40%, configured 30%) |
| PASS | >= 20 rings start and finish inside training | §4.5 | 22 rings |
| PASS | ring sizes stay within the configured range | §4.5 | 6-15 accounts per ring (configured 6-15) |
| PASS | ring approval rate is indistinguishable from ordinary traffic | §4.5 | 1.45% declined vs 1.50% for legitimate traffic |
| PASS | VPN use produces legitimate impossible travel | §4.4 | 419 events from 212 accounts (~449 expected to use a VPN) |
| PASS | households share a device | §4.4 | 573 shared devices (~599 households expected) |
| PASS | new legitimate accounts transact | §4.4 | 2,007 of 2,670 mid-simulation accounts are active |
| PASS | legitimate micro-payment bursts exist | §4.4 | 650 accounts show a burst (~674 configured) |
| PASS | low-friction merchants have honest customers | §4.4 | small digital purchases occur outside card-testing attacks |
| PASS | same seed reproduces identical files (sim_tiny) | §4.8 | events sha256 `b00053cf8d34cb49…` |

## Fraud by pattern

| Pattern | Produced | Target | Drift |
|---|---|---|---|
| ATO | 668 | 600 | +11.3% |
| CARD_TESTING | 2,210 | 2,300 | -3.9% |
| RING | 4,282 | 4,250 | +0.8% |
| VELOCITY | 1,174 | 1,500 | -21.7% |

## Example attacks

### VELOCITY — `VEL0026`

24 transactions, 1 account(s), 1 device(s), spanning 7 minutes.

| event_time | account_id | merchant_category | amount | city | status |
|---|---|---|---|---|---|
| 2026-02-20 17:39:29 | A0000357 | digital_goods | 2706.47 | Kanpur | APPROVED |
| 2026-02-20 17:39:44 | A0000357 | digital_goods | 1407.53 | Kanpur | APPROVED |
| 2026-02-20 17:39:47 | A0000357 | digital_goods | 3180.67 | Kanpur | APPROVED |
| 2026-02-20 17:39:50 | A0000357 | digital_goods | 5192.72 | Kanpur | DECLINED |
| 2026-02-20 17:40:15 | A0000357 | digital_goods | 8618.87 | Kanpur | DECLINED |
| 2026-02-20 17:40:19 | A0000357 | digital_goods | 2654.76 | Kanpur | APPROVED |

_(first 6 of 24)_

### ATO — `ATO0048`

6 transactions, 1 account(s), 1 device(s), spanning 154 minutes.

| event_time | account_id | merchant_category | amount | city | status |
|---|---|---|---|---|---|
| 2026-02-14 19:29:28.413530 | A0002516 | electronics | 24378.16 | Kuala Lumpur | APPROVED |
| 2026-02-14 19:56:59.413530 | A0002516 | travel | 15993.72 | Kuala Lumpur | APPROVED |
| 2026-02-14 21:02:17.413530 | A0002516 | gift_cards_wallet | 44217.28 | Kuala Lumpur | DECLINED |
| 2026-02-14 21:46:25.413530 | A0002516 | gift_cards_wallet | 33142.97 | Kuala Lumpur | APPROVED |
| 2026-02-14 21:51:20.413530 | A0002516 | electronics | 42875.17 | Kuala Lumpur | APPROVED |
| 2026-02-14 22:03:14.413530 | A0002516 | travel | 39596.74 | Kuala Lumpur | APPROVED |


### CARD_TESTING — `CT0038`

61 transactions, 55 account(s), 1 device(s), spanning 621 minutes.

| event_time | account_id | merchant_category | amount | city | status |
|---|---|---|---|---|---|
| 2026-02-11 04:34:53 | A0003212 | digital_goods | 19.87 | London | DECLINED |
| 2026-02-11 04:36:03 | A0011440 | digital_goods | 46.14 | London | DECLINED |
| 2026-02-11 04:36:08 | A0007060 | digital_goods | 30.81 | London | DECLINED |
| 2026-02-11 04:38:03 | A0015872 | digital_goods | 21.99 | London | DECLINED |
| 2026-02-11 04:38:07 | A0014325 | digital_goods | 13.6 | London | DECLINED |
| 2026-02-11 04:38:34 | A0015956 | digital_goods | 31.3 | London | APPROVED |

_(first 6 of 61)_

### RING — `RING0025`

123 transactions, 14 account(s), 4 device(s), spanning 30,624 minutes.

| event_time | account_id | merchant_category | amount | city | status |
|---|---|---|---|---|---|
| 2026-02-08 14:52:30 | A0022267 | digital_goods | 200.36 | Ahmedabad | APPROVED |
| 2026-02-08 15:04:53 | A0022267 | digital_goods | 168.57 | Ahmedabad | APPROVED |
| 2026-02-08 18:34:52 | A0022266 | digital_goods | 825.57 | Ahmedabad | APPROVED |
| 2026-02-08 19:44:53 | A0022258 | digital_goods | 219.02 | Ahmedabad | APPROVED |
| 2026-02-08 22:04:31 | A0022258 | pharmacy | 261.86 | Ahmedabad | APPROVED |
| 2026-02-09 11:38:59 | A0022262 | restaurants | 416.87 | Ahmedabad | APPROVED |

_(first 6 of 123)_
