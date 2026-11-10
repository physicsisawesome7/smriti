---
name: compare-documents
description: Redline two documents or two editions of one (for example the 2019 and 2024 powers of acceptance), paragraph by paragraph, with what was added, removed and changed.
requires: [view]
autonomy: L0
inputs:
  old: {type: text, label: Earlier document (reference, alias or document id)}
  new: {type: text, label: Later document (reference, alias or document id)}
---

# Compare two documents

Aligns paragraphs with a sequence matcher, then shows a word-level redline
for each changed pair (`[-removed-]` and `{+added+}`). Both documents must be
ones you can open, and personal data stays masked for your role.
