# External review (ChatGPT, Claude, and 自動交易專家)

Reviewers can read Binance **Demo** strategy and performance data and leave comments. They cannot approve a strategy, place or cancel orders, revoke an approval, or change settings. The book is the Demo account already shown on the dashboard. Nothing in this path sends a live-trading request.

Participants: `chatgpt` (ChatGPT), `claude` (Claude), `auto_trader` (自動交易專家), and `team` (團隊, dashboard login only).

The Cloud Run service is `crypto-trader-api` (region `asia-east1`). Replace `<SERVICE>` below with its URL, for example the `base` value in `data/cloud_api.json`. Do not put the reviewer token in git, in this file, or in a chat.

## 1. Set `REVIEWER_TOKEN`

The API reads the environment variable `REVIEWER_TOKEN`. The process does not log the value. Cloud Run stores it as a plain environment variable on the `crypto-trader-api` service. It is not a Secret Manager secret, and `deploy-trader` does not describe, create, add a version of, or mount one.

Requirements:

- At least 32 characters. `openssl rand -base64 32` is a good size.
- Different from the dashboard PIN, the session secret, and `TRADER_READONLY_TOKEN`.
- Stored as the GitHub Actions secret `REVIEWER_TOKEN`. This repo never contains the value.

No local gcloud is required. `deploy-trader` reads that Actions secret, masks it with `::add-mask::`, and never prints it. When the secret is non-empty and at least 32 characters, the workflow writes the API service environment variables to a mode-600 file and passes that file to `gcloud run deploy` as `--update-env-vars`. The token is not placed on the shell command line. The update applies to the **API service only**. Trading jobs do not receive `REVIEWER_TOKEN`. The Binance Demo key mounts are unchanged: `BINANCE_DEMO_API_KEY` and `BINANCE_DEMO_API_SECRET` still come from Secret Manager.

`--update-env-vars` changes only the keys in that file. Other environment variables already on the service stay as they are. `--set-env-vars` is not used for this service, because it would replace the full set and drop `REVIEWER_TOKEN` when the GitHub secret is empty.

If the Actions secret is empty, the workflow omits `REVIEWER_TOKEN` from the update. The value already on the service is left unchanged and is not removed. A non-empty secret shorter than 32 characters fails the deploy before Cloud Run is updated.

Anyone who can view the Cloud Run service can read this variable. That includes `roles/run.viewer` and every role that includes it (for example `roles/run.developer`, `roles/editor`, and `roles/owner`), including the service details page in the console. Grant viewer access only to people who may hold the reviewer token.

Rotate the token by generating a new value, updating the GitHub Actions secret `REVIEWER_TOKEN`, and running `deploy-trader` again. That deploy writes the new value onto the API service. There is no Secret Manager version to add.

Until `REVIEWER_TOKEN` is set on the service, the dashboard login still works. Connectors do not.

## 2. Endpoints

| Use | Method and path |
| --- | --- |
| MCP (ChatGPT / Claude connectors) | `POST <SERVICE>/mcp` |
| Strategies | `GET <SERVICE>/review/strategies` |
| NAV, PnL, drawdown, virtual books, fees | `GET <SERVICE>/review/performance` |
| Positions and liquidation distance | `GET <SERVICE>/review/positions` |
| Fills, paged (`limit`, `cursor`) | `GET <SERVICE>/review/trades` |
| Live-vs-backtest parity | `GET <SERVICE>/review/backtest` |
| Orchestrator plan and history | `GET <SERVICE>/review/orchestrator` |
| Comments | `GET <SERVICE>/review/comments` |
| New comment | `POST <SERVICE>/review/comments` |
| Reply | `POST <SERVICE>/review/comments/{id}/reply` |
| OpenAPI schema | `GET <SERVICE>/review/openapi.json` |

Team-only, dashboard session (the reviewer token receives **403**):

- `POST <SERVICE>/review/comments/{id}/status` with `{"status":"open"|"acknowledged"|"done"|"rejected","note":"..."}`
- `POST <SERVICE>/review/comments/{id}/reply` also accepts the dashboard session. The author is stored as `team`.

Comment status values are `open`, `acknowledged`, `done`, and `rejected`. Timestamps are Asia/Taipei (`+08:00`). Comments are stored at `gs://$GCS_BUCKET/research/reviews/comments.json`. That object is private. Do not commit it, and do not create a signed or public URL for it.

Figures come from the same `build_status()` path as `GET /status` (Binance Demo), plus the state fields that path already uses (`capital_basis_history`, orchestrator history, the E5 ledger, and `trader/shadow_ab/e5_targets.json` when it is not already on the state). The latest orchestrator `portfolio_equity` and `peak` match `/status` (futures-wallet capital basis). `drawdown` and `current_drawdown` are `1 - nav/peak`. `max_drawdown_pct` is the deepest drop of that wallet series. `nav_history` / `history` on `/review/performance` is `capital_basis_history` (USDT-M + USDC futures, with the starting capital as the first point when the stored series begins later). `book_history` is the virtual-book sum the drawdown alert uses. `/review/backtest` still returns satellite and U1 parity rows, and adds `e5`: per-day target weight and quantity versus the E5 virtual book.

The reviewer token is rejected on `/status` and on every `/control/*` route, including approve and revoke.

### Auth

Send the token in the header:

```http
Authorization: Bearer <REVIEWER_TOKEN>
```

This is the option to use whenever the client can set a header (Claude request headers, ChatGPT Custom GPT Action, curl).

Connector compatibility also accepts `?access_token=` or `?token=` on `/mcp` and `/review/*` only. Set `REVIEW_ALLOW_QUERY_TOKEN=0` on the service to refuse query tokens.

Trade-offs:

| Method | When to use it | Cost |
| --- | --- | --- |
| `Authorization: Bearer` | Claude static header, Custom GPT Action, any server you control | Token stays out of the URL. Prefer this. |
| Query `access_token` | A connector UI that has no header field (ChatGPT's custom MCP form is in this group; see below) | The token is part of the URL. Cloud Run request logs, browser history, and some proxies store full URLs. Anyone who can read those logs can read the token. Rotate it if the URL leaks. The query token does **not** authenticate `/control/*`. |
| No auth | Not offered | The endpoint returns 401. Do not pick "no authentication" unless the token is already in the URL. |
| OAuth | Not implemented | ChatGPT's MCP UI prefers OAuth. This service does not run an authorization server, dynamic client registration, or a login screen. A shared reviewer credential is not a per-user account. Use the bearer header or, if the product cannot send one, the query token. |

There is no separate reviewer login or PIN. The dashboard PIN remains the team login.

Limits: comment body 8000 characters, reply 4000, suggested action 2000, topic 160. Writes are limited to 8 per minute and 30 per hour per instance (Cloud Run `max-instances` is 2, so the ceiling is per process). Reads are limited to 120 per minute per instance. Adjust with `REVIEW_BODY_MAX`, `REVIEW_WRITE_MAX`, `REVIEW_WRITE_BURST`, `REVIEW_READ_MAX` if you need to.

## 3. Claude custom connector

Claude's labels move. As of the connector docs (static request headers are in beta and only show up for some organizations):

1. Open **Organization settings → Connectors → Add**, then **Custom → Web**. On a personal account the path is **Customize → Connectors → Add custom connector**.
2. Name it `Crypto strategy review`.
3. URL: `https://<SERVICE>/mcp` with no token in the URL. The transport is streamable HTTP. Do not use an `/sse` URL.
4. Authentication: **No sign in**.
5. If **Request headers** is shown, add `Authorization` with the value `Bearer <REVIEWER_TOKEN>` (the word `Bearer`, a space, then the secret). This is the preferred setup. The header is shared by everyone who uses the connector.
6. If Request headers is not available, put the token in the URL instead: `https://<SERVICE>/mcp?access_token=<REVIEWER_TOKEN>`, and still choose **No sign in**. Read the query-token trade-off above before you do this.
7. Do not configure an OAuth client. This server will not complete an OAuth dance.
8. Add the connector, then in a chat ask it to review the book using the tools. Paste the system prompt from section 6 as the first message, or save it in the connector's instructions if the UI has a field.

The server answers `initialize`, `tools/list`, and `tools/call`. A single JSON body is returned when the client accepts `application/json`. A one-event SSE stream is returned when the client accepts only `text/event-stream`. `GET /mcp` is 405; streamable HTTP uses `POST`.

## 4. ChatGPT

ChatGPT's custom MCP form (Plugins → Add custom MCP server; developer mode is no longer a separate switch) offers OAuth, no authentication, or a mix. It does not offer a static bearer field. Two ways to connect:

### 4a. Custom GPT Action (preferred)

The Action sends the token as a header. Download the schema while authenticated, or paste it into the Action. Example that does not print the token into the shell history beyond what `curl` already does — prefer saving the file from a machine you control:

```bash
curl -sS -H "Authorization: Bearer $REVIEWER_TOKEN" \
  "https://<SERVICE>/review/openapi.json" -o review-openapi.json
```

Do not commit `review-openapi.json` if you substituted a real host you consider private. The schema itself has no secret. It does not list the query token, and it does not list the team status route, so the Action cannot close a comment or call `/control/*`.

In the GPT editor: **Actions → Import from URL or schema**, authentication **API key / Bearer**, header `Authorization`. When the UI asks for the key, enter `Bearer <REVIEWER_TOKEN>` if it does not add the `Bearer ` prefix itself. Paste the system prompt from section 6 into the GPT instructions.

### 4b. Custom MCP server

Use this only if you want tool-calling via MCP rather than an Action.

1. On chatgpt.com open **Plugins → + → Add custom MCP server**.
2. Name it `Crypto strategy review`.
3. Server URL: `https://<SERVICE>/mcp?access_token=<REVIEWER_TOKEN>`
4. Authentication: **No authentication** (the token is already in the URL).
5. Confirm the risk prompt and create the plugin. Install it, then invoke it with `@` in a conversation.

Because the token sits in the URL, treat the plugin configuration as secret, restrict who can edit the plugin, and rotate `REVIEWER_TOKEN` if the URL is copied into a ticket or a screenshot. Prefer section 4a when both are available.

## 5. Dashboard

`review.html` is linked from the site nav as **外部審閱**. It uses the existing dashboard login (session bearer), not the reviewer token. Author badges are ChatGPT, Claude, 自動交易專家, and 團隊. Replies nest under the comment (a reply can target another reply with `in_reply_to`). The team can filter by status, reply, and move a comment through 未處理 / 已收下 / 完成 / 不採用. Nothing on this page approves, trades, or revokes.

## Internal agent: 自動交易專家

Id `auto_trader`. It uses the same reviewer token over REST or MCP. It can post a comment, reply inside a thread, and list threads. It cannot set status, approve, trade, or revoke. `team` is not a reviewer id for this token; the dashboard login is the only way to appear as 團隊.

Read the token from a file. Do not paste it into the command, a chat, or git. The shell snippets below never expand the token into the curl command line.

```bash
export REVIEWER_TOKEN_FILE=/path/to/reviewer-token   # mode 600, one line, not in git
umask 077
HDR=$(mktemp)
python3 -c 'import os, pathlib
token = pathlib.Path(os.environ["REVIEWER_TOKEN_FILE"]).read_text().strip("\n")
pathlib.Path(os.environ["HDR"]).write_text("Authorization: Bearer " + token + "\n")'
HDR="$HDR"

# List threads
curl -sS -H @"$HDR" "https://<SERVICE>/review/comments?status=open"

# Post
curl -sS -H @"$HDR" -H "Content-Type: application/json" \
  --data '{"reviewer":"auto_trader","topic":"E5 risk","severity":"medium","body":"liquidation distance is the open question","suggested_action":"compare with the exchange mark"}' \
  "https://<SERVICE>/review/comments"

# Reply, nested under an existing reply id
curl -sS -H @"$HDR" -H "Content-Type: application/json" \
  --data '{"reviewer":"auto_trader","body":"the 0.42 distance is the exchange figure","in_reply_to":"r_REPLACEME"}' \
  "https://<SERVICE>/review/comments/c_REPLACEME/reply"
rm -f "$HDR"
```

MCP is the same header file against `POST https://<SERVICE>/mcp`.

## 6. Suggested reviewer prompt

Paste this as the connector or Custom GPT instructions:

```text
You are a critical reviewer of a Binance Demo crypto strategy book. You have no authority to trade, approve, revoke, or change settings. Every number you cite must come from the tools (list_strategies, get_performance, get_positions, get_trades, get_backtest, get_orchestrator_plan). If a tool fails, say so. Do not invent fills, NAV, or backtest results.

Review for:
- strategy logic that does not match the stated params summary
- overfitting: too many rules for the live sample, unstable weights, a backtest that the live book does not resemble
- risk: leverage, liquidation distance, concentration, paused sleeves that still show exposure
- live-versus-backtest deviation from get_backtest (mismatches, unsupported strategies, missing parity)
- fees and per-strategy virtual-book PnL versus the capital-basis NAV (futures wallet, not a sum of virtual books)

Leave one post_comment per finding. reviewer is chatgpt, claude, or auto_trader (自動交易專家). topic is the strategy code or the risk theme. severity is info, low, medium, high, or critical. body states the evidence (tool fields, not guesses) and why it matters. suggested_action is advice for the team; it is not an order and must not say to move real money. Use reply_comment to continue a thread; set in_reply_to to nest under a specific reply. Do not ask for API keys, PINs, or account identifiers. Times you are shown are Asia/Taipei. The account is Binance Demo only.
```

## 7. What reviewers cannot do

A call with the reviewer token to `/control/pause`, `/control/approve`, `/control/revoke`, `/control/close`, the family approve/revoke routes, or `/status` returns **403**. Changing comment status returns **403**. The dashboard session cannot create an external comment; it can reply and set status. The read-only dashboard token (`TRADER_READONLY_TOKEN`) is also refused on `/review/*`.
