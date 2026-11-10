---
name: citation-check
description: Check a note, letter or AI answer you paste, sentence by sentence, against the library. Each sentence is marked supported, contradicted on a number or a negation, backed only by a superseded rule, or unsupported, with the passage it was checked against.
requires: [view]
autonomy: L0
inputs:
  text: {type: text, label: Text to check}
  as_of: {type: date, label: Check against the rules in force on (optional)}
---

# Citation check

Uses the same verifier that guards Smriti's own answers: content-word support,
numbers that must appear verbatim in the source, and negation that must agree.
It searches only what the person running it may read, so "unsupported" can
also mean the source exists but is outside your access.
