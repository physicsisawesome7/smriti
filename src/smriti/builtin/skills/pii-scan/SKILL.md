---
name: pii-scan
description: Count the personal data in a document (Aadhaar, PAN, phone, email, bank details, land-owner names) and flag it when the document's access class or masking settings do not match. Values are never shown.
requires: [view]
autonomy: L0
inputs:
  document: {type: text, label: Document (reference, alias or document id)}
---

# Personal-data scan

Runs the same detectors as ingestion (Saaf) over the stored text and reports
counts per category. A document marked `public` that holds personal data is
critical. Personal data in a document whose access record lists no PII
categories means masking is off for it, which is major. The skill never
returns the values, so it is safe for anyone who can open the document.
