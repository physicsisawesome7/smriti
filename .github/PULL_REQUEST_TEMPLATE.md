## What and why

<!-- One or two sentences. Link the issue: Fixes #123 -->

## How it was tested

- [ ] `ruff check . && ruff format --check .`
- [ ] `pytest -q` (includes the release gates)
- [ ] New or changed behaviour has a test
- [ ] Tried in the web app (screenshot below if the UI changed)

## Does it touch Smriti's promises?

- [ ] Access control: who can read what (retrieval, caches, channels, agents, API)
- [ ] Answer verification or abstention
- [ ] Personal data masking
- [ ] The audit log
- [ ] None of these

If you ticked one, say how the change keeps the promise.

## Notes for reviewers

<!-- Anything risky, follow-ups, or a line for CHANGELOG.md -->
