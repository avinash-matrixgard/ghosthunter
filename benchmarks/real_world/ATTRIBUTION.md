# Attribution

The file `focus_sample.csv` in this directory is taken verbatim from:

**FOCUS Sample Data** — [FinOps Foundation](https://www.finops.org/) /
[FOCUS project](https://focus.finops.org/)

- **Upstream repository:** https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data
- **Upstream path:** `FOCUS-1.0/focus_sample.csv`
- **License:** [Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/)
- **Fetched:** 2026-04-17

## What it contains

Anonymized real-world billing data from multiple cloud providers
(AWS, Google Cloud, Microsoft Azure, Oracle Cloud Infrastructure),
formatted according to the FOCUS 1.0 specification. The data has been
de-identified by the FinOps Foundation — see the upstream repository's
`license.md` and `FOCUS-1.0/README.md` for methodology.

## Why we include it

To validate Ghosthunter's FOCUS 1.0 parser against schemas the
maintainers didn't shape themselves — see
[`tests/test_focus_sandbox.py`](../../tests/test_focus_sandbox.py).
Synthetic fixtures in [`benchmarks/spikes/`](../spikes/) prove the
parser works on data we shape; this file proves it also works on
real anonymized cross-cloud data.

## Usage and redistribution

Per CC BY 4.0 you may copy, redistribute, and adapt this data as long
as you provide attribution. Attribution to the FinOps Foundation is
preserved here and must be preserved in any downstream fork.
