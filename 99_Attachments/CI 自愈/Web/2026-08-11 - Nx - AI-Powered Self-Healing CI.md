---
title: "AI-Powered Self-Healing CI"
type: attachment
subtype: web-snapshot
domain: ci-self-healing
status: active
source: "https://nx.dev/docs/features/ci-features/self-healing-ci"
canonical_url: "https://nx.dev/docs/features/ci-features/self-healing-ci"
author: Nx
published:
created: 2026-08-11
retrieved_at: 2026-08-11
description: "Learn how Nx Cloud Self-Healing CI uses AI to automatically detect, analyze, and fix CI failures, eliminating the need to babysit PRs and keeping you focused on building features."
tags:
  - clippings
  - ci-cd
  - ci-self-healing
  - nx
ai_access: true
ai_generated: false
reviewed: false
immutable: true
---
![](https://www.youtube.com/watch?v=aQUlsilNSQ8)

Nx Cloud Self-Healing CI is an **AI-powered system that automatically detects, analyzes, and proposes fixes for CI failures**, offering several key advantages:

- **Improves Time to Green (TTG):** Automatically proposes fixes when tasks fail, significantly reducing the time to get your PR merge-ready. No more babysitting PRs.
- **Keeps You in the Flow:** Get notified about failed PRs and proposed fixes via PR/MR comments or directly in your editor with Nx Console (VS Code, Cursor, or WebStorm). Review, approve, and keep working while AI handles the rest.
- **Leverages Deep Context:** AI agents understand your workspace structure, project relationships, and build configurations through the Nx [project graph](https://nx.dev/docs/features/explore-graph) and metadata.
- **Non-Invasive Integration:** Works with your existing CI provider without overhauling your current setup.

## Enable self-healing CI

To enable Self-Healing CI in your workspace, you'll need to connect to Nx Cloud and configure your CI pipeline.

If you haven't already connected to Nx Cloud, run the following command:

```shell
npx nx@latest connect
```

Next, check the [Nx Cloud workspace settings](https://cloud.nx.app/go/workspace/settings/self-healing-ci?__hstc=221401095.ceb1e5c92e1c8162a8e448b57ab626ef.1785142709557.1785917349596.1786433646920.4&__hssc=221401095.1.1786433646920&__hsfp=9c34502f4ff8e9fb34b0e047e53bb8a1) in the Nx Cloud web application to ensure that "Self-Healing CI" is enabled.

### Configure your CI pipeline

Add a step to your CI configuration's "main" job that runs the `fix-ci` command. It doesn't matter exactly what this job is called, it is whichever job in your config where you invoke `nx start-ci-run` and kick off your `nx run-many` or `nx affected` commands.

By default, your CI provider will only run the step if the previous steps succeeded, but by definition we want to run Self-Healing CI even when previous steps fail. Therefore make sure you are using a condition of `if: always()` or equivalent to ensure it runs even when previous steps fail:

```yaml
name: CI

jobs:
  main:
    runs-on: ubuntu-latest
    steps:
      # Your existing steps which check out the repo, start-ci-run, install
      # dependencies, etc.
      # These are just illustrative examples...
      - uses: actions/checkout@v6
      - uses: actions/setup-node@v6
      - run: npx nx start-ci-run
      - run: npm ci
      - run: npx nx affected -t lint test build

      # NEW: Add this step at the end of your job
      - run: npx nx fix-ci
        if: always() # IMPORTANT: Always run
```

> NOTE: If all tasks succeed then the `fix-ci` command becomes a no-op automatically, so that is why "always" is recommended.

## Configuring self-healing CI

Self-Healing CI configuration is primarily managed through your [Nx Cloud workspace settings](https://cloud.nx.app/go/workspace/settings/self-healing-ci?__hstc=221401095.ceb1e5c92e1c8162a8e448b57ab626ef.1785142709557.1785917349596.1786433646920.4&__hssc=221401095.1.1786433646920&__hsfp=9c34502f4ff8e9fb34b0e047e53bb8a1) in the Nx Cloud dashboard. This provides a centralized, auditable way to control the feature's behavior.

### General settings

| Setting | Description |
| --- | --- |
| **Enable Self-Healing CI** | Enable Self-Healing CI for PRs in the current workspace |
| **GitHub PR comments** | Show Self-Healing CI feedback and actions within GitHub PR comments (in addition to the Nx Cloud UI) |
| **Auto-retry flaky tasks** | Automatically re-run tasks that are detected as flaky to improve CI reliability. This works by pushing an empty commit to the PR branch |
| **Allow public link access** | Allow anyone with access to the link the ability to apply or reject a suggested change (not recommended unless absolutely necessary) |
| **Draft PR handling** | Allow Self-Healing CI to create fixes for draft PRs |
| **Protected branch prefixes** | Configure branch prefixes for which fixes should not be generated (e.g., `release/` will match `release/v1.0`). The default branch and branches named `main`, `master`, `trunk`, `dev`, `stable`, or `canary` will never have fixes generated |

### Eligible tasks

Control which failing tasks Nx Cloud will actively try and fix for pull request CI pipeline executions.

You can choose between two modes:

| Mode | Description |
| --- | --- |
| **Any failing task** | Any task that fails during PR CI pipeline executions is eligible (recommended) |
| **Specific patterns** | Limit Self-Healing CI to failing PR tasks that match the specified glob patterns |

You can also specify **Never fix** patterns to exclude certain tasks from ever being fixed by Self-Healing CI. For example, `*e2e*` would exclude all e2e-related tasks.

### Auto-apply verified code changes

![](https://www.youtube.com/watch?v=30qh5W8zXTY)

Automatically commit code change suggestions to the PR branch **when ALL of these are true**:

1. The task **matches the glob patterns** configured below
2. The AI agent is **highly confident** that the suggested code change will fix the failing task
3. The suggestion has been **explicitly verified** to fix the failing task

#### Deterministic Nx checks

A built-in preset that auto-fixes failures from `nx format:check`, `nx sync:check`, and `nx conformance:check` commands. The AI agent has special knowledge of these commands and will invoke the corresponding "writable" version to fix the issue (e.g., `nx format`). You can safely enable this preset even if you only use a subset of these commands.

#### Additional include patterns

Tasks matching these patterns will also have high-confidence, verified code changes auto-applied. For example: `*build*`, `*test*`, `lint`.

#### Exclude patterns

Tasks matching these patterns will **never** have code changes auto-applied, even if they match the include patterns or presets specified above. For example: `*e2e*`.

## Configuration with SELF\_HEALING.md

Create a `.nx/SELF_HEALING.md` file in your repository to provide project-specific instructions to the Self-Healing CI agent. This file contains freeform markdown that the AI agent reads and interprets naturally.

### Example SELF\_HEALING.md

```markdown
# Self-Healing Configuration

## Confidence Rules

- Fixes involving "test" targets should require high confidence
- Formatting fixes can be applied with medium confidence

## Off-Limits Areas

- \`/src/generated/\` - auto-generated, do not modify
- \`/legacy/\` - requires manual review

## Fix Preferences

- Prefer updating ESLint rules over adding disable comments
- For type errors, prefer explicit types over \`any\`

## Context

See ARCHITECTURE.md for module boundaries.
```

### What to include

| Section | Purpose | Example |
| --- | --- | --- |
| **Confidence Rules** | Override how the AI categorizes failure severity | "Failures in `**/migrations/**` should be classified as `environment_state` " |
| **Off-Limits Areas** | Directories or files the agent should never modify | " `/src/generated/` - auto-generated code" |
| **Fix Preferences** | Guide the agent's approach to common issues | "Prefer updating ESLint rules over adding disable comments" |
| **Predefined Fixes** | Specify deterministic solutions for known failures | "For lint failures, always try running `nx lint --fix` first" |
| **Context** | Reference other documentation the agent should read | "See ARCHITECTURE.md for module boundaries" |

### Using CLAUDE.md

If your repository already has a `CLAUDE.md` file at the root, the Self-Healing CI agent will read it for additional context. When both files exist:

- **SELF\_HEALING.md takes precedence** for any conflicting instructions
- Both files are read, so general context in `CLAUDE.md` is still available
- CI-specific instructions should go in `SELF_HEALING.md`

This allows teams to maintain `CLAUDE.md` for local development workflows while using `SELF_HEALING.md` for CI-specific behavior.

### Viewing configuration status

After a CI run, navigate to the pipeline execution in Nx Cloud and check the **Configurations** tab to see whether `SELF_HEALING.md` was detected and applied.

## Receiving fix notifications

### In your editor

With [Nx Console](https://nx.dev/docs/getting-started/editor-setup) installed, you'll receive notifications directly in VS Code, Cursor, or WebStorm when a fix is available:

![Notification in your editor about an AI fix](https://nx.dev/.netlify/images?url=docs%2F_astro%2Fnotification-self-healing-ci.BlyaGWe1.avif&w=1128&h=537)

### On your pull request/Merge request

Self-Healing CI posts a comment on your PR with:

- A summary of the reasoning behind the fix
- A diff view showing the proposed changes
- Buttons to apply or reject the fix

![Self-Healing CI GitHub Comment](https://nx.dev/.netlify/images?url=docs%2F_astro%2Fself-healing-fix-gh-comment.CpHVEjYK.avif&w=1862&h=1422)

## Applying and reverting fixes

### Applying a fix

You can apply a proposed fix through:

1. **Editor notification** - Click "Apply" in the Nx Console notification
2. **PR/MR comment** - Click the "Apply" button
3. **Nx Cloud UI** - Use the apply button in the diff viewer

### Applying locally for fine-tuning

If a fix is 90% correct but needs minor adjustments:

1. Click "Apply Locally" in the GitHub comment or Nx Cloud UI
2. Run the provided command in your terminal
3. Make your adjustments and commit

![Apply Self-Healing Fixes Locally](https://nx.dev/.netlify/images?url=docs%2F_astro%2Fself-healing-apply-locally.rvmiPIQy.avif&w=1656&h=1040)

### Reverting a fix

If you accidentally applied a fix, you can:

1. Manually revert the Git commit
2. Use the "Revert changes" button in the Nx Cloud diff viewer

## Advanced: CLI overrides

![](https://www.youtube.com/watch?v=KSb48zHbaHg)

### When to use CLI overrides

- **Temporarily disable** Self-Healing CI on a sensitive branch
- **Test configurations** before committing to workspace settings

### Available flags

Pass these flags to `nx start-ci-run`:

| Flag | Description | Example |
| --- | --- | --- |
| `--fix-tasks` | Override which tasks are eligible for fixes | `--fix-tasks="*lint*,*test*"` |
| `--auto-apply-fixes` | Override which tasks can be auto-applied | `--auto-apply-fixes="*lint*"` |

### Disabling features via CLI

Use an empty string to **disable** a feature for a specific CI run:

```shell
# Disable fix generation entirely for this run
npx nx start-ci-run --fix-tasks=""

# Disable auto-apply entirely for this run
npx nx start-ci-run --auto-apply-fixes=""
```

Both flags treat empty string consistently as an explicit "disable" override.

### Pattern syntax

Patterns use glob syntax to match task names:

```shell
# Only fix lint and test tasks
npx nx start-ci-run --fix-tasks="*lint*,*test*"

# Fix everything except e2e tasks
npx nx start-ci-run --fix-tasks="!*e2e*"

# Auto-apply only lint fixes
npx nx start-ci-run --auto-apply-fixes="lint"
```

### Precedence rules

When both workspace settings and CLI overrides are present:

1. **CLI override** (if provided) always takes precedence
2. Falls back to **workspace settings** if no CLI override
3. Empty string (`""`) is an explicit "disable", not "use defaults"

### Viewing applied configuration

After your CI runs, navigate to the CIPE → **Configurations** tab to see:

- What workspace settings were relevant at the time of the CI pipeline execution
- What CLI overrides were applied, if any
- What the final, effective configuration was
