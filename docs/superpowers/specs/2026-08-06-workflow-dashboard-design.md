# Workflow Dashboard — Design

**Date:** 2026-08-06
**Status:** Approved for planning
**Supersedes:** nothing. Extends the existing invoice processor.

---

## 1. Problem

The app today is an artifact pipeline: invoices in → Claude extraction → filed under a
property → staged → assembled into a bank-rec packet → reconciled. It handles things that
*arrive*.

The Korus Property Accounting SOP describes something different: a calendar with
dependencies, spanning 17 accounts and 9 ownership groups. Almost none of it is in the app.

| SOP section | In app today |
|---|---|
| §2 month shape (Day 1 → month end) | no |
| §3 six day-1 information requests, and what each blocks | no |
| §4 rent posting (post NEXT month — SOP calls this the most common error) | no |
| §5.2 Yardi recurring payables | no |
| §5.3 payment routing (autopay / ACH / EFT / check) | no field exists |
| §6 check runs, check-number sequence | `check_number` column only |
| §7 report month-range rules, Noel's 12-month exception | no |
| §8.2 per-client packet contents | generic packet only |
| §9 property procedures (LADWP 4-sheet update, Allied proration, fixed-amount checks) | no |
| §10 autopay sweep | no |
| App. A credentials index / App. B open gaps | no |

The user's stated goal is to forget as few things as possible. Of four candidate pain
points they identified three:

1. **Missing invoices** — a vendor's invoice never arrived or never got entered, discovered
   mid-reconciliation when a statement line has no Yardi entry (SOP §8.1 step 4).
2. **Calendar items** — check runs, rent posting, distributions, day-1 requests: right work,
   wrong week, or skipped.
3. **Autopay didn't post** — one of the automatic debits silently failed, found late or never.

Packet contents (§8.2) was explicitly *not* selected and is out of scope for round one.

All three selected pains are the same shape: **something expected did not happen, and
nothing said so.**

---

## 2. Scope

### In scope

- An obligation ledger: per-month instances of every recurring task, request, verification,
  and expected invoice.
- A recurrence-learning engine that derives expected invoices from history plus two
  authoritative lists.
- A vendor identity layer (canonical vendors + aliases) with a human verification queue.
- Draft-and-send email composition for the day-1 request batch and rent-posting notices.
- A one-time import that seeds all of the above from `Korus_Monthly_Close_System.xlsx`,
  `Vendor Directory - Active Only.xlsx`, and the existing 264-invoice history.
- Four narrowly-scoped new uses of the Anthropic API (§8).

### Out of scope (round one)

- Per-client owner packet checklists (SOP §8.2). Deliberately deferred — not a selected pain.
  The `Bank Rec Packets` sheet is still imported and stored so a later round can build on it
  without a second migration.
- Anything that sends email unattended, holds mail credentials, or auto-clears a
  reconciliation.
- Credentials storage. Appendix A is an *index* of what exists and where; values stay in a
  password manager. The app stores names only.
- Resolving the eight open gaps in SOP Appendix B. They are recorded, not answered.

---

## 3. Decisions

These were settled during brainstorming and are not open for re-litigation during planning.

| Decision | Choice | Consequence |
|---|---|---|
| Fate of the workbook | **App replaces it.** One-time import seeds the DB; the app then owns the calendar, autopay list, recurring list, and packet definitions. Workbook is archived. | Needs edit screens for the imported data sets. Removes the risk of the two drifting apart — itself a forgetting risk. |
| Email | **Draft, user sends.** App composes recipient, subject, body, attachments and hands off to the mail client (or writes a `.eml`). Tracks Sent / Received. | No SMTP credentials stored. Nothing leaves the machine unreviewed. Retains the day-1 time saving. |
| Enforcement | **Warn only, never block.** | See the note below. |
| Architecture | **Obligation ledger with evidence satisfaction** (approach A, §4). | Larger schema and a rollover step, bought for per-month history and zero-touch satisfaction of the two biggest pains. |

### Note on warn-only

Warn-only was the user's explicit choice over a recommendation to hard-gate the three places
the SOP itself says stop (deposits balancing to zero before a packet is assembled, check
number matching the physical checkbook before printing, rent roll confirmed before posting).
The user's call stands and the design implements it.

The known failure mode is warning fatigue: a warning seen fifty times stops registering.
The design mitigates this structurally rather than by adding gates:

- There is exactly one warnings surface (the Today view's "Needs attention" list), not a
  persistent banner on every page.
- An item enters it only once past its *learned* due window plus slack — not on a guessed
  date (§5). On a good day the list is empty.
- Severity escalates with age rather than by category, so a genuinely stale item eventually
  looks different from a fresh one.

If the list is routinely long in practice, the correct fix is tightening the timing model,
not adding gates. This is stated so a future reader knows warn-only was a decision, not an
oversight.

---

## 4. Architecture

### 4.1 The obligation ledger

Two tables carry all four kinds of expectation uniformly.

```
obligation                 -- the template
  id
  kind                     -- ACTION | VERIFY | ASK | EXPECT
  title
  client                   -- ownership group, e.g. "Noel"
  property_id              -- nullable; some obligations are portfolio-wide
  vendor_id                -- nullable; set for EXPECT and VERIFY
  window_rule              -- see 4.3
  depends_on               -- nullable obligation id
  method                   -- check | EFT | ACH | autopay | n/a
  fixed_amount             -- nullable
  source                   -- authoritative | learned | manual
  confidence               -- high | medium | low; denormalized from the recurrence
                           -- profile (§5.2) at refresh time. Authoritative and manual
                           -- obligations are always 'high'. Stored rather than computed
                           -- per render so warning eligibility (§5.4) is a plain query.
  active                   -- 0 | 1
  notes

obligation_instance        -- one row per obligation per period
  id
  obligation_id
  period                   -- "August 2026"
  due_from, due_to         -- resolved dates for this period
  state                    -- open | done | skipped | retired
  satisfied_by             -- 'tick' | 'invoice:<id>' | 'stmtline:<hash>' | 'auto'
  done_at
  sent_at, received_at     -- ASK only
  note                     -- required when a month closes with the instance open
```

The four kinds, and why one table holds them:

- **ACTION** — you do it. Check runs, rent posting, distribution checks, saving statements.
  Satisfied by a tick.
- **ASK** — you request it from a person. The six day-1 requests. Satisfied by a tick, but
  carries `sent_at` / `received_at` so blocked downstream work can name who it is waiting on.
- **VERIFY** — confirm something happened without you. The autopay debits. Satisfied by
  **evidence**: a parsed bank-statement line matching vendor, window, and amount profile.
- **EXPECT** — an invoice that should arrive. Satisfied by **evidence**: a matching invoice
  row landing in the database.

The leverage is `satisfied_by`. For EXPECT and VERIFY — the two kinds behind the user's #1
and #3 pains — nothing is ticked by hand. An EXPECT closes itself when the processor files
the matching invoice. A VERIFY closes itself when the statement is dropped in during
month-end and `parse_statement` finds the line. Everything still open is the answer to
"what did I forget."

### 4.2 Evidence satisfaction, and its timing limits

**EXPECT** resolves continuously through the month, as invoices are processed. This is the
prospective case: by the 25th, the still-open EXPECT list *is* the missing-invoice list —
found before reconciliation instead of during it.

**VERIFY** is retrospective and this must be stated plainly: bank statements arrive Days 1–5
for the *prior* month (SOP §2), so a debit expected on the 28th is not verifiable from a
statement until roughly five weeks later. The design accepts this rather than pretending
otherwise:

- A VERIFY instance sits in state `open` with a distinct display of "awaiting statement"
  until the covering statement is imported. It does not appear in the warnings list during
  that window, because there is nothing the user could do about it.
- Once the statement is imported, unmatched VERIFY instances become warnings immediately and
  loudly — that is the moment the information is actionable.
- A manual tick is always available for a user who checked the bank site directly.

Matching a statement line to a VERIFY uses the existing ladder in `core/bankrec.py`
(check number → verified amount → settlement membership → subset sum → vendor name), plus
the vendor identity layer from §6. A match is recorded with its evidence key so it can be
audited or undone.

### 4.3 Window rules

`window_rule` is a small declarative string, parsed into `(due_from, due_to)` for a given
period. Supported forms, all present in the source material:

| Form | Example | Source |
|---|---|---|
| `day:N` | `day:1` | "Send all six requests on the 1st" |
| `day:N-M` | `day:28-30` | Autopay Sweep "28th–30th, monthly" |
| `week:N` | `week:2` | "Second week: main check run" |
| `week:N-M` | `week:1-2` | "First or second week: Noel management checks" |
| `last-week` | | "Last week: HSF checks, post next month's rent" |
| `month-end` | | "Autopay sweep" |
| `learned` | | Derived from history (§5); resolves to a median day ± spread |

Cadence is a separate modifier: `monthly` (default), `even-months`, `odd-months`,
`quarterly`. Even/odd is not optional — SOP §10 states LADWP bills Lisa Ahn and Sunggwang on
even months and Monette on odd months. A monthly-only model would raise a false missing
warning for half of those every single month.

### 4.4 Period rollover

Creating instances for a new period is an explicit, idempotent operation, not a background
job:

- Triggered when the current period is changed on the Settings page, and available as a
  manual "open month" action.
- Idempotent: re-running for an existing period creates only instances that do not already
  exist. It never resets state on an existing instance.
- Instances left `open` when a month closes are **carried forward** into the next period with
  their original period recorded, and closing requires a typed reason on each (the warn-only
  model's one point of friction, and the only one).

---

## 5. Recurrence learning

### 5.1 What the data supports

Measured against the live database (264 invoices) on 2026-08-06:

```
Months of history (by invoice date):
   2026-05   25      (partial)
   2026-06   91      (complete)
   2026-07  108      (complete)
   2026-08   23      (in progress, as of the 6th)

(property, vendor) pairs present in BOTH June and July:  32
   of those, already seen in August:                     11
   not yet seen in August as of the 6th:                 21

Pairs recurring across >=2 distinct months:              47
```

Day-of-month spread for those 47 recurring pairs:

```
   invoice_date    median spread  2 days   (28 of 47 pairs land within a 3-day window)
   date_processed  median spread 16 days   (only 14 of 47 that tight)
```

**This is the finding the timing model rests on.** `date_processed` records when the user
got to the invoice — batching makes it noise. `invoice_date` records when the vendor billed,
and it is genuinely regular. Every timing decision in the system reads `invoice_date`.
Nothing may key a due window off `date_processed`.

Amount stability is also learnable and varies widely: some pairs are fixed
(`rolling greens` at 790.50, `athens / 10630 Santa Monica` at 1,125.06), some tight
(`mitsubishi` cv 0.03, `west coast maintenance` cv 0.10), some genuinely variable
(`LADWP / Sunggwang` cv 2.25).

**Honest limit:** two complete months is thin. At launch the learned half will be modest and
low-confidence, and the two authoritative lists carry the load. Confidence tightens every
month the system runs. This is a stated property of the design, not a defect to be worked
around.

### 5.2 The recurrence profile

Computed per (property_id, vendor_id) pair, on demand and cached:

| Field | Derived from |
|---|---|
| `months_seen`, `consecutive_months` | distinct `invoice_date` year-months |
| `cadence` | monthly / even-months / odd-months / quarterly / irregular |
| `due_day`, `due_spread` | median and range of `invoice_date` day-of-month |
| `amount_profile` | `fixed` \| `tight` (cv < 0.15) \| `variable`, with last value and range |
| `confidence` | **high**: n ≥ 3 and spread ≤ 3 · **medium**: n ≥ 3, spread ≤ 10 · **low**: otherwise |

### 5.3 Precedence

Three sources feed the EXPECT and VERIFY obligation sets, in this order:

1. **Authoritative** — the 42 `Yardi Recurring Setup` rows (which carry fixed amounts and
   method) and the 20 `Autopay Sweep` rows (which carry explicit windows). The user stated
   these; they win on every field they specify.
2. **Learned** — the 47 recurring pairs from history. Fills everything the lists do not name.
3. **Manual** — anything the user adds or mutes.

Every obligation displays its source, so "why is this asking me?" always has an answer.

### 5.4 Warning timing

An EXPECT enters the warnings list only when `today > due_to + slack`, where slack is set by
confidence:

- **high** → slack 2 days. Surfaces on or near the real due date.
- **medium** → slack 7 days.
- **low** → does not surface until the last week of the period.

This is the mechanism that makes warn-only viable. Flagging all 21 currently-unseen August
pairs on the 6th would be noise; most legitimately arrive later in the month.

### 5.5 Drift in both directions

An expectation list that only grows becomes noise within a year.

- **Promotion:** a (property, vendor) pair seen in two consecutive months and not already an
  obligation is proposed automatically, badged `new`. The user keeps or mutes it.
- **Retirement:** an obligation missed in two consecutive periods prompts *"retire this?"*
  rather than warning a third time. Retiring sets `active = 0`; history is kept.

---

## 6. Vendor identity

### 6.1 Why it is a prerequisite

Expectations key on `vendor_id`, not on a string. Without this layer `Athens Services` and
`ATHENS SERVICES` are two half-confident expectations that each look sporadic; with it they
are one confident monthly expectation.

The machinery mostly exists but is wired to the wrong job: `match_vendor_short_name`
(`core/processor.py:832`) already does exact → alias-inside → close-spelling matching,
identical in shape to `match_property` (`core/processor.py:760`), but its docstring scopes it
to filenames only and the `vendors` table holds 3 rows, so it effectively never fires.

### 6.2 Schema

- `vendors` gains `canonical_name`, `short_name`, `aliases` (semicolon-separated, same
  convention as `properties.aliases`), `active`.
- `invoices` gains `vendor_id` (nullable) and `vendor_needs_review` (0 | 1).
- The raw `vendor_name` Claude extracted is **never overwritten**. It is provenance, and it
  is what makes a bad merge reversible.

### 6.3 Match pipeline

Runs at process time, reusing the three existing tiers with an added confidence band:

| Tier | Outcome |
|---|---|
| Exact match on canonical name or alias (normalized) | bind silently |
| A known alias appears inside the extracted name, longest wins | bind silently |
| Close spelling, ratio ≥ 0.92 | bind silently |
| Close spelling, 0.80 ≤ ratio < 0.92 | **suggest** → review queue |
| No match | **new** → review queue |

Only high confidence binds without a human. Everything else queues.

### 6.4 Verification UX

A second tab on the existing Fixer page, not a new page — same place, same muscle memory as
the property review the user already trusts. Each row shows the string as the vendor printed
it, the top suggestion, and three actions: `Confirm` · `Pick another` · `It's new`.

**Confirming writes the raw string into that vendor's aliases.** `MITSUBISHI ELECTRIC US,
INC.` is asked once, ever. The queue shrinks toward zero on its own.

### 6.5 Bootstrap

Clustering the 264 existing invoices (normalize, then `difflib` ratio ≥ 0.86) yields:

```
   95 distinct raw vendor strings
   82 clusters
      69 singletons        -- auto-accept, short name suggested
      13 need a decision   -- of which 12 are pure case differences
```

Roughly fifteen minutes, once. Only four are genuinely ambiguous and require the user's
judgment rather than a rubber stamp:

- `Michelle Suh` vs `Michelle Suh (Rooter Plumbing)`
- `James Chin` vs `James Chin (Stamp Reimbursement)`
- `South Coast Mechanical, LLC` vs `South Coast Mechanical, Inc.`
- `City of Los Angeles` vs `City of Los Angeles, Department of Public Works, Bureau of
  Sanitation`

### 6.6 The abbreviation gap

String distance will never join `LADWP` ↔ `Los Angeles Department of Water and Power`. The
invoices print the full name; the SOP and the Autopay Sweep both say `LADWP`. Two fixes,
both cheap:

1. Seed aliases from the SOP portfolio table, the workbook sheets, and the 168-row Vendor
   Directory during import.
2. For a genuinely new vendor, ask Claude for canonical name, short name, and likely
   abbreviations (§8). A handful of calls a month, not one per invoice.

---

## 7. Pages

Existing pages keep their jobs. New and changed:

| Page | Role |
|---|---|
| **Today** (new home) | What is due now, what is overdue, what is blocked and on whom. The single warnings surface. Replaces the current Dashboard as the landing page; the current stat cards move here. |
| **Month** | The calendar grid for the current period — every obligation, its window, its state. Per-account progress. Replaces the `Close Tracker` and `Monthly Calendar` sheets. |
| **Expected** | The EXPECT and VERIFY sets: what should arrive, what has, what has not, and each item's source and confidence. Where obligations are added, muted, and retired. |
| **Requests** | The six ASK obligations: draft, mark sent, mark received, see what each is blocking. |
| **Reference** | Read-only: property procedures (SOP §9), the credentials index (names only, no values), and the Appendix B gaps register. |
| **Fixer** | Gains a second tab for vendor review (§6.4). |
| **Vendors** | Gains canonical name, short name, aliases, active. |
| **Settings** | Gains the period-rollover action. |

Design language is already settled and is not revisited: IBM Plex Sans/Mono, `#3a5bd9`
accent, 236px sidebar, 12px cards, dark mode via `prefers-color-scheme`. New pages inherit
`base.html` and the tokens in `static/style.css`, which is the source of truth.

---

## 8. Anthropic API usage

The app already calls Claude for invoice extraction (`claude-haiku-4-5`, ~110 invoices a
month). Four narrow additions, and an explicit list of places it must not be used.

### 8.1 Where it is used

1. **Bank statement descriptor → vendor.** Bank descriptors are semantically opaque
   (`ATHENS SERVIC DES:PAYMENT ID:8842 INDN:KORUS`); no regex maps that to `Athens Services`.
   Claude sees **only the leftovers** — statement lines the existing match ladder could not
   resolve — and **proposes**. It never auto-clears. This preserves the property the README
   already states: only strong matches are auto-reconciled.
2. **Multi-account utility bills.** SOP §9.1: one LADWP bill covers five accounts (Army,
   Navy, Marines, Media Center, Billboard), currently split by hand and copied into four
   spreadsheets. Reading one document and allocating it five ways is document understanding.
3. **New-vendor canonicalization.** Canonical name, short name, likely abbreviations, for a
   vendor with no alias match (§6.6). Human still confirms.
4. **Amount anomaly *explanation*.** Detection is a range check against the learned amount
   profile — arithmetic. The explanation ("SCE Beach is 4,180 against a usual 19,572–34,396")
   requires reading the PDF: partial period, credit applied, meter re-read. Advisory only, so
   a wrong answer costs nothing.

### 8.2 Where it is not used

| Task | Use instead |
|---|---|
| Is this autopay / EFT / check? | Vendor attribute — a lookup |
| What is missing this month? | Set difference |
| Monthly vs even-month vs odd-month? | Statistics on `invoice_date` — deterministic, unit-testable |
| Is this amount anomalous? | Range check against the amount profile |
| The six day-1 emails | Templates with variables. They are the same six every month; a model adds variance, not value. Have Claude write the templates once, then never call it again. |
| Clearing a reconciliation, or releasing a payment | Never. Nondeterminism on money is a bad trade. |

### 8.3 Model choice and two upgrades

- **Haiku 4.5** stays for bulk extraction — high volume, well-scoped.
- **Sonnet 5** for the judgment calls (8.1 items 1 and 2): low volume, higher stakes.
- **Structured outputs.** `core/processor.py:262` catches `json.JSONDecodeError`. Passing
  `output_config={"format": {"type": "json_schema", "schema": ...}}` removes that failure
  mode; supported on Haiku 4.5.
- **Prompt caching — measure before relying on it.** The system prompt embeds the property
  list and aliases (`core/processor.py:150`), identical across a batch. But Haiku 4.5's
  minimum cacheable prefix is 4096 tokens and this prompt is likely shorter; below the
  minimum, caching silently does not happen (`cache_creation_input_tokens: 0`, no error).
  Verify with `count_tokens` before adding `cache_control`. Sonnet 5's minimum is 1024.

Cost is not the constraint: extraction runs roughly $1/month today, and the additions are
~60 low-volume calls. Correctness is the constraint, which is what §8.2 is for.

---

## 9. Import and bootstrap

One-time, idempotent, and refuses to run twice without `--force`, matching the existing
`migrate_to_db.py` convention.

| Source | Feeds |
|---|---|
| `Korus_Monthly_Close_System.xlsx` → `Monthly Calendar` (40 lines) | ACTION obligations |
| → `Info Requests` (6) | ASK obligations |
| → `Autopay Sweep` (20) | VERIFY obligations, authoritative windows |
| → `Yardi Recurring Setup` (42) | EXPECT obligations, authoritative amounts and methods |
| → `Close Tracker` (22 accounts) | account list, cross-checked against `properties` |
| → `Bank Rec Packets` (79 lines) | stored for a later round; not surfaced in round one |
| `Vendor Directory - Active Only.xlsx` (168 rows) | vendor canonical names and aliases |
| `Monthly Bank Reconciliation Reports - Owners email list.xlsx` (31 rows) | recipients for draft emails |
| `data/invoices.db` (264 invoices) | vendor clustering (§6.5), recurrence profiles (§5.2) |

The SOP itself is not machine-read. Its property procedures (§9), credentials index
(App. A), and gaps register (App. B) are transcribed into the Reference page as content
during implementation.

---

## 10. Failure modes and error handling

- **A bad vendor merge.** Recoverable, because raw `vendor_name` is preserved on every
  invoice. An un-merge action re-runs the match pipeline for the affected invoices.
- **A statement that will not parse.** Existing behaviour is unchanged: the assembler
  reports it. VERIFY instances for that account stay "awaiting statement" rather than being
  wrongly marked missed.
- **Re-running rollover.** Idempotent by construction (§4.4). Never resets an existing
  instance.
- **A learned expectation that is simply wrong.** Muting is one click and always available
  from the Expected page. Muting is preferred over deleting so the history stays intact.
- **Import run against a database that already has obligations.** Refuses, and says so.
- **Windows file locks.** Unchanged from current behaviour — the app names the blocking file
  and never half-applies a change.

## 11. Testing

Extends the existing `tests/` suite (26 unittest cases, no DB or network required). New
cases, all pure functions with fixture data:

- Window-rule parsing: every form in §4.3, across month lengths, including February and
  months where "week 2" and "the 8th–14th" diverge.
- Cadence detection: monthly, even-months, odd-months, quarterly, irregular — with the real
  LADWP even/odd split as a fixture, since a regression there produces false warnings at
  scale.
- Recurrence profile: `due_day` / `due_spread` / `amount_profile` / `confidence` from a
  known invoice fixture set.
- Warning eligibility: an EXPECT is and is not surfaced at each confidence level, at
  boundary dates.
- Vendor match tiers: each of the five outcomes in §6.3, including that 0.91 suggests and
  0.93 binds.
- Vendor clustering: the four genuinely-ambiguous cases in §6.5 must land in separate
  clusters, not be silently merged.
- Rollover idempotence: running twice produces one instance and preserves state.

---

## 12. Open questions

Recorded rather than guessed at. None block starting implementation; each needs an answer
before the affected piece ships.

1. **Autopay count mismatch.** SOP §10 says seventeen automatic debits, and "fifteen in an
   even month, fourteen in an odd month". The `Autopay Sweep` sheet has 20 rows. These do not
   obviously reconcile — Kenmore's "LADWP — three separate payments" may be one row expanding
   to three. Resolve before the VERIFY set is treated as complete.
2. **Recurring count mismatch.** SOP §5.2 says thirty-eight Yardi recurring entries totalling
   roughly $193,000 a month; the `Yardi Recurring Setup` sheet has 42 rows. Resolve during
   import.
3. **Property name mapping.** The SOP portfolio table, the workbook's `Close Tracker`, and
   the `properties` table use different names for the same accounts (`Calypte — 374 & 390
   Santa Rosa St` vs `Calypte, LLC`). The DB has 19 properties; the SOP describes 17 accounts
   across 9 ownership groups. A mapping table must be built and confirmed during import, not
   inferred.
4. **Ownership group is not modelled.** `properties` has no client/owner column, but nearly
   every SOP rule is stated per ownership group. Adding `properties.client` is almost
   certainly required; confirm the grouping against the SOP §1.1 table.
5. **SOP Appendix B, gaps 1–8** are carried into the Reference page verbatim and remain
   unanswered. Gap 4 in particular — no documented escalation when a reconciliation will not
   balance — is the one most likely to matter during a real close.

---

## 13. Suggested build order

The spec is one coherent system but too large to build in one pass. The dependency chain
gives a natural phasing; the implementation plan should follow it, and each phase should
leave the app working.

1. **Vendor identity** (§6) — schema, match pipeline, Fixer tab, clustering bootstrap.
   A prerequisite for everything else, and independently useful the day it ships: it fixes
   filenames and de-duplicates the invoice list even with no ledger present.
2. **Import** (§9) — reads the workbook and the Vendor Directory into the new tables.
   Resolves open questions 1–4 (§12) in the process, since they surface as import conflicts.
3. **Obligation ledger** (§4) — tables, window-rule parser, rollover, the Month page.
   ACTION and ASK work at this point; both are manual-tick, so no learning is needed yet.
4. **Recurrence learning** (§5) — profiles, EXPECT and VERIFY generation, evidence
   satisfaction, warning eligibility. This is where pains #1 and #3 get solved.
5. **Today page and Requests page** (§7) — the daily surface and the draft-email flow.
6. **API additions** (§8) — the four uses, plus structured outputs and the caching
   measurement. Last because each is an improvement to something already working, not a
   dependency of it.

## 14. What this deliberately does not do

- It does not gate. See §3.
- It does not send anything. See §3.
- It does not store a credential.
- It does not auto-clear a reconciliation, and does not let a model do so either.
- It does not build per-client packet checklists this round.
- It does not modify the existing extraction, staging, assembly, or reconciliation paths
  beyond adding `vendor_id` and the four API uses in §8.1. The pipeline that works today
  keeps working.
