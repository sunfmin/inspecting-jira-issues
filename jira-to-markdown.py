#!/usr/bin/env python3
"""
jira-to-markdown.py — fetch a Jira ticket, render it as a self-contained
markdown directory with all attachments downloaded inline.

Usage: jira-to-markdown.py <ISSUE-KEY> [<OUTPUT-DIR>]

Default OUTPUT-DIR: /tmp/jira/<ISSUE-KEY>

Produces:
  <OUTPUT-DIR>/
    ticket.md
    attachments/
      image-foo.png
      ...

Reads acli's OAuth token from the macOS keychain (no `acli auth login`
prompt). On 401 (expired token), run `acli auth status` to refresh and
re-run this script.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


# ---------- keychain + auth ----------

def acli_keychain_account() -> str:
    """Return the keychain account string, e.g. ``oauth:<cloudId>:<userId>``."""
    out = subprocess.run(
        ["security", "find-generic-password", "-s", "acli"],
        capture_output=True, text=True,
    )
    text = out.stdout + out.stderr
    m = re.search(r'"acct"<blob>="([^"]+)"', text)
    if not m:
        sys.exit("No acli OAuth keychain entry. Run 'acli auth login' first.")
    return m.group(1)


def acli_access_token() -> str:
    out = subprocess.run(
        ["security", "find-generic-password", "-s", "acli", "-w"],
        capture_output=True, text=True, check=True,
    )
    blob = out.stdout.strip()
    if blob.startswith("go-keyring-base64:"):
        blob = blob[len("go-keyring-base64:"):]
    decoded = gzip.decompress(base64.b64decode(blob))
    return json.loads(decoded)["access_token"]


def cloud_id_from_account(account: str) -> str:
    parts = account.split(":")
    if len(parts) < 3 or parts[0] != "oauth":
        sys.exit(f"Unexpected acli keychain account format: {account!r}")
    return parts[1]


def jira_get(cloud_id: str, token: str, path: str) -> bytes:
    """GET a path under the OAuth proxy, returning raw bytes."""
    url = f"https://api.atlassian.com/ex/jira/{cloud_id}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(req) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        sys.exit(f"HTTP {e.code} GET {path}\n{body}")


# ---------- ADF → Markdown ----------

INLINE_MARKS = {
    "strong": ("**", "**"),
    "em": ("*", "*"),
    "strike": ("~~", "~~"),
    "underline": ("<u>", "</u>"),
    "code": ("`", "`"),
    "subsup": ("", ""),  # ignore sub/sup
}


def render_text(node: dict) -> str:
    """Render an ADF text node, applying inline marks."""
    out = node.get("text", "")
    if not out:
        return ""
    for mark in node.get("marks", []) or []:
        mt = mark.get("type")
        if mt in INLINE_MARKS:
            o, c = INLINE_MARKS[mt]
            out = f"{o}{out}{c}"
        elif mt == "link":
            href = (mark.get("attrs") or {}).get("href", "")
            out = f"[{out}]({href})"
        elif mt == "textColor":
            pass  # ignore colour
        elif mt == "backgroundColor":
            pass
    return out


def render_children(node: dict, ctx: dict, sep: str = "") -> str:
    return sep.join(adf_to_md(c, ctx) for c in (node.get("content") or []))


def render_list_items(node: dict, ctx: dict, marker_for: callable) -> str:
    pieces: list[str] = []
    for i, item in enumerate(node.get("content") or []):
        body = adf_to_md(item, ctx).rstrip("\n")
        marker = marker_for(i)
        # Indent continuation lines so paragraphs/sub-lists nest correctly.
        indent = " " * (len(marker))
        lines = body.split("\n")
        first, *rest = lines if lines else [""]
        block = marker + first
        for line in rest:
            block += "\n" + (indent + line if line else "")
        pieces.append(block)
    return "\n".join(pieces) + "\n\n"


def adf_to_md(node: dict | None, ctx: dict) -> str:
    if node is None:
        return ""
    t = node.get("type")
    text = node.get("text")

    if t == "doc":
        return render_children(node, ctx)
    if t == "paragraph":
        body = render_children(node, ctx).rstrip()
        return body + "\n\n" if body else ""
    if t == "text":
        return render_text(node)
    if t == "hardBreak":
        return "  \n"
    if t == "heading":
        level = (node.get("attrs") or {}).get("level", 1)
        return "#" * level + " " + render_children(node, ctx).rstrip() + "\n\n"
    if t == "bulletList":
        return render_list_items(node, ctx, lambda i: "- ")
    if t == "orderedList":
        start = (node.get("attrs") or {}).get("order", 1)
        return render_list_items(node, ctx, lambda i: f"{start + i}. ")
    if t == "listItem":
        # Children are usually paragraphs; render them and trim.
        body = render_children(node, ctx).strip()
        return body + "\n"
    if t == "codeBlock":
        lang = (node.get("attrs") or {}).get("language", "") or ""
        body = "".join(c.get("text", "") for c in (node.get("content") or []))
        return f"```{lang}\n{body}\n```\n\n"
    if t == "rule":
        return "---\n\n"
    if t == "blockquote":
        body = render_children(node, ctx).strip()
        return "\n".join("> " + line for line in body.split("\n")) + "\n\n"
    if t == "panel":
        kind = (node.get("attrs") or {}).get("panelType", "info")
        body = render_children(node, ctx).strip()
        prefix = f"> **[{kind}]** "
        lines = body.split("\n")
        first, *rest = lines if lines else [""]
        out = prefix + first + "\n"
        for line in rest:
            out += "> " + line + "\n"
        return out + "\n"
    if t == "mention":
        attrs = node.get("attrs") or {}
        return "@" + (attrs.get("text") or attrs.get("displayName") or "user").lstrip("@")
    if t == "emoji":
        attrs = node.get("attrs") or {}
        return attrs.get("text") or attrs.get("shortName") or ""
    if t == "date":
        ts = (node.get("attrs") or {}).get("timestamp")
        if ts:
            try:
                from datetime import datetime, timezone
                dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
                return dt.strftime("%Y-%m-%d")
            except Exception:
                return f"(date {ts})"
        return ""
    if t == "status":
        attrs = node.get("attrs") or {}
        return f"`[{attrs.get('text','status')}]`"
    if t == "inlineCard":
        url = (node.get("attrs") or {}).get("url", "")
        return f"<{url}>"
    if t == "media":
        attrs = node.get("attrs") or {}
        media_id = attrs.get("id", "")
        alt = attrs.get("alt") or ""
        # ADF media's ``id`` is a UUID separate from the attachment's numeric id.
        # The reliable bridge is ``alt``, which Jira sets to the original filename.
        local = (
            ctx["media_to_filename"].get(alt)
            or ctx["media_to_filename"].get(media_id)
            or ctx["media_to_filename"].get(str(media_id))
        )
        if local:
            return f"![{alt or local}](attachments/{urllib.parse.quote(local)})\n\n"
        return f"![{alt or media_id}](#missing-media-{media_id})\n\n"
    if t in ("mediaSingle", "mediaGroup", "mediaInline"):
        return render_children(node, ctx)
    if t == "table":
        return render_table(node, ctx)
    if t in ("tableRow", "tableCell", "tableHeader"):
        # Handled inside render_table; avoid double-walking.
        return ""
    if t == "layoutSection":
        return render_children(node, ctx, sep="\n")
    if t == "layoutColumn":
        return render_children(node, ctx)
    if t == "expand" or t == "nestedExpand":
        title = (node.get("attrs") or {}).get("title", "Details")
        body = render_children(node, ctx).strip()
        return f"<details><summary>{title}</summary>\n\n{body}\n\n</details>\n\n"
    if t == "decisionList" or t == "taskList":
        return render_children(node, ctx)
    if t == "decisionItem":
        return "- [decision] " + render_children(node, ctx).strip() + "\n"
    if t == "taskItem":
        state = (node.get("attrs") or {}).get("state", "TODO")
        box = "[x]" if state == "DONE" else "[ ]"
        return f"- {box} " + render_children(node, ctx).strip() + "\n"

    # Fallback: render text or children if we don't know the type.
    if text:
        return text
    if node.get("content"):
        return render_children(node, ctx)
    return ""


def render_table(table: dict, ctx: dict) -> str:
    rows = []
    for row in table.get("content") or []:
        if row.get("type") != "tableRow":
            continue
        cells = []
        for cell in row.get("content") or []:
            body = render_children(cell, ctx).strip().replace("\n", " ")
            cells.append(body)
        rows.append(cells)

    if not rows:
        return ""

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    out = ["| " + " | ".join(rows[0]) + " |",
           "|" + "|".join(["---"] * width) + "|"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out) + "\n\n"


# ---------- formatters ----------

def fmt_user(u: dict | None) -> str:
    if not u:
        return "(unassigned)"
    return u.get("displayName") or u.get("emailAddress") or u.get("accountId") or "?"


def fmt_dt(s: str | None) -> str:
    if not s:
        return ""
    return s.split(".")[0].replace("T", " ")


def cell(s: str) -> str:
    """Escape a value for use inside a markdown table cell."""
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


ISO_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def render_scalar_field(v) -> str | None:
    """Render an arbitrary Jira field value as a one-line markdown-table-safe
    string, or return None if the value is empty / not meaningful as a scalar.

    ADF doc values return None — the caller renders them as their own section.
    """
    if v is None or v == "" or v == [] or v == {} or v == "{}":
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v) if v != 0 else None
    if isinstance(v, str):
        return fmt_dt(v) if ISO_DT.match(v) else v
    if isinstance(v, list):
        parts = [render_scalar_field(x) for x in v]
        parts = [p for p in parts if p]
        return ", ".join(parts) if parts else None
    if isinstance(v, dict):
        # ADF doc → caller handles as its own section
        if v.get("type") == "doc":
            return None
        # Common Jira shapes: option, user, named entity, issue ref
        for k in ("value", "displayName", "name"):
            if v.get(k):
                return str(v[k])
        if v.get("key"):
            ksum = (v.get("fields") or {}).get("summary")
            return f"{v['key']}: {ksum}" if ksum else str(v["key"])
        if v.get("emailAddress"):
            return str(v["emailAddress"])
        # Pure-zero composites (progress, aggregateprogress, workratio carriers)
        scalars = [x for x in v.values() if isinstance(x, (int, float, bool))]
        if scalars and all(x == 0 or x is False for x in scalars):
            non_scalars = [x for x in v.values() if not isinstance(x, (int, float, bool))]
            if not non_scalars or all(x in (None, "", [], {}, "{}") for x in non_scalars):
                return None
        return None  # unknown / not worth rendering as scalar
    return None


# Fields that have their own dedicated section in the output, so should NOT
# also appear in the catch-all field table or as auto-rendered ADF sections.
DEDICATED_FIELDS = {
    "summary",        # used as title
    "description",    # ## Description
    "attachment",     # ## Attachments
    "comment",        # ## Comments
    "issuelinks",     # ## Linked Issues
    "subtasks",       # ## Subtasks
}

# Pure plumbing — never useful to a human reader.
SKIP_FIELDS = {"expand", "self", "_links"}


def is_internal_label(label: str) -> bool:
    """Skip labels that are pure-internal Jira machinery."""
    if not label:
        return True
    if label.startswith("[CHART]"):
        return True
    if label in {"Rank"}:
        return True
    return False


# ---------- main ----------

def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 1

    key = argv[1].strip()
    out_dir = Path(argv[2]) if len(argv) >= 3 else Path(f"/tmp/jira/{key}")
    out_dir.mkdir(parents=True, exist_ok=True)
    att_dir = out_dir / "attachments"
    att_dir.mkdir(exist_ok=True)

    account = acli_keychain_account()
    cloud_id = cloud_id_from_account(account)
    token = acli_access_token()

    # Fetch the issue with everything we care about.
    raw = jira_get(cloud_id, token,
                   f"/rest/api/3/issue/{urllib.parse.quote(key)}?expand=names")
    issue = json.loads(raw)
    fields = issue.get("fields") or {}

    # Download attachments and build a map from media-id (UUID) and numeric-id
    # to the local filename. Different ADF versions reference media by either.
    attachments = fields.get("attachment") or []
    media_to_filename: dict[str, str] = {}
    seen_filenames: set[str] = set()
    for att in attachments:
        att_id = str(att.get("id", ""))
        filename = att.get("filename") or f"attachment-{att_id}"
        # Disambiguate collisions
        original = filename
        i = 1
        while filename in seen_filenames:
            stem, _, ext = original.rpartition(".")
            filename = f"{stem}-{i}.{ext}" if stem else f"{original}-{i}"
            i += 1
        seen_filenames.add(filename)

        local_path = att_dir / filename
        if not local_path.exists():
            data = jira_get(cloud_id, token,
                            f"/rest/api/3/attachment/content/{att_id}")
            local_path.write_bytes(data)

        media_to_filename[att_id] = filename
        media_to_filename[filename] = filename
        # ADF media nodes carry a UUID separate from the numeric attachment id;
        # we resolve by alt-text fallback when no UUID match exists.

    ctx = {"media_to_filename": media_to_filename}

    # Comments
    comments = ((fields.get("comment") or {}).get("comments")) or []
    comments_md_parts: list[str] = []
    for c in comments:
        author = fmt_user(c.get("author"))
        created = fmt_dt(c.get("created"))
        body = adf_to_md(c.get("body"), ctx).rstrip()
        comments_md_parts.append(f"### {author} — {created}\n\n{body}\n")

    # Linked issues
    issuelinks = fields.get("issuelinks") or []
    links_md: list[str] = []
    for link in issuelinks:
        type_obj = link.get("type") or {}
        if "outwardIssue" in link:
            rel = type_obj.get("outward", "relates to")
            target = link["outwardIssue"]
        elif "inwardIssue" in link:
            rel = type_obj.get("inward", "relates to")
            target = link["inwardIssue"]
        else:
            continue
        tkey = target.get("key", "")
        tsum = ((target.get("fields") or {}).get("summary")) or ""
        links_md.append(f"- {rel} **{tkey}**: {tsum}")

    # Subtasks
    subtasks = fields.get("subtasks") or []
    subtasks_md: list[str] = []
    for st in subtasks:
        skey = st.get("key", "")
        sf = st.get("fields") or {}
        ssum = sf.get("summary", "")
        sstat = ((sf.get("status") or {}).get("name")) or ""
        subtasks_md.append(f"- **{skey}** ({sstat}): {ssum}")

    # Walk every populated field once, route into:
    #   - scalar_rows: rendered into the header table
    #   - adf_sections: rendered as their own ## section
    # Use the names map (from ?expand=names) to get human labels — falls back
    # to the field id (e.g. customfield_10063) when no label is present.
    names_map = issue.get("names") or {}

    def label_for(fid: str) -> str:
        return names_map.get(fid) or fid

    scalar_rows: list[tuple[str, str]] = []
    adf_sections: list[tuple[str, str]] = []

    for fid, val in fields.items():
        if fid in DEDICATED_FIELDS or fid in SKIP_FIELDS:
            continue
        label = label_for(fid)
        if is_internal_label(label):
            continue
        # ADF doc → its own section (description, environment, custom-field ADFs)
        if isinstance(val, dict) and val.get("type") == "doc":
            md = adf_to_md(val, ctx).rstrip()
            if md:
                adf_sections.append((label, md))
            continue
        rendered = render_scalar_field(val)
        if rendered:
            scalar_rows.append((label, rendered))
    scalar_rows.sort(key=lambda r: r[0].lower())
    adf_sections.sort(key=lambda r: r[0].lower())

    # Always render Description first, even when empty, so readers know it was
    # checked. Environment commonly holds the actual repro on TPT tickets;
    # render it second when present.
    desc_node = fields.get("description")
    desc_md = adf_to_md(desc_node, ctx).rstrip() if desc_node else "_(no description)_"
    env_node = fields.get("environment")
    env_md = adf_to_md(env_node, ctx).rstrip() if env_node else ""

    # Site URL for browse link — derive from accessible resources isn't free,
    # so the URL field is best-effort: read JIRA_SITE env if set, or skip.
    site = os.environ.get("JIRA_SITE", "")
    browse_url = f"{site.rstrip('/')}/browse/{key}" if site else ""

    # Build markdown
    parts: list[str] = []
    parts.append(f"# {key}: {fields.get('summary','')}\n")
    if browse_url:
        parts.append(f"[{browse_url}]({browse_url})\n")
    parts.append("| Field | Value |")
    parts.append("|-------|-------|")
    for lbl, val in scalar_rows:
        parts.append(f"| {cell(lbl)} | {cell(val)} |")
    parts.append("")
    parts.append("## Description\n")
    parts.append(desc_md)
    parts.append("")

    if env_md:
        parts.append("## Environment\n")
        parts.append(env_md)
        parts.append("")

    # Any other ADF fields (besides description/environment, which we already
    # emitted in fixed order above)
    for lbl, md in adf_sections:
        if lbl.lower() in {"description", "environment"}:
            continue
        parts.append(f"## {lbl}\n")
        parts.append(md)
        parts.append("")

    if attachments:
        parts.append("## Attachments\n")
        for att in attachments:
            fn = media_to_filename.get(str(att.get("id", "")), att.get("filename", ""))
            size = att.get("size", 0)
            parts.append(f"- [{fn}](attachments/{urllib.parse.quote(fn)}) — {size} bytes")
        parts.append("")

    if links_md:
        parts.append("## Linked Issues\n")
        parts.extend(links_md)
        parts.append("")

    if subtasks_md:
        parts.append("## Subtasks\n")
        parts.extend(subtasks_md)
        parts.append("")

    parts.append(f"## Comments ({len(comments)})\n")
    if comments_md_parts:
        parts.append("\n".join(comments_md_parts))
    else:
        parts.append("_(no comments)_")

    out_file = out_dir / "ticket.md"
    out_file.write_text("\n".join(parts) + "\n")

    print(f"Wrote {out_file} ({len(attachments)} attachment(s))", file=sys.stderr)
    print(out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
