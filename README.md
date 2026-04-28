# Inspecting Jira Issues for AI Agents

A Claude Code skill for reading Jira tickets *completely* — not just the summary, but attachments (screenshots, mockups), comments, linked issues, and custom fields — using `acli` plus a small keychain trick for downloading attachment bytes that the agent can actually view.

## What problem this solves

A Jira summary is a label, not a bug report. The actual repro lives in attachments, comments, and linked issues. AI agents that "work from the summary" guess at half the problem and waste a round-trip asking the human to paste a screenshot the agent could fetch itself.

`acli jira workitem view` shows the issue's text but **has no `attachment download` subcommand**. Naive `curl` against `*.atlassian.net/rest/api/3/attachment/content/<id>` returns 403, and a Bearer-authenticated request with `Accept: image/png` returns 406. This skill bundles the working incantation.

## What's in the skill

| File | Purpose |
|------|---------|
| `SKILL.md` | The inspection workflow + four pitfalls of attachment download |
| `jira-to-markdown.py` | One-shot: writes `ticket.md` + downloads every attachment, ADF→Markdown |
| `download-jira-attachment.sh` | Single-attachment downloader: keychain → OAuth proxy → bytes on disk |

## Requirements

- macOS (uses the `security` CLI for keychain access)
- `python3` (for a one-line JSON parse)
- `acli` installed and authenticated:

```bash
brew tap atlassian/homebrew-acli
brew install acli
acli auth login
```

## Install

Use the [`skills`](https://www.npmjs.com/package/skills) CLI from vercel-labs:

```bash
# user-level (available in every project)
npx skills add sunfmin/inspecting-jira-issues -g

# project-level (drops into ./.claude/skills/)
npx skills add sunfmin/inspecting-jira-issues
```

Then put the helper scripts on `$PATH` so Claude can invoke them directly:

```bash
ln -s ~/.claude/skills/inspecting-jira-issues/jira-to-markdown.py ~/bin/
ln -s ~/.claude/skills/inspecting-jira-issues/download-jira-attachment.sh ~/bin/
```

## Updating / publishing changes

This repo is the upstream that `skills add` reads from — there's no separate registry. To publish a fix:

```bash
git commit -am "…"
git push origin main
```

Existing installs pick up the change with:

```bash
npx skills update inspecting-jira-issues
```

## Usage

Once installed, just ask Claude Code naturally:

```
> Fix https://your-site.atlassian.net/browse/PROJ-1234
> What does PROJ-1234 say?
> Look at the screenshot in PROJ-1234 and tell me what's wrong
```

Claude will fetch the full issue JSON, download every attachment, view each image, walk linked issues and comments — then propose a fix backed by the actual ticket content.

## License

MIT
