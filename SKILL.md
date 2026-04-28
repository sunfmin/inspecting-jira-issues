---
name: inspecting-jira-issues
description: Use when starting work on a Jira ticket and you need full context — not just summary, but attachments (screenshots, mockups), comments (where repros usually live), linked issues, and custom fields. Use when `acli jira workitem view` returns a description that's just a media reference like `image-20260427-021811.png` and you can't see what the bug is. Use when an AI agent is about to guess at a bug from the summary or about to ask the user to paste a screenshot it could fetch itself. Use when authenticated curl against the Jira REST API returns 401, 403, or 406.
---

# Inspecting Jira Issues for AI Agents

## Overview

A Jira summary is the worst way to understand a bug. The actual repro almost always lives in **attachments**, **comments**, or **linked issues** — three places `acli jira workitem view` does not show by default. An AI agent that "works from the summary" guesses at half the problem.

This skill is the standard recipe for reading a Jira ticket completely from the command line, and for downloading attachment bytes that the agent can actually view (the non-obvious part — `acli` has no `attachment download` subcommand).

## When to Use

- You just opened a Jira URL and are about to start fixing
- The description is `image-20260427-021811.png` with no text
- An agent says "I'll work from the summary" or "can you paste the screenshot"
- `curl` returns 401/403/406 against the Jira REST API

**Don't use** for sparse tickets with no attachments and no linked issues — `acli jira workitem view KEY` is enough.

## Inspection workflow

```bash
KEY=KGM-3320

# 1. Full JSON — description, status, comments, attachments, issuelinks, custom fields
acli jira workitem view "$KEY" --fields '*all' --json > /tmp/$KEY.json

# 2. Attachments
acli jira workitem attachment list --key "$KEY" --json
./download-jira-attachment.sh 188469 /tmp/$KEY.png      # repeat per attachment

# 3. Read every downloaded image with the agent's image-reading tool

# 4. Linked issues — duplicates, blockers, related
jq '.fields.issuelinks[] | {type: .type.name, key: (.outwardIssue.key // .inwardIssue.key)}' /tmp/$KEY.json

# 5. Comments — repros and decisions live here
acli jira workitem comment list --key "$KEY"
```

## Where things hide

| You want | It is actually in |
|----------|-------------------|
| Bug repro steps | A comment, not the description |
| The screenshot showing the bug | An attachment |
| Why this ticket matters / customer impact | Custom fields (criticality, urgency) — fetch with `--fields '*all'` |
| Whether the fix already shipped elsewhere | A comment on the parent epic, or a linked "is duplicated by" |
| The actual symptom | The screenshot, never the summary |

## Downloading attachments — the non-obvious parts

`acli` has no `attachment download`. Naive `curl` against `*.atlassian.net/rest/api/3/attachment/content/<id>` returns **403**. The four pitfalls every agent hits:

| Pitfall | Workaround |
|---------|-----------|
| Where's the OAuth token? | macOS Keychain (`security find-generic-password -s acli`), **not** `~/.config/acli/`. Stored as `go-keyring-base64:<base64(gzip(JSON))>`. |
| Which URL do I hit? | `https://api.atlassian.com/ex/jira/<cloudId>/rest/api/3/attachment/content/<id>` — the OAuth proxy. The site URL rejects acli's tokens. |
| Where's the cloudId? | The keychain account name is literally `oauth:<cloudId>:<userId>`. No need to call `getAccessibleAtlassianResources`. |
| Why does my request 406? | `Accept: image/png` triggers 406 ("Acceptable representations: [application/json]"). Use `Accept: */*` (or omit `Accept`). |

`download-jira-attachment.sh` in this skill's directory bundles all four. Copy it to `$PATH` (e.g. `~/bin/`).

## Token expiry

Access tokens last ~1 hour. On `401`, run any `acli` command (e.g. `acli auth status`) to silently refresh, then re-run the script.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Start fixing from the summary alone | The summary is a label, not the bug. Always fetch JSON + attachments first. |
| Skip attachments because "the description has text" | If the description references an image, the image *is* the description. Download it. |
| Ignore comments | Repro steps, decisions, and "this was actually fixed in PR #123" live here. |
| Skip linked issues | "is duplicated by" often points to the ticket with the actual fix plan. |
| Use the site URL for OAuth requests | Use `api.atlassian.com/ex/jira/<cloudId>/...`. |
| Use `Accept: image/png` | 406. Use `Accept: */*`. |
| Search `~/.config/acli` for the token | Token is in macOS Keychain. Config files only have metadata. |
| Treat the keychain blob as a raw token | It is `go-keyring-base64:<base64(gzip(JSON))>`. Decode; extract `access_token`. |
| Give up because `acli` has no download command | The keychain bypass is exactly why this skill exists. |
| Ask the user to paste the screenshot | Don't bother the user when the API is right there. |

## Red flags — stop and re-inspect

If you find yourself thinking any of these, you have not read the ticket completely:

- "Based on the summary, I'll…"
- "Can you share the screenshot?"
- "I'll assume the bug is…"
- "The description doesn't say much, so…"
- "There's an attachment but I don't have access to images"

All of these mean: run the inspection workflow above. Then think.

## Iron law

**If a Jira ticket mentions a screenshot, attachment, comment, or linked issue and you have not read it, you do not understand the bug yet.**

Read the whole ticket. Then think.
