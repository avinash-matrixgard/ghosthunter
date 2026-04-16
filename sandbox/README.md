# Sandbox — explore Ghosthunter with no cloud account

Runs a **real** Opus investigation against **synthetic billing data**.
Zero cloud credentials required. ~$0.05 in Anthropic API usage.

## Prereqs

- `ANTHROPIC_API_KEY` exported in your shell
- `ghosthunter` on your PATH (the symlink we set up, or a venv activated)

## Run it

From the repo root:

```bash
ghosthunter investigate sandbox/aws-sandbox-billing.csv
```

Ghosthunter will detect three correlated spikes:
- **EC2 - Other** +640% (NAT Gateway)
- **AWS Lambda** +1515%
- **Amazon CloudWatch** +181%

Pick the biggest one (`0`) when prompted, and let Opus drive.

## What to do when Opus asks for command output

Open `sandbox/SCRIPTED_OUTPUTS.md` in another window. For each
`aws ...` command Opus proposes, find the matching entry and paste its
JSON block back into the Ghosthunter prompt. If Opus asks for something
not in the file, make up a plausible output or use `/note` / `/skip`.

The scenario is baked in: a Lambda deployed on 2026-02-19 reads big S3
objects cross-region with no VPC endpoint, with DEBUG logging. Opus
should piece this together across 4-6 commands.

## Investigate the other spikes too

Each one is self-contained — rerun with `--spike 1` for Lambda,
`--spike 2` for CloudWatch. They should all trace back to the same
root cause, which is half the fun.

## Files in this directory

- `aws-sandbox-billing.csv` — the synthetic billing data
- `SCRIPTED_OUTPUTS.md` — paste-backs for the commands Opus will propose
- `README.md` — this file
