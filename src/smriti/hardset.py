"""The hard set: a synthetic corpus and question set that can tell systems apart.

The demo pack's golden set (14 questions over 14 documents) is passed by every
embedder, so it guards against regressions but cannot choose between two
configurations. The hard set is built to be failed in places:

* about 290 documents in three generic domains (purchasing and contracts, staff
  policy, equipment maintenance), with near-duplicates that differ only in
  their numbers (the same policy at two sites, sibling machine models);
* about 150 questions in eight slices: exact lookup, paraphrase (the
  question avoids the document's wording and names things differently),
  cross-language (Hindi and Hinglish questions over English documents, English
  questions over Hindi documents), multi-hop (two documents needed), table
  (lookup and arithmetic), superseded (the current rule, and the rule as of
  an earlier date), unanswerable (must abstain) and restricted (the asker
  may not see the document).

Everything is generated from a seed, so a run is reproducible and nothing
here is copied from a real organisation.

    smriti eval hard                     build, index and score
    smriti eval hard --out DIR           also keep the generated pack
    smriti eval compare A B              two configurations on the same questions
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import yaml

SLICES = ("exact", "paraphrase", "cross-language", "multi-hop", "table", "superseded", "unanswerable", "restricted")

SITES = [
    {"key": "NORTH", "name": "North plant", "alias": "northern site", "hi": "उत्तरी संयंत्र"},
    {"key": "SOUTH", "name": "South plant", "alias": "southern site", "hi": "दक्षिणी संयंत्र"},
]

# name: the wording documents use. alias: how a person would say it in a question.
CATEGORIES = [
    ("laboratory consumables", "lab supplies", "प्रयोगशाला की उपभोज्य सामग्री"),
    ("information technology hardware", "computers and servers", "कंप्यूटर और सर्वर"),
    ("security services", "guards and site protection", "सुरक्षा गार्ड सेवा"),
    ("industrial gases", "cylinders of oxygen and nitrogen", "ऑक्सीजन और नाइट्रोजन के सिलेंडर"),
    ("office furniture", "desks and chairs", "मेज़ और कुर्सियाँ"),
    ("personal protective equipment", "helmets, gloves and safety shoes", "हेलमेट, दस्ताने और सुरक्षा जूते"),
    ("catering services", "the staff canteen contractor", "कैंटीन ठेकेदार"),
    ("packaging materials", "cartons and wrapping", "डिब्बे और पैकिंग सामग्री"),
    ("electrical spares", "motors, cables and switchgear", "मोटर, केबल और स्विचगियर"),
    ("calibration services", "instrument accuracy checks", "उपकरणों की सटीकता जाँच"),
    ("waste disposal services", "scrap and refuse removal", "कचरा और स्क्रैप हटाने की सेवा"),
    ("vehicle hire", "rented cars and buses", "किराये की गाड़ियाँ"),
    ("printing and stationery", "paper, forms and pens", "काग़ज़, फ़ॉर्म और कलम"),
    ("software licences", "subscriptions for programs", "सॉफ़्टवेयर की सदस्यता"),
    ("civil repair works", "building patching and painting", "इमारत की मरम्मत और रंगाई"),
    ("fire safety equipment", "extinguishers and alarms", "अग्निशामक यंत्र और अलार्म"),
    ("lubricants and oils", "greases and machine oil", "ग्रीस और मशीन का तेल"),
    ("housekeeping services", "cleaning crews", "सफ़ाई कर्मचारी सेवा"),
    ("medical supplies", "first aid stock", "प्राथमिक चिकित्सा सामग्री"),
    ("training services", "outside instructors and courses", "बाहरी प्रशिक्षक और पाठ्यक्रम"),
    ("raw steel", "plates, bars and coils", "इस्पात की प्लेट, छड़ और कॉइल"),
    ("welding consumables", "electrodes and filler wire", "इलेक्ट्रोड और वेल्डिंग तार"),
    ("measuring instruments", "gauges and meters", "गेज और मीटर"),
    ("pest control services", "fumigation and rodent treatment", "कीट नियंत्रण सेवा"),
    ("courier services", "parcel and document delivery", "पार्सल और दस्तावेज़ पहुँचाने की सेवा"),
]

APPROVERS = ["Section Head", "Department Manager", "Plant Director"]

LEAVES = [
    ("annual leave", "a yearly holiday", "वार्षिक अवकाश"),
    ("sick leave", "being unwell", "बीमारी अवकाश"),
    ("casual leave", "short personal errands", "आकस्मिक अवकाश"),
    ("maternity leave", "a mother after childbirth", "मातृत्व अवकाश"),
    ("paternity leave", "a father looking after a newborn", "पितृत्व अवकाश"),
    ("bereavement leave", "a death in the family", "शोक अवकाश"),
    ("study leave", "sitting examinations or attending a course", "अध्ययन अवकाश"),
    ("sabbatical leave", "a long career break", "सबैटिकल अवकाश"),
    ("compensatory leave", "having worked on a holiday", "प्रतिपूरक अवकाश"),
    ("marriage leave", "their own wedding", "विवाह अवकाश"),
    ("relocation leave", "moving house after a transfer", "स्थानांतरण अवकाश"),
    ("volunteering leave", "unpaid community work", "स्वयंसेवा अवकाश"),
    ("adoption leave", "welcoming an adopted child", "दत्तक ग्रहण अवकाश"),
    ("quarantine leave", "isolation after exposure to an infectious disease", "संगरोध अवकाश"),
    ("election duty leave", "serving as a polling official", "चुनाव ड्यूटी अवकाश"),
]

ALLOWANCES = [
    ("shift allowance", "working rotating shifts", "पाली भत्ता"),
    ("transport allowance", "the daily commute", "परिवहन भत्ता"),
    ("meal allowance", "food during working hours", "भोजन भत्ता"),
    ("housing allowance", "rent", "आवास भत्ता"),
    ("child education allowance", "school fees", "बाल शिक्षा भत्ता"),
    ("remote work allowance", "working from home", "दूरस्थ कार्य भत्ता"),
    ("uniform allowance", "work clothing", "वर्दी भत्ता"),
    ("tool allowance", "buying their own hand tools", "औज़ार भत्ता"),
    ("mobile phone allowance", "official calls on a personal handset", "मोबाइल फ़ोन भत्ता"),
    ("hardship allowance", "a posting to a remote location", "कठिनाई भत्ता"),
    ("night duty allowance", "working after midnight", "रात्रि ड्यूटी भत्ता"),
    ("medical allowance", "routine doctor visits", "चिकित्सा भत्ता"),
    ("fitness allowance", "a gym membership", "फिटनेस भत्ता"),
    ("language allowance", "passing a regional language test", "भाषा भत्ता"),
    ("resettlement allowance", "settling in a new city", "पुनर्वास भत्ता"),
]

MACHINES = [
    ("air compressor", "AC"),
    ("hydraulic press", "HP"),
    ("conveyor drive", "CD"),
    ("chiller unit", "CH"),
    ("boiler feed pump", "BF"),
    ("diesel generator", "DG"),
    ("forklift truck", "FT"),
    ("turning centre", "TC"),
    ("injection moulder", "IM"),
    ("cooling tower fan", "CT"),
    ("dust collector", "DC"),
    ("overhead crane", "OC"),
    ("vacuum pump", "VP"),
    ("carton sealer", "CS"),
    ("welding robot", "WR"),
]
LUBRICANTS = ["ISO VG 32", "ISO VG 46", "ISO VG 68", "ISO VG 100", "SAE 15W-40", "EP 220"]
BOLTS = ["cylinder head", "base frame", "coupling", "bearing cap", "flange"]
FILTERS = ["intake", "oil", "hydraulic return", "coolant", "exhaust"]
PARTS = ["seal kit", "drive belt", "bearing set", "filter element", "gasket set", "pressure sensor", "relief valve"]

VENDOR_A = ["Aravali", "Deccan", "Konkan", "Malwa", "Nilgiri", "Sahyadri", "Vindhya", "Kaveri", "Narmada", "Satpura"]
VENDOR_B = ["Traders", "Engineering", "Supplies", "Services", "Industries"]

DELEGATION_NEW = [("Section Head", 5), ("Department Manager", 25), ("Plant Director", 100), ("Managing Director", 500)]
DELEGATION_OLD = [("Section Head", 2), ("Department Manager", 10), ("Plant Director", 50), ("Managing Director", 250)]

GRADES = ["E1", "E2", "E3", "M1", "M2", "M3", "M4", "D1", "D2", "D3"]

UNANSWERABLE = [
    "What is the warranty period for drone batteries at the East plant?",
    "How many days of pet care leave can staff take each year?",
    "What is the monthly parking fee for visitors at the West plant?",
    "How often must the laser cutter LC-900 be serviced?",
    "Who approved contract CT-2019-900?",
    "What stock options are granted to new joiners?",
    "What is the tightening torque for the turbine blade bolts on the gas turbine GT-70?",
    "How much is the cryptocurrency reimbursement allowance per month?",
    "What performance security is required for buying aircraft engines at the North plant?",
    "What is the retirement age for contract farm workers?",
    "Which vendor supplies the satellite uplink for the South plant?",
    "What is the unit price of the plasma torch for the welding robot WR-999?",
    "How many written quotations are needed before buying submarine cables?",
    "What is the dress code for the annual shareholders' gala?",
    "What did service bulletin SB-2031-01 change?",
]


def _fm(meta: dict[str, Any], body: str) -> str:
    return (
        "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=None) + "---\n\n" + body
    )


def _distinct(rng: random.Random, choices: list[int], other: int | None = None) -> int:
    v = rng.choice(choices)
    while v == other:
        v = rng.choice(choices)
    return v


class Builder:
    """Collects documents and questions; every question records its slice."""

    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.docs: dict[str, str] = {}
        self.cases: list[dict[str, Any]] = []
        self._n: dict[str, int] = {}

    def doc(self, name: str, meta: dict[str, Any], body: str) -> None:
        meta = {"access": {"doc_class": "internal", "divisions": ["*"]}, **meta}
        self.docs[name] = _fm(meta, body)

    def ask(self, slice_: str, question: str, *, user: str = "priya", **kw: Any) -> None:
        self._n[slice_] = self._n.get(slice_, 0) + 1
        self.cases.append(
            {"id": f"{slice_}-{self._n[slice_]:02d}", "slice": slice_, "as_user": user, "question": question, **kw}
        )

    # ---------------------------------------------------------------- purchasing

    def purchasing(self) -> dict[str, dict[str, Any]]:
        rng = self.rng
        policies: dict[str, dict[str, Any]] = {}
        for ci, (name, alias, hi) in enumerate(CATEGORIES, 1):
            prev: dict[str, Any] = {}
            for site in SITES:
                f = {
                    "ref": f"PUR/{site['key']}/{ci:02d}",
                    "name": name,
                    "alias": alias,
                    "hi": hi,
                    "site": site,
                    "thr": _distinct(rng, [2, 3, 4, 6, 8, 12, 15, 20], prev.get("thr")),
                    "approver": rng.choice(APPROVERS),
                    "quotes": rng.choice([2, 3, 4, 5]),
                    "ps": _distinct(rng, [2, 3, 4, 5, 6, 8, 10], prev.get("ps")),
                    "psd": _distinct(rng, [7, 10, 14, 15, 21, 28, 30], prev.get("psd")),
                    "pay": _distinct(rng, [15, 20, 30, 45, 60, 75, 90], prev.get("pay")),
                    "warranty": _distinct(rng, [6, 9, 12, 18, 24, 36], prev.get("warranty")),
                    "ld": rng.choice(["0.25", "0.5", "0.75", "1"]),
                    "ldcap": _distinct(rng, [3, 5, 6, 8, 10, 12], prev.get("ldcap")),
                }
                prev = f
                policies[f["ref"]] = f
                body = (
                    f"# Purchase policy: {name}, {site['name']}\n\n"
                    f"Policy No. {f['ref']} dated 01.04.2023. Synthetic document for the Smriti hard set.\n\n"
                    "### 1. Approval\n\n"
                    f"1.1 Purchases of {name} at the {site['name']} above Rs {f['thr']} lakh require the approval of the {f['approver']}.\n\n"
                    f"1.2 At least {f['quotes']} written quotations shall be obtained for every purchase of {name}.\n\n"
                    "### 2. Security and payment\n\n"
                    f"2.1 The supplier shall furnish a performance security of {f['ps']}% of the order value within {f['psd']} days of the purchase order.\n\n"
                    f"2.2 Payment shall be released within {f['pay']} days of acceptance of the goods or services.\n\n"
                    "### 3. Warranty and delay\n\n"
                    f"3.1 The warranty period for {name} shall be {f['warranty']} months from the date of commissioning or delivery.\n\n"
                    f"3.2 For late delivery, liquidated damages of {f['ld']}% of the order value per week shall be recovered, subject to a maximum of {f['ldcap']}%.\n"
                )
                self.doc(
                    f"purchase-{site['key'].lower()}-{ci:02d}.md",
                    {
                        "title": f"Purchase policy: {name}, {site['name']} (synthetic)",
                        "ref_no": f["ref"],
                        "date": "2023-04-01",
                        "effective_from": "2023-04-01",
                        "doc_type": "purchase-policy",
                    },
                    body,
                )
        refs = sorted(policies)
        rng.shuffle(refs)

        # exact lookups: the question uses the document's own identifiers and words
        for ref in refs[:8]:
            f = policies[ref]
            self.ask(
                "exact",
                f"What is the warranty period under clause 3.1 of purchase policy {ref}?",
                expect_docs=[ref],
                expect_text=[f"{f['warranty']} months"],
            )
        # paraphrases: other words for the thing, the site and the rule
        para = [
            (
                "After winning an order for {alias} at the {site}, how soon must the vendor lodge its guarantee?",
                "psd",
                "{v} days",
            ),
            (
                "How long does the {site} take to settle a vendor's bill for {alias} once the delivery is approved?",
                "pay",
                "{v} days",
            ),
            ("For how long are {alias} bought by the {site} covered against defects?", "warranty", "{v} months"),
            (
                "What is the ceiling on the penalty when a vendor of {alias} delivers late to the {site}?",
                "ldcap",
                "{v}%",
            ),
            (
                "Beyond what order size does buying {alias} for the {site} need sign-off from a senior officer?",
                "thr",
                "{v} lakh",
            ),
        ]
        for i, ref in enumerate(refs[8:28]):
            f = policies[ref]
            q, key, exp = para[i % len(para)]
            self.ask(
                "paraphrase",
                q.format(alias=f["alias"], site=f["site"]["alias"]),
                expect_docs=[ref],
                expect_text=[exp.format(v=f[key])],
            )
        # Hindi questions over English documents
        hi_q = [
            ("{site} में {hi} की खरीद पर वारंटी कितने महीने की होती है?", "warranty", ["{v} months", "{v} महीने", "{v} माह"]),
            ("{site} में {hi} के आपूर्तिकर्ता को भुगतान कितने दिन में किया जाता है?", "pay", ["{v} days", "{v} दिन"]),
            ("{site} में {hi} का ऑर्डर मिलने के बाद गारंटी कितने दिन में जमा करनी होती है?", "psd", ["{v} days", "{v} दिन"]),
        ]
        for i, ref in enumerate(refs[28:36]):
            f = policies[ref]
            q, key, exp = hi_q[i % len(hi_q)]
            self.ask(
                "cross-language",
                q.format(hi=f["hi"], site=f["site"]["hi"]),
                expect_docs=[ref],
                expect_any=[e.format(v=f[key]) for e in exp],
            )
        # amendments: one clause changed later; the current answer and the answer as of an earlier date
        for k, ref in enumerate(refs[36:48], 1):
            f = policies[ref]
            new = _distinct(rng, [7, 10, 14, 15, 21, 28, 30, 45], f["psd"])
            aref = f"{ref}/A{k:02d}"
            self.doc(
                f"amendment-{k:02d}.md",
                {
                    "title": f"Amendment No. {k} to purchase policy {ref} (synthetic)",
                    "ref_no": aref,
                    "date": "2025-02-10",
                    "effective_from": "2025-02-10",
                    "doc_type": "amendment",
                    "modifies": [{"ref": ref, "clauses": ["2.1"]}],
                },
                f"# Amendment No. {k} to purchase policy {ref}\n\n"
                "Dated 10.02.2025. Synthetic document for the Smriti hard set.\n\n"
                f"In partial modification of purchase policy {ref} ({f['name']}, {f['site']['name']}), Clause 2.1 shall be read as:\n\n"
                f'"The supplier shall furnish a performance security of {f["ps"]}% of the order value within {new} days of the purchase order."\n\n'
                "The revised period applies to purchase orders placed on or after 10.02.2025.\n",
            )
            q = f"Within how many days must the performance security be furnished under clause 2.1 of purchase policy {ref}?"
            if k % 2:
                self.ask("superseded", q, expect_docs=[ref, aref], expect_text=[f"{new} days"])
            else:
                self.ask(
                    "superseded",
                    q.replace("must", "did"),
                    as_of="2024-06-01",
                    expect_docs=[ref],
                    expect_text=[f"{f['psd']} days"],
                )
            f["psd_now"] = new
        return policies

    def delegation_and_contracts(self, policies: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        rng = self.rng

        def table(rows: list[tuple[str, int]]) -> str:
            return "| Designation | May approve purchases up to (Rs lakh) |\n|---|---|\n" + "".join(
                f"| {d} | {v} |\n" for d, v in rows
            )

        self.doc(
            "delegation-2022.md",
            {"title": "Delegation of financial powers, 2022 (synthetic)", "ref_no": "DOA/2022", "date": "2022-01-15", "effective_from": "2022-01-15", "doc_type": "delegation"},
            "# Delegation of financial powers\n\nOrder No. DOA/2022 dated 15.01.2022. Synthetic document for the Smriti hard set.\n\n" + table(DELEGATION_OLD),
        )  # fmt: skip
        self.doc(
            "delegation-2025.md",
            {"title": "Delegation of financial powers, 2025 (synthetic)", "ref_no": "DOA/2025", "date": "2025-01-06", "effective_from": "2025-01-06", "doc_type": "delegation", "supersedes": ["DOA/2022"]},
            "# Delegation of financial powers\n\nOrder No. DOA/2025 dated 06.01.2025. Synthetic document for the Smriti hard set.\n\n"
            "In supersession of Order No. DOA/2022 dated 15.01.2022, the revised limits are given below. A purchase above a limit goes to the next higher officer.\n\n"
            + table(DELEGATION_NEW),
        )  # fmt: skip
        self.ask(
            "superseded",
            "Up to what value may a Department Manager approve a purchase?",
            expect_docs=["DOA/2025"],
            forbidden_docs=["DOA/2022"],
            expect_text=["25"],
        )
        self.ask(
            "superseded",
            "Up to what value could a Plant Director approve a purchase?",
            as_of="2023-06-01",
            expect_docs=["DOA/2022"],
            expect_text=["50"],
        )

        contracts = []
        refs = sorted(policies)
        vendors = [f"{a} {b}" for a in VENDOR_A for b in VENDOR_B]
        rng.shuffle(vendors)
        for i in range(1, 46):
            pol = policies[rng.choice(refs)]
            value = rng.choice([3, 4, 8, 12, 18, 22, 30, 45, 60, 80, 95, 120, 180, 240, 400])
            approver = next(d for d, lim in DELEGATION_NEW if value <= lim)
            c = {"ref": f"CT-2025-{i:03d}", "vendor": f"{vendors[i]} Pvt Ltd", "policy": pol, "value": value, "approver": approver, "months": rng.choice([6, 9, 12, 18, 24, 36]), "start": f"2025-{rng.randint(3, 11):02d}-{rng.randint(1, 28):02d}"}  # fmt: skip
            contracts.append(c)
            self.doc(
                f"contract-{i:03d}.md",
                {"title": f"Contract summary {c['ref']}: {c['vendor']} (synthetic)", "ref_no": c["ref"], "date": c["start"], "doc_type": "contract"},
                f"# Contract summary {c['ref']}\n\nSynthetic document for the Smriti hard set.\n\n"
                f"Contract {c['ref']} was placed on {c['vendor']} for {pol['name']} at the {pol['site']['name']}. "
                f"The contract value is Rs {value} lakh and the contract runs for {c['months']} months from {c['start']}. "
                f"The contract is governed by purchase policy {pol['ref']}.\n",
            )  # fmt: skip
        rng.shuffle(contracts)
        for c in contracts[:6]:
            self.ask(
                "exact",
                f"What is the value of contract {c['ref']}?",
                expect_docs=[c["ref"]],
                expect_text=[f"{c['value']} lakh"],
            )
        for c in contracts[6:14]:  # contract -> its value -> the officer whose limit covers it
            self.ask(
                "multi-hop",
                f"Which officer is senior enough to sign off the deal with {c['vendor']}?",
                expect_docs=[c["ref"], "DOA/2025"],
                expect_all_docs=True,
                expect_text=[c["approver"]],
            )
        for c in contracts[14:22]:  # contract -> its policy -> the payment term
            self.ask(
                "multi-hop",
                f"How soon after acceptance will {c['vendor']} be paid under contract {c['ref']}?",
                expect_docs=[c["ref"], c["policy"]["ref"]],
                expect_all_docs=True,
                expect_text=[f"{c['policy']['pay']} days"],
            )
        return contracts

    # ---------------------------------------------------------------- staff policy

    def staff(self) -> None:
        rng = self.rng
        topics = [("leave", *x) for x in LEAVES] + [("allowance", *x) for x in ALLOWANCES]
        rng.shuffle(topics)
        facts = []
        for i, (kind, name, alias, hi) in enumerate(topics, 1):
            hindi = i <= 12  # the first twelve are written in Hindi only
            ref = f"HR/{i:02d}"
            f = {"ref": ref, "kind": kind, "name": name, "alias": alias, "hi": hi, "hindi": hindi}
            if kind == "leave":
                f.update(
                    days=rng.choice([3, 5, 7, 10, 12, 15, 18, 21, 26, 30, 45, 90]),
                    carry=rng.choice([0, 5, 10, 15, 30]),
                    notice=rng.choice([1, 2, 3, 7, 14, 30]),
                )
                en = (
                    f"### 1. Entitlement\n\n1.1 Employees are entitled to {f['days']} days of {name} in a calendar year.\n\n"
                    f"1.2 Unused {name} may be carried forward up to {f['carry']} days.\n\n"
                    f"### 2. Procedure\n\n2.1 An application for {name} shall be made at least {f['notice']} days in advance.\n"
                )
                hi_body = (
                    f"### 1. पात्रता\n\n1.1 कर्मचारियों को एक कैलेंडर वर्ष में {f['days']} दिन का {hi} मिलेगा।\n\n"
                    f"1.2 अप्रयुक्त {hi} अधिकतम {f['carry']} दिन तक अगले वर्ष में ले जाया जा सकता है।\n\n"
                    f"### 2. प्रक्रिया\n\n2.1 {hi} के लिए आवेदन कम से कम {f['notice']} दिन पहले करना होगा।\n"
                )
            else:
                f.update(
                    amount=rng.randrange(1150, 9850, 50) + rng.choice([0, 25]),
                    months=rng.choice([3, 6, 12, 18, 24]),
                    claim=rng.choice([7, 10, 15, 30]),
                )
                en = (
                    f"### 1. Entitlement\n\n1.1 A {name} of Rs {f['amount']} per month is payable to eligible employees.\n\n"
                    f"1.2 An employee becomes eligible after {f['months']} months of continuous service.\n\n"
                    f"### 2. Procedure\n\n2.1 Claims shall be submitted within {f['claim']} days of the end of the month.\n"
                )
                hi_body = (
                    f"### 1. पात्रता\n\n1.1 पात्र कर्मचारियों को ₹{f['amount']} प्रति माह {hi} देय होगा।\n\n"
                    f"1.2 कर्मचारी {f['months']} माह की निरंतर सेवा के बाद इसके पात्र होंगे।\n\n"
                    f"### 2. प्रक्रिया\n\n2.1 दावा माह समाप्त होने के {f['claim']} दिन के भीतर प्रस्तुत करना होगा।\n"
                )
            facts.append(f)
            if hindi:
                title = f"कर्मचारी नीति: {hi} (synthetic)"
                body = f"# कर्मचारी नीति: {hi}\n\nनीति संख्या {ref}, दिनांक 01.07.2023. यह हार्ड सेट के लिए बनाया गया काल्पनिक दस्तावेज़ है।\n\n{hi_body}"
            else:
                title = f"Staff policy: {name} (synthetic)"
                body = f"# Staff policy: {name}\n\nPolicy No. {ref} dated 01.07.2023. Synthetic document for the Smriti hard set.\n\n{en}"
            self.doc(
                f"staff-{i:02d}.md",
                {
                    "title": title,
                    "ref_no": ref,
                    "date": "2023-07-01",
                    "effective_from": "2023-07-01",
                    "doc_type": "staff-policy",
                },
                body,
            )

        # amendments to English leave policies, written first so that every other question expects today's rule
        k = 0
        for f in [x for x in facts if not x["hindi"]]:
            if f["kind"] != "leave" or k >= 6:
                continue
            k += 1
            old, new = f["days"], f["days"] + rng.choice([2, 3, 5])
            f["days"] = new  # every other question about this policy expects the rule in force today
            aref = f"{f['ref']}/A{k:02d}"
            self.doc(
                f"staff-amendment-{k:02d}.md",
                {"title": f"Amendment No. {k} to staff policy {f['ref']} (synthetic)", "ref_no": aref, "date": "2025-04-01", "effective_from": "2025-04-01", "doc_type": "amendment", "modifies": [{"ref": f["ref"], "clauses": ["1.1"]}]},
                f"# Amendment No. {k} to staff policy {f['ref']}\n\nDated 01.04.2025. Synthetic document for the Smriti hard set.\n\n"
                f"In partial modification of staff policy {f['ref']} ({f['name']}), Clause 1.1 shall be read as:\n\n"
                f'"Employees are entitled to {new} days of {f["name"]} in a calendar year."\n',
            )  # fmt: skip
            q = f"How many days of {f['name']} are employees entitled to in a calendar year?"
            if k % 2:
                self.ask("superseded", q, expect_docs=[f["ref"], aref], expect_text=[f"{new} days"])
            else:
                self.ask(
                    "superseded",
                    q.replace("are employees", "were employees"),
                    as_of="2024-08-01",
                    expect_docs=[f["ref"]],
                    expect_text=[f"{old} days"],
                )

        def about(f: dict[str, Any], lang: str) -> tuple[str, dict[str, Any]]:
            """A question in ``lang`` (en, hi, hinglish) that avoids the document's wording."""
            if f["kind"] == "leave":
                exp = {"expect_any": [f"{f['days']} days", f"{f['days']} दिन"]}
                q = {
                    "en": f"How much time off in a year can a staff member take for {f['alias']}?",
                    "hi": f"{f['hi']} साल में कितने दिन का मिलता है?",
                    "hinglish": f"{f['name']} saal mein kitne din ki milti hai?",
                }[lang]
            else:
                exp = {"expect_any": [str(f["amount"])]}
                q = {
                    "en": f"How much does the company pay each month towards {f['alias']}?",
                    "hi": f"{f['hi']} हर महीने कितना मिलता है?",
                    "hinglish": f"{f['name']} har mahine kitna milta hai?",
                }[lang]
            return q, {"expect_docs": [f["ref"]], **exp}

        hindi_docs = [f for f in facts if f["hindi"]]
        english_docs = [f for f in facts if not f["hindi"]]
        for f in hindi_docs[:8]:  # English question, Hindi document
            q, exp = about(f, "en")
            self.ask("cross-language", q, **exp)
        for f in english_docs[:6]:  # Hindi question, English document
            q, exp = about(f, "hi")
            self.ask("cross-language", q, **exp)
        for f in english_docs[6:10] + hindi_docs[8:11]:  # Hinglish
            q, exp = about(f, "hinglish")
            self.ask("cross-language", q, **exp)
        for f in english_docs[10:18]:  # paraphrase in the same language
            q, exp = about(f, "en")
            self.ask("paraphrase", q, **exp)
        for f in english_docs[:6]:
            if f["kind"] == "leave":
                q, exp = (
                    f"How many days of {f['name']} are granted under clause 1.1 of staff policy {f['ref']}?",
                    [f"{f['days']} days"],
                )
            else:
                q, exp = (
                    f"What is the monthly amount of the {f['name']} under clause 1.1 of staff policy {f['ref']}?",
                    [str(f["amount"])],
                )
            self.ask("exact", q, expect_docs=[f["ref"]], expect_text=exp)

    # ---------------------------------------------------------------- maintenance

    def maintenance(self) -> None:
        rng = self.rng
        machines = []
        for mtype, code in MACHINES:
            for n in sorted(rng.sample(range(110, 990, 10), 3)):
                model = f"{code}-{n}"
                parts = [
                    (
                        p,
                        f"{code}{rng.randint(1000, 9999)}",
                        rng.choice([1, 2, 3, 4, 6]),
                        rng.randrange(1210, 9890, 10) + rng.randint(1, 9),
                    )
                    for p in rng.sample(PARTS, 5)
                ]
                m = {"type": mtype, "model": model, "ref": f"MM/{model}", "pref": f"SP/{model}", "hours": rng.choice([250, 400, 500, 750, 1000, 1500, 2000, 3000]), "lub": rng.choice(LUBRICANTS), "sump": rng.choice([4, 6, 9, 12, 18, 25, 40]), "bolt": rng.choice(BOLTS), "torque": rng.randrange(35, 480, 5), "bar": rng.choice([4, 6, 7, 8, 10, 12, 16]), "filter": rng.choice(FILTERS), "fmonths": rng.choice([3, 4, 6, 9, 12]), "parts": parts}  # fmt: skip
                m["barmax"] = m["bar"] + rng.choice([1, 2, 3])
                machines.append(m)
                self.doc(
                    f"manual-{model.lower()}.md",
                    {"title": f"Maintenance manual: {mtype} {model} (synthetic)", "ref_no": m["ref"], "date": "2023-09-01", "effective_from": "2023-09-01", "doc_type": "manual"},
                    f"# Maintenance manual: {mtype} {model}\n\nManual No. {m['ref']} dated 01.09.2023. Synthetic document for the Smriti hard set.\n\n"
                    f"### 1. Routine service\n\n1.1 The {mtype} {model} shall be serviced every {m['hours']} operating hours.\n\n"
                    f"1.2 Use only {m['lub']} lubricant; the sump capacity is {m['sump']} litres.\n\n"
                    f"### 2. Settings\n\n2.1 Tighten the {m['bolt']} bolts to {m['torque']} Nm.\n\n"
                    f"2.2 The normal operating pressure is {m['bar']} bar; shut the unit down if it exceeds {m['barmax']} bar.\n\n"
                    f"### 3. Consumables\n\n3.1 Replace the {m['filter']} filter every {m['fmonths']} months.\n",
                )  # fmt: skip
                rows = "".join(f"| {p} | {pn} | {q} | {price} |\n" for p, pn, q, price in parts)
                self.doc(
                    f"parts-{model.lower()}.md",
                    {"title": f"Spare parts list: {mtype} {model} (synthetic)", "ref_no": m["pref"], "date": "2024-01-10", "doc_type": "parts-list"},
                    f"# Spare parts list: {mtype} {model}\n\nList No. {m['pref']} dated 10.01.2024. Synthetic document for the Smriti hard set.\n\n"
                    "| Part | Part number | Quantity per service | Unit price (Rs) |\n|---|---|---|---|\n" + rows,
                )  # fmt: skip
        rng.shuffle(machines)
        for m in machines[:6]:
            self.ask(
                "exact",
                f"What is the tightening torque under clause 2.1 of maintenance manual {m['ref']}?",
                expect_docs=[m["ref"]],
                expect_text=[f"{m['torque']} Nm"],
            )
        para = [
            (
                "How often is the {model} due for its routine overhaul?",
                lambda m: {"expect_any": [f"{m['hours']} operating hours", f"{m['hours']} hours"]},
            ),
            (
                "What is the highest reading on the gauge at which the {model} may keep running before it has to be switched off?",
                lambda m: {"expect_text": [f"{m['barmax']} bar"]},
            ),
            (
                "How firmly should the fasteners on the {model} be done up?",
                lambda m: {"expect_text": [f"{m['torque']} Nm"]},
            ),
            (
                "Which grade of oil goes into the {model}, and how much does it hold?",
                lambda m: {"expect_text": [m["lub"]]},
            ),
        ]
        for i, m in enumerate(machines[6:16]):
            q, exp = para[i % len(para)]
            self.ask("paraphrase", q.format(model=m["model"]), expect_docs=[m["ref"]], **exp(m))
        for m in machines[16:24]:  # one cell of a table
            part, _, _, price = m["parts"][0]
            self.ask(
                "table",
                f"What is the unit price of the {part} for the {m['type']} {m['model']}?",
                expect_docs=[m["pref"]],
                expect_text=[str(price)],
            )
        for m in machines[24:31]:  # arithmetic over a table
            total = sum(q * price for _, _, q, price in m["parts"])
            self.ask(
                "table",
                f"What is the total cost of the parts needed for one service of the {m['type']} {m['model']}?",
                expect_docs=[m["pref"]],
                expect_any=[str(total), f"{total:,}"],
            )
        for k, m in enumerate(machines[31:39], 1):  # service bulletins change the interval
            new = _distinct(rng, [200, 300, 600, 800, 1200, 1800, 2500], m["hours"])
            bref = f"SB-2025-{k:02d}"
            self.doc(
                f"bulletin-{k:02d}.md",
                {"title": f"Service bulletin {bref}: {m['type']} {m['model']} (synthetic)", "ref_no": bref, "date": "2025-06-02", "effective_from": "2025-06-02", "doc_type": "service-bulletin", "modifies": [{"ref": m["ref"], "clauses": ["1.1"]}]},
                f"# Service bulletin {bref}\n\nDated 02.06.2025. Synthetic document for the Smriti hard set.\n\n"
                f"In partial modification of maintenance manual {m['ref']}, Clause 1.1 shall be read as:\n\n"
                f'"The {m["type"]} {m["model"]} shall be serviced every {new} operating hours."\n\n'
                "The change follows field reports of early bearing wear.\n",
            )  # fmt: skip
            q = f"After how many operating hours must the {m['type']} {m['model']} be serviced?"
            if k % 2:
                self.ask(
                    "superseded", q, expect_docs=[m["ref"], bref], expect_any=[f"{new} operating hours", f"{new} hours"]
                )
            else:
                self.ask(
                    "superseded",
                    q.replace("must", "did").replace("be serviced", "have to be serviced"),
                    as_of="2024-12-01",
                    expect_docs=[m["ref"]],
                    expect_any=[f"{m['hours']} operating hours", f"{m['hours']} hours"],
                )

    # ---------------------------------------------------------------- restricted

    def restricted(self, contracts: list[dict[str, Any]]) -> None:
        rng = self.rng
        for k, c in enumerate(contracts[22:37], 1):
            late, amount = rng.choice([9, 14, 23, 31, 47]), rng.randrange(11200, 98700, 100) + rng.randint(1, 99)
            ref = f"AN-2025-{k:02d}"
            self.doc(
                f"audit-note-{k:02d}.md",
                {"title": f"Internal audit note {ref} (synthetic)", "ref_no": ref, "date": "2025-10-03", "doc_type": "audit-note", "access": {"doc_class": "secret", "divisions": ["*"], "groups": ["audit-team"]}},
                f"# Internal audit note {ref}\n\nSynthetic document for the Smriti hard set.\n\n"
                f"Observations on contract {c['ref']} ({c['vendor']}): the supplier's invoice was passed {late} days after the due date without the approval on file, "
                f"and a recovery of Rs {amount} for late delivery was not made.\n",
            )  # fmt: skip
            q = f"What did internal audit observe about contract {c['ref']}?"
            if k <= 8:  # a member of staff must not be shown the note
                self.ask("restricted", q, user="priya", forbidden_docs=[ref])
            else:  # the auditor must be
                self.ask("restricted", q, user="sana", expect_docs=[ref], expect_text=[str(amount)])
        for k, grade in enumerate(GRADES, 1):
            lo = rng.randrange(310000, 2900000, 10000) + 500
            ref = f"COMP/{grade}"
            self.doc(
                f"compensation-{grade.lower()}.md",
                {"title": f"Compensation band: grade {grade} (synthetic)", "ref_no": ref, "date": "2025-04-01", "doc_type": "compensation", "access": {"doc_class": "confidential", "divisions": ["*"]}},
                f"# Compensation band: grade {grade}\n\nSynthetic document for the Smriti hard set.\n\n"
                f"The annual fixed pay for grade {grade} ranges from Rs {lo} to Rs {int(lo * 1.4)}.\n",
            )  # fmt: skip
            q = f"What is the annual fixed pay range for grade {grade}?"
            if k <= 5:
                self.ask("restricted", q, user="priya", forbidden_docs=[ref], must_abstain=True)
            elif k <= 8:
                self.ask("restricted", q, user="arjun", expect_docs=[ref], expect_text=[str(lo)])

    def build(self) -> Builder:
        policies = self.purchasing()
        contracts = self.delegation_and_contracts(policies)
        self.staff()
        self.maintenance()
        self.restricted(contracts)
        for q in UNANSWERABLE:
            self.ask("unanswerable", q, must_abstain=True)
        return self


TENANT = {
    "id": "hardset",
    "name": "Hard set (synthetic)",
    "locale": "en-IN",
    "languages": ["en", "hi"],
    "models": {"bundle": "sovereign", "chat": {}, "byo_api_fallback": False},
    "retrieval": {"candidates": 50, "top_k": 8, "min_support": 0.2, "index_pii": False, "boilerplate": ["^Synthetic document for the Smriti hard set", "यह हार्ड सेट के लिए बनाया गया काल्पनिक दस्तावेज़ है"]},
    "policy": "Write in plain, formal English. Quote clause numbers, amounts and dates exactly.",
    "seed": [{"path": "corpus", "source": "seed"}],
}  # fmt: skip

ROLES = {
    "roles": {
        "staff": {"description": "Rules everyone may read", "grants": ["view", "draft"], "scope": {"division": "all", "doc_class": ["public", "internal"]}},
        "manager": {"description": "Also confidential material", "grants": ["view", "draft", "validate"], "scope": {"division": "all", "doc_class": ["public", "internal", "confidential"]}},
        "auditor": {"description": "Audit files under a group grant", "grants": ["view", "audit"], "scope": {"division": "all", "doc_class": ["public", "internal", "confidential", "secret"]}},
        "curator": {"description": "Curates sources", "grants": ["view", "upload", "curate"], "scope": {"division": "all", "doc_class": ["public", "internal"]}},
    },
    "users": [
        {"id": "priya", "name": "Priya Nair", "title": "Engineer", "roles": ["staff"]},
        {"id": "arjun", "name": "Arjun Rao", "title": "Department Manager", "roles": ["manager"], "clearance": "confidential"},
        {"id": "sana", "name": "Sana Qureshi", "title": "Internal Auditor", "roles": ["auditor"], "groups": ["audit-team"], "clearance": "secret"},
        {"id": "km", "name": "Kiran Mehta", "title": "Knowledge Manager", "roles": ["curator"]},
    ],
}  # fmt: skip

SCHEMAS = {
    "schemas": {
        "purchase-policy": {"label": "Purchase policy", "match": ["purchase policy"], "chunking": "clause"},
        "staff-policy": {"label": "Staff policy", "match": ["staff policy", "कर्मचारी नीति"], "chunking": "clause"},
        "manual": {"label": "Maintenance manual", "match": ["maintenance manual"], "chunking": "clause"},
        "amendment": {"label": "Amendment", "match": ["amendment no", "shall be read as"], "chunking": "paragraph"},
        "service-bulletin": {"label": "Service bulletin", "match": ["service bulletin"], "chunking": "paragraph"},
        "delegation": {"label": "Delegation of powers", "match": ["delegation of financial powers"]},
        "contract": {"label": "Contract summary", "match": ["contract summary"]},
        "parts-list": {"label": "Spare parts list", "match": ["spare parts list"]},
        "audit-note": {"label": "Audit note", "match": ["internal audit note"]},
        "compensation": {"label": "Compensation band", "match": ["compensation band"]},
    }
}  # fmt: skip


def write_pack(out: Path, seed: int = 7) -> dict[str, Any]:
    """Write the hard-set tenant pack to ``out`` and return what was written."""
    b = Builder(seed).build()
    out = Path(out)
    for sub in ("corpus", "roles", "schemas", "evals"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    def dump(path: Path, data: Any) -> None:
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    dump(out / "tenant.yaml", TENANT)
    dump(out / "roles" / "roles.yaml", ROLES)
    dump(out / "schemas" / "hardset.yaml", SCHEMAS)
    dump(out / "evals" / "golden.yaml", {"cases": b.cases})
    for name, text in b.docs.items():
        (out / "corpus" / name).write_text(text, encoding="utf-8")
    by_slice = {s: sum(1 for c in b.cases if c["slice"] == s) for s in SLICES}
    return {"path": str(out), "seed": seed, "documents": len(b.docs), "questions": len(b.cases), "slices": by_slice}
