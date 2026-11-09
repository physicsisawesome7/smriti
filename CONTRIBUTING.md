# Contributing to Smriti

Thank you for helping. Smriti is permission-aware institutional memory, so the bar for a change is that it keeps three promises: nobody sees what they are not allowed to see, every sentence in an answer is backed by a cited source, and an old version is never quoted as current.

## Ways to help

- **Report a bug** with the [bug form](https://github.com/physicsisawesome7/smriti/issues/new?template=bug_report.yml). Include the version (`smriti --version` or the footer of the web app), how you installed it, and the smallest steps that reproduce it.
- **Propose a feature** with the [feature form](https://github.com/physicsisawesome7/smriti/issues/new?template=feature_request.yml). Describe the problem first. We would rather hear "officers can't find the circular that changed clause 16.1" than "add a button".
- **Report a security issue privately.** See [SECURITY.md](SECURITY.md). Do not open a public issue for it.
- **Improve the docs.** The docs are in [`docs/`](docs) and on [safearly.com/docs](https://safearly.com/docs/quickstart).
- **Add a vertical pack, connector, parser or skill.** See [Extending Smriti](#extending-smriti) below.

## Development setup

You need Python 3.11 or newer. Docker is only needed for the full stack.

```bash
git clone https://github.com/physicsisawesome7/smriti && cd smriti
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev,mcp,tables]"
smriti --data-dir data-dev serve --seed --port 8010  # http://127.0.0.1:8010
```

The demo tenant is seeded on first start. Every document in it is **synthetic**. Pick a person under **Viewing as** to see how answers change with access.

## Before you open a pull request

```bash
ruff check . && ruff format --check .
smriti validate-pack packs/tenants/railways-demo
pytest -q
```

`pytest` includes the release gates: a golden question set, a leakage suite (every person × every document they must not see), a red-team suite and planted-defect recall for the skills. A change that makes any of them fail will not be merged, however small it looks.

Please also:

- **Add a test for every bug fix.** Name it after the behaviour, for example `test_outage_never_tombstones`. `tests/test_audit_fixes.py` has many examples.
- **Keep access checks in one place.** Retrieval enforces access twice: a payload filter inside the search, then a bulk re-check (`engine.py`, `access.py`). New code paths that return document content must go through both.
- **Treat everything a document says as data.** Text from files, pages, APIs and tools never decides access, identity or supersession. Only tenant packs and curators do.
- **Write for a reader under pressure.** UI text and docs are read by officers at work: short sentences, plain words, no jargon.
- **Add a line to [CHANGELOG.md](CHANGELOG.md)** under an "Unreleased" heading when the change is visible to users or operators.

## Commit messages and pull requests

- One topic per pull request. Small is better.
- Write the commit subject in the imperative ("Fix duplicate detection after a revert"), at most 72 characters, with the reason in the body.
- Fill in the pull request template. It asks how you tested the change and whether it touches access, verification, PII or the audit log.
- CI must be green: lint, the test matrix on Python 3.11 to 3.13, the release gates and the Docker build.

## Extending Smriti

| What | Where | Notes |
|---|---|---|
| Vertical or tenant pack | `packs/verticals/<name>`, `packs/tenants/<name>` | YAML roles, sources, schemas, glossary, guardrails and golden questions. `smriti validate-pack` checks it. |
| Connector | `src/smriti/connectors/` | Subclass `Connector`, declare `fields`, yield `Item`s from `discover()`. Every outbound request goes through `egress.guarded_client`. |
| Parser (OCR, layout) | `parsers.EXTERNAL_PARSERS` | Return a `ParsedDocument`; mark low-confidence blocks. |
| Agent tool | `src/smriti/agents/tools.py` | Declare `mutates` for actions that change anything, and `external` when data leaves the deployment. |
| Skill | `packs/**/skills/<name>/` | `SKILL.md` plus coded validators and an `evals/cases.yaml`. |

## Licence

Smriti is licensed under [Apache-2.0](LICENSE). By contributing you agree that your contribution is licensed under the same terms (Apache-2.0, section 5). You keep your copyright. No CLA is required.

## Questions

Open a [discussion or question issue](https://github.com/physicsisawesome7/smriti/issues/new/choose), write to [lalit@safearly.com](mailto:lalit@safearly.com), or reach the team on [LinkedIn](https://www.linkedin.com/company/safeary.ai/).
