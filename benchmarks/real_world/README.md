# benchmarks/real_world/

Real-world (anonymized) cloud billing data that Ghosthunter's parser is
validated against. Complements the synthetic fixtures in
[`../spikes/`](../spikes/).

## What's committed

- **`focus_sample.csv`** (~740 KB) — 1,000-row FOCUS 1.0 sample from
  the FinOps Foundation. CC BY 4.0. See
  [`ATTRIBUTION.md`](./ATTRIBUTION.md) for full provenance.

This file is enough to run the skippable test in
[`tests/test_focus_sandbox.py`](../../tests/test_focus_sandbox.py) and
to prove the parser handles real cross-cloud schemas cold.

## What's NOT committed (and how to fetch)

The FinOps Foundation publishes two larger variants of the sample. They
are too large to bloat every Ghosthunter clone, so we `.gitignore` them
and fetch on demand:

| File | Size | Use |
|------|-----:|-----|
| `focus_sample_10000.csv` | ~7 MB | Broader parser smoke |
| `focus_sample_100000.csv` | ~73 MB | Perf stress (100K rows) |

To fetch locally:

```bash
cd benchmarks/real_world
curl -LO https://raw.githubusercontent.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data/main/FOCUS-1.0/focus_sample_10000.csv
curl -LO https://raw.githubusercontent.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data/main/FOCUS-1.0/focus_sample_100000.csv
```

The tests in `tests/test_focus_sandbox.py` use `@pytest.mark.skipif` so
they skip cleanly if these files aren't present — contributors don't
need to fetch them just to get `pytest` green.

## What must NEVER land here

Customer billing exports. Any file that came from a user's real cloud
account — GCP Console export, AWS CUR, Azure Cost Management export —
belongs in the `bills/` directory, which is `.gitignore`d at the repo
root. If you want to add a new safe fixture here, it must be:

1. Explicitly licensed for redistribution (CC BY, MIT, CC0, etc.), AND
2. Anonymized by the upstream source — not by you after-the-fact, AND
3. Small enough to stay in the repo without bloat (≤ 1 MB).

Add an `ATTRIBUTION.md` entry for every new fixture.
