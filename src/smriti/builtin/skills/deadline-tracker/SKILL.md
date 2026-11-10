---
name: deadline-tracker
description: List every deadline and dated obligation in a document (calendar dates and "within N days" periods) with the sentence it comes from, as a table you can export.
requires: [view]
autonomy: L0
inputs:
  document: {type: text, label: Document (reference, alias or document id)}
  start_date: {type: date, label: Start date for "within N days" periods (optional)}
---

# Deadline tracker

Finds calendar dates in Indian day-first order and relative periods such as
"within 30 days" or "not later than 2 weeks". When you give a start date,
relative periods are turned into due dates. Each row keeps the sentence and
clause it came from, so it can be checked against the document.
