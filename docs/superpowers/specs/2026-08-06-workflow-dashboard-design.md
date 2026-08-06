# Workflow Dashboard — Design

**Date:** 2026-08-06
**Revision:** v2 — incorporates a full technical review. See §15 for what changed and why.
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

- **Date normalization** — a parsed `invoice_date_iso` column with an explicit failure queue.
  Everything in §5 depends on it, so it lands first (§6).
- An obligation ledger: per-month instances of every recurring task, request, verification,
  and expected invoice.
- **Statement import and persistence** — a `statement_line` table, an import path, and an
  account↔statement mapping. `parse_statement` exists but is transient; VERIFY has nothing
  to query without this. It is a first-class component, not an adjustment.
- A recurrence-learning engine that derives expected invoices from history plus two
  authoritative lists.
- A vendor identity layer (canonical vendors + aliases) with a human verification queue.
- A **new** statement-line↔obligation matcher (§4.5). The existing document matcher cannot
  be reused; see that section for the measured reason.
- Draft-and-send email composition for the day-1 request batch and rent-posting notices.
- A one-time import that seeds all of the above from `Korus_Monthly_Close_System.xlsx`,
  `Vendor Directory - Active Only.xlsx`, and the existing invoice history.
- Four narrowly-scoped new uses of the Anthropic API (§9).

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
| Architecture | **Obligation ledger with evidence satisfaction** (§4). | Larger schema and a rollover step, bought for per-month history and zero-touch satisfaction of the two biggest pains. |

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
  date (§5.5). On a good day the list is empty.
- Severity escalates with age rather than by category.
- Month close does **not** demand a typed reason on every open item (§4.4). An earlier draft
  did, which would have imposed more friction than the three gates the user declined.

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
  property_id              -- nullable; some obligations are portfolio-wide
  vendor_id                -- nullable; set for EXPECT and VERIFY
  window_rule              -- see 4.3
  cadence                  -- monthly | even-months | odd-months | quarterly | irregular
  depends_on               -- nullable obligation id
  method                   -- check | EFT | ACH | autopay | n/a
  fixed_amount             -- nullable
  source                   -- authoritative | learned | manual
  confidence               -- high | medium | low; denormalized from the recurrence
                           -- profile (§5.3). Authoritative and manual are always 'high'.
                           -- Refresh trigger is defined in §5.4.
  active                   -- 0 | 1
  notes

  -- NOTE: no `client` column. Ownership group lives in exactly one place,
  -- properties.client_id (§12 open question 4), and is derived by join.
  -- Portfolio-wide obligations (property_id IS NULL) carry no client.

obligation_instance        -- one row per obligation per period
  id
  obligation_id
  period                   -- "August 2026", matching the existing settings.month format
  carried_from             -- nullable; the period this instance was first opened in.
                           -- NULL means it originated in `period`.
  due_from, due_to         -- resolved dates for this period
  state                    -- open | done | skipped | retired
  satisfied_by             -- nullable; see 4.2
  done_at
  sent_at, received_at     -- ASK only
  note                     -- optional; required only in the case defined in §4.4

  UNIQUE (obligation_id, period)
```

**The UNIQUE constraint is the idempotence mechanism.** §4.4 and §11 both claim rollover is
idempotent "by construction"; this constraint is that construction. It is not optional.

The four kinds, and why one table holds them:

- **ACTION** — you do it. Check runs, rent posting, distribution checks, saving statements.
  Satisfied by a tick.
- **ASK** — you request it from a person. The six day-1 requests. Satisfied by a tick, but
  carries `sent_at` / `received_at` so blocked downstream work can name who it is waiting on.
- **VERIFY** — confirm something happened without you. The autopay debits. Satisfied by
  **evidence**: a persisted statement line matching vendor, window, and amount profile.
- **EXPECT** — an invoice that should arrive. Satisfied by **evidence**: a matching invoice
  row landing in the database.

The leverage is `satisfied_by`. For EXPECT and VERIFY — the two kinds behind pains #1 and
#3 — nothing is ticked by hand. Everything still open is the answer to "what did I forget."

### 4.2 Evidence satisfaction

`satisfied_by` is one of:

| Value | Meaning |
|---|---|
| `tick` | a human marked it done |
| `invoice:<id>` | one invoice satisfies this EXPECT |
| `stmtline:<statement_line.id>` | one persisted statement line satisfies this VERIFY |
| `auto` | satisfied by a dependency rule, not direct evidence |

**One EXPECT is satisfied by at most one invoice, and vice versa.** The multi-account
allocation case (§9.1 item 2 — one LADWP bill covering five accounts) therefore produces
**five invoice rows**, one per account, not one row with five allocations. This keeps
`satisfied_by` single-valued and needs no join table. It also matches how the five accounts
are already handled downstream: separately, on separate spreadsheet lines.

**EXPECT** resolves continuously through the month, as invoices are processed. This is the
prospective case: by the 25th, the still-open EXPECT list *is* the missing-invoice list —
found before reconciliation instead of during it.

**VERIFY is retrospective**, and this must be stated plainly: bank statements arrive Days
1–5 for the *prior* month (SOP §2), so a debit expected on the 28th is not verifiable from a
statement until roughly five weeks later. The design accepts this rather than pretending
otherwise:

- A VERIFY instance sits `open`, displayed as "awaiting statement", until the covering
  statement is imported. It is not in the warnings list during that window, because there is
  nothing the user could do about it.
- Once the statement is imported, unmatched VERIFY instances become warnings immediately —
  that is the moment the information is actionable.
- A manual tick is always available for a user who checked the bank site directly.

**The missing-statement backstop.** "Awaiting statement" with no clock would reproduce pain
#3 exactly: if a statement is never imported — skipped month, or the parse failure §10
anticipates — every VERIFY behind it stays silent forever. The backstop needs no new
mechanism, because the SOP already contains the obligation: *"Days 1–5: download and save
every bank statement"* is an ACTION obligation imported from the Monthly Calendar, one per
account. If no statement covering period P is imported by the end of P+1, **that ACTION goes
overdue and warns** — about the missing statement, which is the actionable fact, not about
the debit. VERIFY instances behind it display "blocked: statement for <period> not imported"
and name the overdue ACTION.

### 4.3 Statement persistence

`parse_statement` (`core/bankrec.py:339`) is called from exactly one place —
`build()` at `core/bankrec.py:1145` — and its output is transient, in-memory, and scoped to
one packet for one property. `StmtLine` (`core/bankrec.py:294`) uses `__slots__`, has no
identity field, and its `seq` is positional, so it changes if the PDF is re-parsed. Nothing
survives the run.

VERIFY needs statement lines to be queryable months later, so they must be persisted:

```
statement                  -- one imported statement document
  id
  property_id
  period                   -- the period the statement COVERS, not when it was imported
  source_file
  imported_at
  UNIQUE (property_id, period)

statement_line
  id
  statement_id
  line_hash                -- stable content hash; see below
  amount, sign, section, txn_date, description, check_no, is_settlement
  UNIQUE (statement_id, line_hash)
```

`line_hash` is a digest over the **content-bearing** fields — normalized description, amount,
sign, date, check number — deliberately excluding `seq` and `pos`, which are positional and
unstable across re-parses. Re-importing the same statement is then idempotent, and a
`satisfied_by = 'stmtline:<id>'` reference stays valid.

Import is additive and safe to re-run: existing lines match on `(statement_id, line_hash)`
and are left alone. Two genuinely identical lines in one statement (same vendor, same
amount, same day) collide on the hash; the importer appends an occurrence index to
disambiguate rather than dropping the second.

### 4.4 Window rules and cadence

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
| `learned` | | Derived from history (§5.3); resolves to median day ± spread |

`cadence` is a separate field. **Learned cadence is restricted to `monthly` or `irregular`
until there is enough history to justify more.** The reason is in the data: there are two
complete months of history, June (even) and July (odd). A pair seen only in June is
indistinguishable from even-months, quarterly, and one-off. Even/odd detection is gated
behind **n ≥ 4 observations spanning ≥ 4 distinct months**, which the database will not
satisfy until roughly October 2026.

This matters because SOP §10 states LADWP bills Lisa Ahn and Sunggwang on even months and
Monette on odd months, and a monthly-only model would raise a false missing-invoice warning
for half of those every month. Until the gate opens, **every even/odd obligation comes from
the authoritative Autopay Sweep import**, never from learning. The detector is built, and is
correct, and simply does not fire on real data yet.

### 4.5 The statement-line matcher

§4.2 requires matching a persisted statement line to a VERIFY obligation. **The existing
document matcher cannot be reused for this**, and the reason is structural rather than
incidental:

- `score_doc_line` (`core/bankrec.py:566`) scores a *support document* against a line.
  A VERIFY has no document — only a vendor, a window, and an amount profile. The fields the
  scorer reads (`d.slip_total`, `d.content_amounts`, `d.fname_moneys`, `d.check_numbers`) do
  not exist for it.
- The weights are wrong for this job: amount scores 6, check number 8, vendor **2**.
- `assign_docs` (`core/bankrec.py:603`) is greedy and 1 document : 1 line, and holds out
  future-dated documents.
- Decisively, pass 2 (`core/bankrec.py:681` and `:785`) requires
  `set(why) - {"vendor", "date", "date~"}` — it *explicitly rejects* vendor-and-date-only
  evidence. For an autopay VERIFY, vendor plus amount window is exactly and only the
  evidence available.

So this is a **new matcher**, priced as its own unit of work, not a call into existing code.
It scores (vendor identity match, amount against the profile, date within the window) and
returns a confidence. Only high-confidence matches auto-satisfy; anything weaker is
presented for confirmation. The existing document matcher is untouched — the packet
assembly path that works today keeps working.

### 4.6 Period rollover and carry-forward

Creating instances for a new period is an explicit, idempotent operation, not a background
job. It is triggered when the current period changes on the Settings page, and is available
as a manual "open month" action.

- **Idempotent**, enforced by `UNIQUE (obligation_id, period)` (§4.1): re-running inserts
  only missing instances and never resets state on an existing one.
- **Carry-forward creates a new row, and does not mutate the old one.** An instance still
  `open` when its period closes is left in place, keeping the per-month history the ledger
  was bought for. The new period gets its own row with `carried_from` set to the *original*
  period — which propagates, so an item open for three months carries its first period, not
  its second. The pair `(obligation_id, period)` stays unique; the two rows differ by period,
  so there is no collision.
- **Age is computed from `carried_from`**, which is what drives severity escalation (§3).

**Closing a month does not require a typed reason on every open item.** The instance count is
roughly 42 recurring + 20 autopay + ~49 learned + ~40 ACTION/ASK ≈ 150 per month, and §5.3
says confidence is explicitly low at launch, so open-at-close counts will start high.
Demanding dozens of free-text boxes would be heavier friction than the three checkpoint gates
the user declined, and the predictable result is a column of "x". Instead:

- A typed reason is required **only** for open instances at `high` confidence — the ones the
  system is genuinely confident should have happened. That set is small.
- Everything else is bulk-dismissed in one action, recorded as `skipped` with no note.

---

## 5. Dates and recurrence learning

### 5.1 Date normalization (prerequisite)

`invoices.invoice_date` is free text (`core/db.py:72`). Measured against the live database:

```sql
-- reproduce: the figures below drift as invoices are processed
SELECT COUNT(*) FROM invoices;                        -- 265 on 2026-08-06
```

Of 265 rows, 250 are `M/D/YYYY`-shaped, 13 are other shapes, 2 are empty. The existing
parser, `state._parse_date_any` (`state.py:35`), resolves 11 of the 13 outliers. Four rows
in total fail: `06262026` (no separators), `13-Jul-26` (DD-Mon-YY, not in the format list),
and the 2 empties.

Three problems, none of which is the failure *count*:

1. **Failures are silent.** `_parse_date_any` returns `None` and callers drop the row. A
   dropped row is indistinguishable from a vendor who skipped a month — which is precisely
   the signal this system exists to detect.
2. **An unstated assumption is doing real work.** `07-01-2026` and `08-01-2026` are
   genuinely ambiguous between MM-DD-YYYY and DD-MM-YYYY. The parser silently picks
   MM-DD-YYYY. That is the right default for US invoices, but it is currently an accident of
   format ordering rather than a decision.
3. **It is untested.** No case in `tests/` covers date parsing, and it is now the
   highest-risk pure function in the system.

Therefore, before any learning work:

- Add `invoices.invoice_date_iso TEXT` (`YYYY-MM-DD`, empty when unresolved) and backfill it.
- Populate it at write time in the processor and on every edit path.
- **Every timing decision reads `invoice_date_iso`.** Nothing re-parses free text at read
  time, and nothing keys a due window off `date_processed` (§5.2).
- Rows that fail parsing go to an explicit **date review queue** — same pattern as Needs
  Review — showing the raw string with a date picker. Four rows today; the queue exists so
  the number stays visible rather than becoming an invisible drift.
- The MM-DD-YYYY preference for ambiguous `NN-NN-YYYY` is written down as a decision, and
  covered by a test.

### 5.2 What the data supports

Measured on 2026-08-06 using `state._parse_date_any`:

```
Months of history (by invoice date):
   2026-05   25      (partial)
   2026-06   91      (complete)
   2026-07  108      (complete)
   2026-08   23+     (in progress)

(property, vendor) pairs recurring across >=2 distinct months:   49
```

Day-of-month spread across those recurring pairs:

```
   invoice_date    median spread  2.0 days   (30 of 49 pairs within a 3-day window)
   date_processed  median spread 16   days   (14 of 47 that tight)
```

**This is the finding the timing model rests on**, and it is stable: re-measured under both
a narrow parser and the app's own parser, the median spread is 2.0 days either way (48 vs 49
pairs, 29 vs 30 within three days). `date_processed` records when the user got to the
invoice — batching makes it noise. `invoice_date` records when the vendor billed.

Amount stability is also learnable and varies widely: some pairs are fixed
(`rolling greens` at 790.50, `athens / 10630 Santa Monica` at 1,125.06), some tight
(`mitsubishi` cv 0.03, `west coast maintenance` cv 0.10), some genuinely variable
(`LADWP / Sunggwang` cv 2.25).

**Two caveats that must survive into implementation:**

- **These are pre-merge figures.** Pairs were keyed on *normalized raw vendor strings*, not
  on merged vendor identities. §6.5 collapses 95 raw strings to 82 clusters, and vendor
  identity ships first (§13). The pair count and the profiles **must be re-measured after
  Phase 1**; some pairs currently reading `irregular` will become `monthly` once their two
  spellings merge. This is an argument for the design, not against it — but 49 is a
  pre-merge number and should not be quoted as settled.
- **Two complete months is thin.** At launch the learned half is modest and low-confidence,
  and the two authoritative lists carry the load. Confidence tightens every month the system
  runs. This is a stated property, not a defect to work around.

### 5.3 The recurrence profile

Computed per (property_id, vendor_id) pair from `invoice_date_iso`:

| Field | Derived from |
|---|---|
| `months_seen`, `consecutive_months` | distinct year-months |
| `cadence` | `monthly` \| `irregular`; even/odd and quarterly only past the §4.4 gate |
| `due_day`, `due_spread` | median and range of day-of-month |
| `amount_profile` | `fixed` \| `tight` (cv < 0.15) \| `variable`, with last value and range |
| `confidence` | **high**: n ≥ 3 and spread ≤ 3 · **medium**: n ≥ 3, spread ≤ 10 · **low**: otherwise |

### 5.4 Profile refresh

Profiles and the `obligation.confidence` denormalization are recomputed at three explicit
moments, and at no other time:

1. **On rollover** (§4.6), for the period being opened.
2. **After a vendor merge or split**, for the affected pairs only — since a merge is exactly
   what changes a pair's history.
3. **On demand**, from a "recompute" action on the Expected page.

Refresh updates `confidence` and the *future* resolution of `learned` windows. It does
**not** rewrite `due_from` / `due_to` on instances that already exist, which keeps §4.6's
"never resets state on an existing instance" true. A mid-month improvement in learning takes
effect next month.

### 5.5 Precedence and warning timing

Three sources feed the EXPECT and VERIFY sets, in order:

1. **Authoritative** — the `Yardi Recurring Setup` rows (fixed amounts and methods) and the
   `Autopay Sweep` rows (explicit windows, and the *only* source of even/odd cadence until
   the §4.4 gate opens). These win on every field they specify.
2. **Learned** — the recurring pairs from history. Fills everything the lists do not name.
3. **Manual** — anything the user adds or mutes.

Every obligation displays its source, so "why is this asking me?" always has an answer.

An EXPECT enters the warnings list only when `today > due_to + slack`:

- **high** → slack 2 days. Surfaces on or near the real due date.
- **medium** → slack 7 days.
- **low** → does not surface until the last week of the period.

This is what makes warn-only viable. Flagging every not-yet-seen pair early in the month
would be noise; most legitimately arrive later.

### 5.6 Drift in both directions

An expectation list that only grows becomes noise within a year.

- **Promotion:** a (property, vendor) pair seen in two consecutive months and not already an
  obligation is proposed automatically, badged `new`. The user keeps or mutes it.
- **Retirement:** an obligation missed in two consecutive periods prompts *"retire this?"*
  rather than warning a third time. Retiring sets `active = 0`; history is kept.
- **Retirement excludes `low` confidence and `irregular` cadence.** Otherwise the rule eats
  exactly what the system exists to catch: a quarterly vendor mis-read as monthly is missed
  twice by construction and would be retired after two months. Those obligations are never
  auto-proposed for retirement; they surface in the last week (§5.5) and stay.

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

Clustering the existing invoices (normalize, then `difflib` ratio ≥ 0.86) yields:

```
   95 distinct raw vendor strings
   82 clusters
      69 singletons        -- auto-accept, short name suggested
      13 need a decision   -- of which 12 are pure case differences
```

Roughly fifteen minutes, once. Only four are genuinely ambiguous and need judgment rather
than a rubber stamp:

- `Michelle Suh` vs `Michelle Suh (Rooter Plumbing)`
- `James Chin` vs `James Chin (Stamp Reimbursement)`
- `South Coast Mechanical, LLC` vs `South Coast Mechanical, Inc.`
- `City of Los Angeles` vs `City of Los Angeles, Department of Public Works, Bureau of
  Sanitation`

### 6.6 Merges never rewrite filed PDFs

`_build_processed_filename` (`core/processor.py:648`) bakes the vendor short name into
`VendorShort_MM_YYYY.pdf`, and `stored_file` is the sidecar and assembler join key
(`core/db.py:45`), is indexed, and has already been copied into `data/Bank Rec/<month>/`
staging folders. It is currently rewritten only when a property changes (`app.py:344`,
`app.py:455`).

**A vendor merge must never rewrite `stored_file` on an already-filed invoice.** Doing so
would break the sidecar join and orphan copies already staged. §10's claim that a bad merge
is recoverable is true in the database and false on disk; this is the boundary.

The consequence for §13 Phase 1 is that its filename benefit is **forward-only**: newly
processed invoices get correct short names, because `match_vendor_short_name` will finally
have a populated map to consult. Already-filed invoices keep the names they have. That is
still a real improvement — today that map has 3 rows and effectively never fires — but it is
not retroactive, and the phase's value should not be sold as if it were.

### 6.7 The abbreviation gap

String distance will never join `LADWP` ↔ `Los Angeles Department of Water and Power`. The
invoices print the full name; the SOP and the Autopay Sweep both say `LADWP`. Two fixes:

1. Seed aliases from the SOP portfolio table, the workbook sheets, and the Vendor Directory
   during import.
2. For a genuinely new vendor, ask Claude for canonical name, short name, and likely
   abbreviations (§9). A handful of calls a month, not one per invoice.

---

## 7. Pages

Existing pages keep their jobs. New and changed:

| Page | Role |
|---|---|
| **Today** (new home) | What is due now, what is overdue, what is blocked and on whom. The single warnings surface. Replaces the current Dashboard as the landing page; the current stat cards move here. |
| **Month** | The calendar grid for the current period — every obligation, its window, its state. Per-account progress. Replaces the `Close Tracker` and `Monthly Calendar` sheets. |
| **Expected** | The EXPECT and VERIFY sets: what should arrive, what has, what has not, and each item's source and confidence. Where obligations are added, muted, retired, and where profiles are recomputed (§5.4). |
| **Statements** | Import a statement, see which period and account it covers, and which VERIFY instances it resolved. |
| **Requests** | The six ASK obligations: draft, mark sent, mark received, see what each is blocking. |
| **Reference** | Read-only: property procedures (SOP §9), the credentials index (names only, no values), and the Appendix B gaps register. |
| **Fixer** | Gains a second tab for vendor review (§6.4) and a third for the date review queue (§5.1). |
| **Vendors** | Gains canonical name, short name, aliases, active. |
| **Settings** | Gains the period-rollover action. |

Design language is already settled and is not revisited: IBM Plex Sans/Mono, `#3a5bd9`
accent, 236px sidebar, 12px cards, dark mode via `prefers-color-scheme`. New pages inherit
`base.html` and the tokens in `static/style.css`, which is the source of truth.

---

## 8. Import and bootstrap

**Dry-run is the default.** §13 Phase 2's stated job is resolving open questions 1–4 (§12)
*through* import conflicts, which is iterative — the importer will be run repeatedly. So:

- Default behaviour parses everything, reports what it would create, change, and conflict
  on, and writes nothing.
- `--apply` performs the writes. Writes **upsert on natural keys** (`obligation` on
  `(kind, title, property_id, vendor_id)`; `vendors` on `canonical_name`; `properties` on
  `canonical_name`) so a re-run converges rather than duplicating.
- There is no `--force` and no refuse-to-run-twice guard; those are the wrong shape for an
  iterative reconciliation tool. Safety comes from dry-run-by-default plus upsert.
- Conflicts that need a human decision (§12 items 1–4) are listed, not resolved silently.

| Source | Feeds |
|---|---|
| `Korus_Monthly_Close_System.xlsx` → `Monthly Calendar` | ACTION obligations, incl. the per-account "save the statement" backstop (§4.2) |
| → `Info Requests` | ASK obligations |
| → `Autopay Sweep` | VERIFY obligations; authoritative windows and even/odd cadence |
| → `Yardi Recurring Setup` | EXPECT obligations; authoritative amounts and methods |
| → `Close Tracker` | account list, cross-checked against `properties` |
| → `Bank Rec Packets` | stored for a later round; not surfaced in round one |
| `Vendor Directory - Active Only.xlsx` | vendor canonical names and aliases |
| `Monthly Bank Reconciliation Reports - Owners email list.xlsx` | recipients for draft emails |
| `data/invoices.db` | vendor clustering (§6.5), recurrence profiles (§5.3) |

Row counts for each sheet are deliberately not quoted here; they are reported by the
dry run, and §12 records the two places they disagree with the SOP's prose.

The SOP itself is not machine-read. Its property procedures (§9), credentials index
(App. A), and gaps register (App. B) are transcribed into the Reference page during
implementation.

---

## 9. Anthropic API usage

The app already calls Claude for invoice extraction (`claude-haiku-4-5`, ~110 invoices a
month). Four narrow additions, and an explicit list of places it must not be used.

### 9.1 Where it is used

1. **Bank statement descriptor → vendor.** Bank descriptors are semantically opaque
   (`ATHENS SERVIC DES:PAYMENT ID:8842 INDN:KORUS`); no regex maps that to `Athens Services`.
   Claude sees **only the leftovers** — statement lines the §4.5 matcher could not resolve —
   and **proposes**. It never auto-clears. This preserves the property the README already
   states: only strong matches are auto-reconciled.
2. **Multi-account utility bills.** SOP §9.1: one LADWP bill covers five accounts (Army,
   Navy, Marines, Media Center, Billboard), currently split by hand and copied into four
   spreadsheets. Reading one document and allocating it five ways is document understanding.
   Output is **five invoice rows** (§4.2), not one row with five allocations.
3. **New-vendor canonicalization.** Canonical name, short name, likely abbreviations, for a
   vendor with no alias match (§6.7). Human still confirms.
4. **Amount anomaly *explanation*.** Detection is a range check against the learned amount
   profile — arithmetic. The explanation ("SCE Beach is 4,180 against a usual 19,572–34,396")
   requires reading the PDF: partial period, credit applied, meter re-read. Advisory only, so
   a wrong answer costs nothing.

### 9.2 Where it is not used

| Task | Use instead |
|---|---|
| Is this autopay / EFT / check? | Vendor attribute — a lookup |
| What is missing this month? | Set difference |
| Monthly vs even-month vs odd-month? | Statistics on `invoice_date_iso` — deterministic, unit-testable |
| Is this amount anomalous? | Range check against the amount profile |
| Parsing a date | A parser with an explicit failure queue (§5.1). Never a model. |
| The six day-1 emails | Templates with variables. They are the same six every month; a model adds variance, not value. Have Claude write the templates once, then never call it again. |
| Clearing a reconciliation, or releasing a payment | Never. Nondeterminism on money is a bad trade. |

### 9.3 Model choice, and one upgrade

- **Haiku 4.5** stays for bulk extraction — high volume, well-scoped.
- **Sonnet 5** for the judgment calls (9.1 items 1 and 2): low volume, higher stakes.
- **Structured outputs.** `core/processor.py:262` catches `json.JSONDecodeError`, and
  `core/processor.py:256` strips markdown fences the model sometimes adds. Passing
  `output_config={"format": {"type": "json_schema", "schema": ...}}` removes both failure
  modes; supported on Haiku 4.5.

**Prompt caching is not available on the current call shape, and the reason is structural.**
`extract_invoice_data` (`core/processor.py:206`) passes **no `system` parameter at all** —
`grep` for `system=` across the codebase returns nothing — and builds
`content = [document_block, text_block]` with the base64 PDF **first**
(`core/processor.py:217`). Caching is a prefix match, and the prefix here is a different
document on every call, so the hit rate is zero regardless of token counts. Measuring with
`count_tokens` would report a healthy number against a design that can never hit.

Enabling it requires a change to the call shape: move the stable instructions and property
list into the `system` parameter (or, less cleanly, reorder to text-then-document). Only
then does the 4096-token minimum for Haiku 4.5 become the relevant question. This is
recorded as available future work, not as a task in this round — the reordering interacts
with extraction accuracy and deserves its own measurement.

Cost is not the constraint: extraction runs roughly $1/month today, and the additions are
low-volume. Correctness is the constraint, which is what §9.2 is for.

---

## 10. Failure modes and error handling

- **A bad vendor merge.** Recoverable *in the database*, because raw `vendor_name` is
  preserved on every invoice; an un-merge re-runs the match pipeline for affected rows.
  **Not recoverable on disk, because it never touches disk** — see §6.6.
- **A date that will not parse.** Goes to the date review queue (§5.1) and is visible.
  It is never silently dropped, and never counted as a skipped month.
- **A statement that will not parse.** The assembler reports it, as today. VERIFY instances
  for that account stay "blocked", and the per-account "save the statement" ACTION goes
  overdue and warns (§4.2) — so the failure is loud rather than silent.
- **Re-running rollover.** Idempotent via `UNIQUE (obligation_id, period)` (§4.1).
- **Re-importing a statement.** Idempotent via `UNIQUE (statement_id, line_hash)` (§4.3).
- **Re-running the workbook import.** Dry-run by default; writes upsert (§8).
- **A learned expectation that is simply wrong.** Muting is one click from the Expected page.
  Muting is preferred over deleting so history stays intact.
- **Windows file locks.** Unchanged — the app names the blocking file and never half-applies
  a change.

---

## 11. Testing

Extends the existing `tests/` suite (26 unittest cases, no DB or network required). New
cases, all pure functions with fixture data:

- **Date normalization (§5.1)** — highest-risk function in the system, currently untested.
  Every shape present in the live data: `M/D/YYYY`, `MM/DD/YY`, `YYYY-MM-DD HH:MM:SS`,
  `Mon D, YYYY`, `Mon/DD/YY`, `MM-DD-YYYY`, `DD-Mon-YY`, `MMDDYYYY`, and empty. Ambiguous
  `NN-NN-YYYY` must resolve MM-DD-YYYY and that must be asserted, not incidental.
  Unparseable input must return a failure marker that reaches the review queue — never
  `None` silently swallowed by a caller.
- Window-rule parsing: every form in §4.4, across month lengths, including February and
  months where "week 2" and "the 8th–14th" diverge.
- Cadence detection: `monthly` and `irregular` on real fixtures. Even/odd and quarterly are
  tested **behind the §4.4 gate** with synthetic ≥4-month fixtures, and a test asserts the
  detector does **not** classify even/odd on a 2-month fixture — that guard is the thing that
  can actually regress before October.
- Recurrence profile: `due_day` / `due_spread` / `amount_profile` / `confidence` from a
  known invoice fixture set.
- Warning eligibility: an EXPECT is and is not surfaced at each confidence level, at
  boundary dates.
- Vendor match tiers: each of the five outcomes in §6.3, including that 0.91 suggests and
  0.93 binds.
- Vendor clustering: the four genuinely-ambiguous cases in §6.5 must land in separate
  clusters, not be silently merged.
- `line_hash` stability: the same statement re-parsed produces identical hashes; two
  identical lines in one statement disambiguate rather than collide (§4.3).
- Statement-line matcher (§4.5): vendor + amount + window satisfies; vendor alone does not.
- Rollover idempotence: running twice produces one instance and preserves state.
- Carry-forward: `carried_from` propagates the *original* period across three periods, and
  the old instance is not mutated (§4.6).

---

## 12. Open questions

Recorded rather than guessed at. None block starting implementation; each needs an answer
before the affected piece ships. Items 1–4 are expected to surface as dry-run conflicts
during Phase 2 (§8).

1. **Autopay count mismatch.** SOP §10 says seventeen automatic debits, and "fifteen in an
   even month, fourteen in an odd month". The `Autopay Sweep` sheet has more rows than that.
   Kenmore's "LADWP — three separate payments" may be one row expanding to three. Resolve
   before the VERIFY set is treated as complete.
2. **Recurring count mismatch.** SOP §5.2 says thirty-eight Yardi recurring entries totalling
   roughly $193,000 a month; the `Yardi Recurring Setup` sheet has more rows.
3. **Property name mapping.** The SOP portfolio table, the workbook's `Close Tracker`, and
   the `properties` table use different names for the same accounts (`Calypte — 374 & 390
   Santa Rosa St` vs `Calypte, LLC`). The DB has 19 properties; the SOP describes 17 accounts
   across 9 ownership groups. The mapping must be built and confirmed, not inferred.
4. **Ownership group is not modelled.** `properties` has no client column, but nearly every
   SOP rule is stated per ownership group. §4.1 commits to a single home for this fact —
   `properties.client_id` with a `clients` table — rather than denormalizing it onto
   `obligation`. Confirm the grouping against the SOP §1.1 table.
5. **SOP Appendix B, gaps 1–8** are carried into the Reference page verbatim and remain
   unanswered. Gap 4 in particular — no documented escalation when a reconciliation will not
   balance — is the one most likely to matter during a real close.

---

## 13. Build order

The spec is one coherent system but too large to build in one pass. The dependency chain
gives a natural phasing; each phase leaves the app working.

0. **Date normalization** (§5.1) — `invoice_date_iso`, backfill, write-path population,
   review queue, tests. Small, and everything in §5 is wrong without it.
1. **Vendor identity** (§6) — schema, match pipeline, Fixer tab, clustering bootstrap.
   A prerequisite for the learning engine. Independently useful the day it ships: it
   de-duplicates the invoice list, and gives newly-processed invoices correct short names
   (forward-only — see §6.6).
2. **Import** (§8) — dry-run first, reads the workbook and Vendor Directory into the new
   tables. Resolves open questions 1–4 in the process. Re-measure §5.2's pair statistics
   here, now that vendor identities are merged.
3. **Obligation ledger** (§4.1, §4.4, §4.6) — tables, window-rule parser, rollover,
   carry-forward, the Month page. ACTION and ASK work at this point; both are manual-tick,
   so no learning is needed yet.
4. **Statement persistence** (§4.3) — `statement` / `statement_line`, the import path, the
   Statements page, `line_hash`. Independent of learning, and pain #3 does not exist without
   it.
5. **Recurrence learning** (§5) — profiles, EXPECT generation, invoice-side evidence
   satisfaction, warning eligibility. Solves pain #1.
6. **The statement-line matcher** (§4.5) — VERIFY satisfaction, the missing-statement
   backstop. Needs both 4 and 5. Solves pain #3.
7. **Today page and Requests page** (§7) — the daily surface and the draft-email flow.
8. **API additions** (§9) — the four uses, plus structured outputs. Last because each
   improves something already working rather than being a dependency of it.

---

## 14. What this deliberately does not do

- It does not gate. See §3.
- It does not send anything. See §3.
- It does not store a credential.
- It does not auto-clear a reconciliation, and does not let a model do so either.
- It does not use a model to parse a date.
- It does not rewrite filenames on already-filed invoices. See §6.6.
- It does not build per-client packet checklists this round.
- It does not enable prompt caching; §9.3 records why that needs a call-shape change first.
- It does not modify the existing extraction, staging, assembly, or reconciliation paths
  beyond adding `vendor_id`, `invoice_date_iso`, and the four API uses in §9.1. The pipeline
  that works today keeps working, and the existing document matcher is untouched.

---

## 15. Revision history

**v2 (2026-08-06)** — technical review of v1. Sixteen findings, all verified against the
code and the live database before being accepted. Material changes:

| # | Finding | Change |
|---|---|---|
| 1 | `invoice_date` is unparsed free text; timing model rests on it | New §5.1 and Phase 0. Verified: 4 of 265 rows fail the app parser; failures are silent; the MM-DD-YYYY resolution of ambiguous input was an unstated accident. **Partially disputed** — the v1 finding was re-measured under both parsers and is stable at a 2.0-day median spread, so it was reproducible; the hazard is silent drops and the absence of tests, not a broken premise. |
| 2 | Prompt caching cannot work | §9.3 rewritten. Verified worse than reported: there is no `system` parameter anywhere, and the base64 document is content block 0. The v1 remedy measured the wrong thing. Item removed from scope. |
| 3 | Statements are never stored | New §4.3, new Statements page, new Phase 4. `parse_statement` has two call sites, both transient. Largest unpriced item in v1. |
| 4 | The existing ladder is not reusable | New §4.5. Verified: vendor scores 2 vs amount 6, and pass 2 explicitly rejects vendor-and-date-only evidence — exactly what a VERIFY has. |
| 5 | "Awaiting statement" has no clock | §4.2 backstop. Uses the *existing* per-account "save the statement" ACTION rather than new machinery. |
| 6 | Carry-forward contradicts the schema | §4.1 gains `carried_from` and `UNIQUE (obligation_id, period)`; §4.6 specifies new-row-not-mutate. |
| 7 | Typed-reason-per-item is heavier than the rejected gates | §4.6 scopes it to `high` confidence; everything else bulk-dismisses. |
| 8 | Pair statistics are pre-merge | §5.2 caveat; re-measurement added to Phase 2. |
| 9 | Even/odd is not learnable from two months | §4.4 gate (n ≥ 4 over ≥ 4 months); authoritative-only until then; §11 tests the guard rather than a detector that cannot fire. |
| 10 | Retirement eats what you're trying to catch | §5.6 excludes `low` and `irregular`. |
| 11 | Merges must not rewrite filed filenames | New §6.6. Phase 1's filename benefit restated as forward-only. |
| 12 | Two homes for ownership group | §4.1 drops `obligation.client`; §12.4 commits to `properties.client_id`. |
| 13 | `confidence` refresh never scheduled | New §5.4 naming three triggers. |
| 14 | Multi-allocation conflicts with `satisfied_by` | §4.2 commits to five invoice rows. |
| 15 | Import guard is the wrong shape for iterative use | §8 makes dry-run the default with upsert-on-natural-key. |
| 16 | Figures drift | §5.2 cites the query; counts removed from §8. |
