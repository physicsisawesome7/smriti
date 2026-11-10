---
name: rti-reply
description: Draft a reply to a Right to Information (RTI Act 2005) application from the library, point by point with citations, check the 30-day limit under section 7(1) and flag points that may fall under a section 8(1) exemption. The CPIO reviews and signs.
requires: [view, draft]
autonomy: L1
inputs:
  received_date: {type: date, label: Date the application was received}
  questions: {type: text, label: Information sought, one point per line}
  applicant_reference: {type: text, label: Registration number or applicant reference (optional)}
  life_or_liberty: {type: text, label: "Concerns life or liberty (yes or no, optional)"}
  as_of: {type: date, label: Today's date (optional, for the deadline check)}
---

# Draft an RTI reply

For each point the skill searches the library as the officer running it, and
quotes the best passage with its reference. A point with no matching record
is marked "information not held", which the CPIO must confirm. Points that
touch investigations, personal data, commercial confidence or security are
flagged with the clause of section 8(1) to consider. The reply is due 30 days
from receipt, or 48 hours when the application concerns life or liberty.

The output is a draft under maker-checker. The platform never issues it.
Personal data in quoted passages stays masked unless the officer's role
unmasks it, and even then section 8(1)(j) is flagged.
