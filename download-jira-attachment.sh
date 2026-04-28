#!/usr/bin/env bash
# download-jira-attachment.sh — download a Jira attachment using acli's OAuth
# token from the macOS keychain.
#
# Usage: download-jira-attachment.sh <ATTACHMENT_ID> <OUTPUT_PATH>
#
# Why this script exists:
#   - `acli` has no `attachment download` subcommand.
#   - Plain curl against *.atlassian.net rest URLs returns 403.
#   - With Bearer token but `Accept: image/png` → 406 ("Acceptable
#     representations: [application/json]").
#   This script handles all three: pulls the OAuth token from acli's keychain
#   entry, hits the api.atlassian.com OAuth proxy with `Accept: */*`.
#
# Prerequisites:
#   - macOS (uses `security` CLI for the keychain)
#   - `acli auth login` has been run at least once
#   - python3 (for tiny JSON parse)
#
# List attachment IDs first:
#   acli jira workitem attachment list --key KEY --json
#
# Token expiry:
#   Access tokens last ~1h. On 401, run any `acli` command (e.g.
#   `acli auth status`) to silently refresh, then re-run.

set -euo pipefail

if [[ $# -ne 2 ]]; then
    cat >&2 <<'USAGE'
Usage: download-jira-attachment.sh <ATTACHMENT_ID> <OUTPUT_PATH>

  List attachment IDs first:
    acli jira workitem attachment list --key KEY --json
USAGE
    exit 1
fi

ATTACHMENT_ID=$1
OUTPUT=$2

# 1. Read the keychain entry's metadata to find the cloudId.
#    The account name is literally `oauth:<cloudId>:<userId>`.
META=$(security find-generic-password -s acli 2>/dev/null) || {
    echo "No acli OAuth keychain entry found. Run 'acli auth login' first." >&2
    exit 1
}

ACCOUNT=$(echo "$META" | grep -o '"acct"<blob>="[^"]*"' | head -1 \
    | sed 's/"acct"<blob>="//; s/"$//')

if [[ -z "$ACCOUNT" || "$ACCOUNT" != oauth:* ]]; then
    echo "Unexpected acli keychain entry; account is '$ACCOUNT'." >&2
    exit 1
fi

CLOUD_ID=${ACCOUNT#oauth:}      # strip prefix
CLOUD_ID=${CLOUD_ID%%:*}        # take everything before the next colon

# 2. Decode the keychain blob: prefix + base64(gzip(JSON{access_token, ...}))
TOKEN=$(security find-generic-password -s acli -w 2>/dev/null \
    | sed 's/^go-keyring-base64://' \
    | base64 -d \
    | gunzip \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

if [[ -z "$TOKEN" ]]; then
    echo "Failed to decode access_token from keychain entry." >&2
    exit 1
fi

# 3. Hit the OAuth proxy. `Accept: */*` avoids the 406 that image/png triggers.
HTTP=$(curl -sSL -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" \
    -H "Accept: */*" \
    -o "$OUTPUT" \
    "https://api.atlassian.com/ex/jira/$CLOUD_ID/rest/api/3/attachment/content/$ATTACHMENT_ID")

if [[ "$HTTP" != "200" ]]; then
    echo "HTTP $HTTP — failed to download attachment $ATTACHMENT_ID" >&2
    if [[ -s "$OUTPUT" ]]; then
        echo "--- response body ---" >&2
        cat "$OUTPUT" >&2
        echo >&2
    fi
    rm -f "$OUTPUT"
    exit 1
fi

echo "Downloaded attachment $ATTACHMENT_ID → $OUTPUT" >&2
