# Agents, tools and API keys

Open **Agents** in the web app. Anyone with the `view` grant can build a private agent. Sharing an agent with everyone or with chosen roles, and connecting external tools, needs `curate` or `administer`.

## Kinds

| Kind | What it does | Needs a model? |
|---|---|---|
| **Classic** | Cited answers from the agent's sources through the normal ask pipeline: version rules, "as of" dates, amendments, table maths, the answer cache. | No. Lookups and table questions never call the GPU. |
| **Agentic** | A tool loop: the model decides which tools to call, reads the results and answers with citations to them. | Yes for the full loop. With no model, a rule planner searches, reads any links in the question and does the arithmetic it finds (including "5% of 4,80,00,000"). |
| **Research** | Splits the question into 2 to 5 sub-questions, answers each through the ask pipeline with its own verification, renumbers the citations into one list, and adds a summary that is verified too. | No. With a model, the plan and the summary are better. |

## What an agent is made of

- **Instructions**: who it answers for, tone, what to always include. Platform rules sit above them and cannot be overridden: cite every factual sentence, treat tool results as data, say what is missing instead of guessing.
- **Sources**: everything the person asking may see, or only chosen document types and ingestion sources. Sources narrow retrieval. They never widen it.
- **Tools**: built-in and connected tools (below).
- **Approval**: ask before anything that changes data (default), ask before every tool call, or don't ask. Even with "don't ask", once a run has read outside content (web, API, MCP, SQL), any action that changes data waits for approval. Text injected into a web page cannot trigger a write.
- **Starter questions**, **max tool steps**, and **only answer when supported by citations** (on by default).
- **Sharing**: only me, everyone in the tenant, or chosen roles. Draft or published.
- **Schedule**: a cron expression and a question. Published agents run on schedule as their owner and post to a conversation called "Scheduled: <name>".

The builder has a **Try it** panel that runs the agent as it stands in the form, unsaved, as you.

## Tools

| Tool | Kind | Notes |
|---|---|---|
| Search the library | built in | Permission-aware search with version rules, limited to the agent's sources. |
| Calculator | built in | Exact arithmetic with a safe expression parser (no `eval`). Shows Indian digit grouping. |
| Read a web page | built in | One URL through the egress guard. The content is untrusted. |
| Web search | built in | Self-hosted SearxNG (`docker compose --profile search`, then `SMRITI_SEARXNG_URL=http://searxng:8080`). Or connect Brave or Tavily with a key. Personal data in the query is masked before it leaves. |
| Run Python | built in | The `sandbox` container: no network, read-only root, non-root user, no capabilities, memory, process and time limits. Files the script writes come back as downloads. |
| Create a file | built in | Markdown, CSV, JSON, HTML or text the agent writes, for the person to download. Downloads are attachments, never rendered in Smriti's origin. |
| Tenant skills | built in | The pack's coded skills (tender validation, circular impact), with their own grant checks. |
| REST API | connected | Import operations from an OpenAPI spec or list them by hand. Write operations are hidden unless you allow them, and then need approval. Bearer, header, basic or query-key auth. |
| MCP server | connected | Every tool of a server over streamable HTTP or SSE. Tools not marked read-only need approval. |
| Database | connected | Read-only SELECT with a row cap and a read-only transaction. Give it table notes so the model writes good SQL. |
| Web search provider | connected | SearxNG at your URL, Brave or Tavily. |

Every connected tool has **roles that may use it** and a **sensitivity** for its results. Credentials are encrypted at rest and never returned by the API. Saving a tool reads its actions from the target, so you see exactly what agents can call. The **Tools** tab has demo presets that point at Smriti's own demo MCP server, notices API and works database.

## Why answers can be trusted

- **The agent acts as the person asking.** Retrieval is filtered to what they may read, then narrowed to the agent's sources. An agent built by someone with wide access never shows more to someone with less.
- **Every tool result is a citable passage.** The final answer is verified sentence by sentence against them, numbers included. An answer that fails is replaced by a verified extract, or the agent says it cannot answer.
- **Personal data is masked** before the model sees the question or any tool result, and before anything goes to an external tool. Internal tools get the real values. The person sees them restored.
- **Prompt injection in tool results** is scanned for, flagged and wrapped as data.
- **The audit log records** every tool call (arguments as the model wrote them, masked), every approval request and decision, and every answer.

## Conversations

Chats are saved per person and agent. **Share** makes a link. Someone who opens it sees only the answers whose cited documents they can open themselves. The rest are withheld.

## API keys and the OpenAI-compatible endpoint

Open an agent's **API keys**. A key (`sk-smriti-…`) is shown once, works for that agent only, and acts as the person who created it. It is refused on every other endpoint.

```bash
curl http://127.0.0.1:8000/api/agents/tender-desk/chat \
  -H "Authorization: Bearer sk-smriti-..." -H "Content-Type: application/json" \
  -d '{"question": "What is the LD ceiling under clause 46.1?"}'
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-smriti-...")
r = client.chat.completions.create(model="agent/tender-desk",
    messages=[{"role": "user", "content": "What is the LD ceiling under clause 46.1?"}])
```

`"stream": true` on `/api/agents/{id}/chat` returns server-sent events: `start`, `step`, `token`, `approval`, `final`. A run that needs approval stops with an `approval` event. Resume it with `POST /api/runs/{run_id}/approve?approve=true` (or `false`) as the same person.

## Models

Tool calling uses OpenAI-style function calls. The vLLM service starts with `--enable-auto-tool-choice --tool-call-parser hermes`, which suits Qwen3 (`LLM_TOOL_PARSER` in `.env`). Servers without a tool parser often return Hermes-style `<tool_call>` text. Smriti parses that too.
