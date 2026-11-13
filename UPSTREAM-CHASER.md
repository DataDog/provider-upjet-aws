# Upstream sync (GitLab CI)

This CI-only `gitlab` branch runs upstream-chaser against the DataDog fork. The
config in `upstream-chaser.yaml` initially selects only upstream `v2.4.*` tags;
`syncHead: true` **also fast-forwards `main` to upstream's current default-branch
HEAD**, independently of the selected release tags. It never force-pushes a
diverged `main`.

## Before running

- Create a `dd-octo-sts` policy for `DataDog/provider-upjet-aws` that grants
  `contents: write` (and GitHub Releases access). Confirm the bot can push to
  the fork's protected `main` under its GitHub rulesets; otherwise the HEAD
  sync will fail or be skipped. Store the policy name as the GitLab CI variable
  `DDOCTOSTS_POLICY`, and set `GITHUB_ACTOR` and `GITHUB_EMAIL` for the bot.
- Run a GitLab **web** pipeline on the `gitlab` branch with the variable
  `UPSTREAM_CHASER_DRY_RUN=true`, then start its manual `sync_upstream_chaser`
  job. Check the reported tag/branch results before running without dry-run.
- Once validated, create a GitLab pipeline schedule on `gitlab` with
  `TARGET_JOB=sync-upstream-chaser`. A non-dry-run push creates branches like
  `v2.4.0-dd` and tags like `v2.4.0-dd.1`; `release: true` also creates a GitHub
  release. Confirm GitLab receives the pushed tags so `build_provider` can run.

`files/.gitlab-ci.yml` is overlaid on each generated tag branch because the
upstream tree does not contain the CI file from this separate `gitlab` branch.
Keep it identical to `.gitlab-ci.yml` whenever changing CI; the latter runs
the sync job, while the overlaid copy runs the tag-triggered provider build.
