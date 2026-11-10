# Built-in skills

Every tenant gets these. A pack overrides one by shipping a skill with the
same `name`. Each skill is a folder with `SKILL.md` (front matter: when to use
it, the grants it needs, its autonomy level and inputs) and a validator in
`validators/`. Validators decide with code; the model is never the judge.

| Skill | Needs | What it does |
|---|---|---|
| `pii-scan` | `view` | Counts personal data in a document by category and flags it against the document's access class. Never shows the values. |
| `compare-documents` | `view` | Paragraph-level redline between two documents or versions, with a similarity score. |
| `deadline-tracker` | `view` | Lists every dated or "within N days" obligation in a document, with the sentence it comes from. |
| `rti-reply` | `view`, `draft` | Drafts a reply to a Right to Information application from the library, checks the 30-day limit and flags possible section 8(1) exemptions. Maker-checker before issue. |
| `citation-check` | `view` | Checks each sentence of a note you paste against the library: supported, numbers that do not match, or nothing found. |
