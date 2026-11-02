# Workflows

A workflow is an agent built as a graph of steps. Open **Agents → New agent**, choose **Workflow** and draw it on the canvas, or start from one of the templates under **Start from a workflow**. It is saved, shared, scheduled, given API keys and chatted with like any other agent.

## Steps

| Step | What it does | Outputs |
|---|---|---|
| **Start** | Every run begins here with `query` (the question) and `chat_history`. | one |
| **AI agent** | Runs a classic, agentic or research agent on a templated prompt, with its own instructions and tools. Its cited answer goes into the output variable (default `answer`) and into `last`. | one |
| **If / else** | Checks conditions in order and follows the first that is true. | one per condition, plus **else** |
| **Set state** | Sets variables from expressions, without calling a model. | one |
| **Code** | Runs Python in the sandbox. The variables are in a dict called `state`; print JSON on the last line to return a value. | one |
| **Human approval** | Pauses the run until the person running it approves or declines. | **approved**, **declined** |
| **Loop** | Runs one agent prompt for each item of a list (up to 20) and collects `[{item, answer}]`. | one |
| **End** | Finishes the run. Its template becomes the answer. | none |
| **Note** | A comment on the canvas. It never runs. | none |

Every step also writes its result to `node_<id>`. A run stops after 50 steps, so a loop back through an If / else cannot run forever.

## Templates and expressions

Prompts, approval messages and End outputs are templates: `Release this answer?\n\n{{ draft }}`. Conditions and Set state take expressions:

```text
len(answer) > 0 and contains(answer, 'clause')
float(amount) * 0.05
[strip(q) for q in split(query, ';')]
join(pluck(answers, 'answer'), '\n\n')
'urgent' if matches(lower(query), 'today|immediately') else 'normal'
```

The expression language is a whitelisted subset of Python expressions, parsed and walked by Smriti, never passed to `eval`. Allowed: literals, variables, arithmetic, comparisons, `and`/`or`/`not`, `in`, indexing, `x if c else y`, simple list comprehensions, and these functions: `abs bool contains default float int join json keys len lower matches max min number pluck round split startswith str strip upper`. Attribute access, imports, lambdas and every other function are rejected when the workflow is saved.

## What still holds inside a workflow

- **Every agent step acts as the person running the workflow.** It sees only what they may see, narrowed by the workflow's sources.
- **Every agent step verifies its answer and masks personal data** before the model, exactly as a stand-alone agent does. Citations from all steps are renumbered into one list, and the final answer is scored against them.
- **Approvals pause the whole workflow.** That covers a Human approval step and also a tool action inside an agent step that needs approval. Only the person who started the run can approve. Inside a Loop, actions that need approval are skipped, and the result records that.
- **Errors stop the run with a reason.** A failing expression, a Code step with the sandbox off, or the step limit ends the run with an answer that names the step that failed.
- **Saving validates the graph.** It checks for exactly one Start, at least one End, every output connected, every step reachable, nothing connected into Start, valid variable names and safe expressions.

## API

Workflows are agents with `"kind": "workflow"` and a `graph`:

```json
{
  "name": "Sign-off flow",
  "kind": "workflow",
  "graph": {
    "nodes": [
      {"id": "start", "type": "start"},
      {"id": "draft", "type": "agent", "data": {"kind": "research", "prompt": "{{ query }}", "output": "draft"}},
      {"id": "ok", "type": "approval", "data": {"message": "Release?\n\n{{ draft }}"}},
      {"id": "yes", "type": "end", "data": {"output": "{{ draft }}"}},
      {"id": "no", "type": "end", "data": {"output": "Held back."}}
    ],
    "edges": [
      {"source": "start", "target": "draft"},
      {"source": "draft", "target": "ok"},
      {"source": "ok", "target": "yes", "handle": "approved"},
      {"source": "ok", "target": "no", "handle": "declined"}
    ]
  }
}
```

Run it with `POST /api/agents/{id}/chat` (streaming or not), or try an unsaved graph with `POST /api/agents/preview`. A paused run returns `status: awaiting_approval` with a `run_id`. Continue it with `POST /api/runs/{run_id}/approve?approve=true`. `GET /api/agents/catalog` lists the step types, templates and expression functions under `workflow`.
