# Results: simple mode (baseline) vs smart mode

Run on 2026-11-17 17:33, userspace switch.

## 1. Traffic jam: alert latency (ms)

| Mode | Jam | Alerts arrived | Mean | 95th pct | Max | Host slowed down |
|---|---|---|---|---|---|---|
| simple | none | 63 of 63 | 0.5 | 0.8 | 1.4 | no |
| simple | heavy | 59 of 59 | 327.6 | 610.6 | 1611.5 | no |
| smart | none | 64 of 64 | 0.4 | 0.8 | 2.2 | no |
| smart | heavy | 68 of 68 | 0.3 | 0.6 | 0.8 | yes |

## 2. Server failure: time until DOWN (s)

| Mode | What happened | Mean | Max | Missed |
|---|---|---|---|---|
| simple | stop agent | 2.05 | 2.06 | 0 |
| simple | cut cable | 3.08 | 3.09 | 0 |
| smart | stop agent | 2.18 | 2.55 | 0 |
| smart | cut cable | 3.08 | 3.10 | 0 |

## 3. Packet loss: alerts delivered

| Mode | Loss | Alerts arrived | Delivered | First try only | Reports lost |
|---|---|---|---|---|---|
| simple | 0% | 98 of 98 | 100.0% | 100.0% | 0.0% |
| simple | 5% | 102 of 102 | 100.0% | 89.2% | 2.1% |
| simple | 10% | 99 of 99 | 100.0% | 87.9% | 11.3% |
| simple | 20% | 98 of 98 | 100.0% | 69.4% | 18.3% |
| smart | 0% | 105 of 105 | 100.0% | 100.0% | 0.0% |
| smart | 5% | 99 of 99 | 100.0% | 92.9% | 5.1% |
| smart | 10% | 100 of 100 | 100.0% | 86.0% | 7.2% |
| smart | 20% | 100 of 100 | 100.0% | 77.0% | 20.4% |

## 4. More servers

| Mode | Agents | UP | Reports | Lost | Latency mean (ms) | Monitor CPU |
|---|---|---|---|---|---|---|
| simple | 4 | 4 | 100 | 0.00% | 0.48 | 0.3% |
| simple | 10 | 10 | 250 | 0.00% | 0.50 | 0.4% |
| simple | 20 | 20 | 486 | 0.00% | 0.51 | 0.5% |
| simple | 50 | 50 | 1250 | 0.00% | 0.53 | 0.8% |
| smart | 4 | 4 | 100 | 0.00% | 0.46 | 0.3% |
| smart | 10 | 10 | 250 | 0.00% | 0.53 | 0.4% |
| smart | 20 | 20 | 487 | 0.00% | 0.51 | 0.5% |
| smart | 50 | 50 | 1250 | 0.00% | 0.49 | 0.8% |

## 5. Flooding host

| Mode | Noticed after (s) | Blocked after (s) | Junk packets at monitor |
|---|---|---|---|
| simple | 0.56 | never | 4500 of ~4500 |
| smart | 1.04 | 1.04 | 300 of ~4500 |
