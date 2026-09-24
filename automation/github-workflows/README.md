# GitHub Actions workflows

Copy these into `.github/workflows/` (or push the local commit that already adds them) using a token with the `workflow` scope:

- `paper-runner.yml` — every 15 minutes + manual
- `paper-scanner.yml` — daily 06:00 UTC + manual

Local commit `da9a5b4` already contains the real `.github/workflows/` files; `git push` once `gh auth refresh -s workflow` succeeds.
