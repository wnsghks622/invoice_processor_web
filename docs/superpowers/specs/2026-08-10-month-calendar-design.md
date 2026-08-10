# Month Calendar and Reminders — Design

**Date:** 2026-08-10
**Status:** Approved for planning
**Parent spec:** `docs/superpowers/specs/2026-08-06-workflow-dashboard-design.md`
**Depends on:** the data foundation (that spec's Phases 0–1), merged to `main` at `ba6f1f8`

---

## 1. What this is

A per-month page answering two questions: *which invoices that normally arrive haven't
arrived yet*, and *what did I tell myself to do this month*.

It implements a deliberately narrow slice of the parent spec — the obligation ledger (§4.1),
recurrence learning (§5), and the Month page (§7) — and defers the rest of that spec's
Phase 2 and 3. Specifically **out of scope here**: the workbook import, the six day-1
information requests, check runs, rent posting, statement persistence, and autopay
verification. Those remain specified in the parent and are additive; the ledger this builds
is the same one they will land in, so adding them later is data entry rather than rework.

Two requirements arrived during design that the parent spec does not cover, and they are the
substance of §4 and §5 below: user-authored reminders, and vendors that bill only when work
is done.

## 2. Why now

The data foundation shipped the two columns this depends on. Measured on the live database
on 2026-08-10:

```
275 invoices — 273 parse to invoice_date_iso, 2 need review (both genuinely blank)
```

And measured after vendor identities were merged (parent spec §5.2):

```
49 recurring (property, vendor) pairs across >=2 distinct months
   invoice_date    median day-of-month spread  2.0 days   (30 of 49 within a 3-day window)
   date_processed  median day-of-month spread 15   days   (17 of 49 that tight)
```

That contrast is what makes this feasible. Vendor billing dates are regular enough to
predict; the dates you process them on are not. Every timing decision here reads
`invoice_date_iso` and nothing reads `date_processed`.

**Note on database state:** the three post-merge scripts (`backfill_dates.py`,
`bootstrap_vendors.py`, `backfill_vendors.py`) have **not** yet been run against the live
database. Until they are, it holds 3 vendors and no populated `invoice_date_iso` or
`vendor_id`. Nothing in this spec works before that runbook is executed; the README
documents it.

## 3. Decisions

Settled during brainstorming. Not open for re-litigation during planning.

| Decision | Choice | Consequence |
|---|---|---|
| Scope | Missing invoices + reminders only. No workbook import. | Nothing blocks on the parent spec's four open questions. Ships the daily value first. |
| Model | Reminders are manual obligations on the shared ledger, not a separate table. | One model, one rollover, one page rendering. Adding the workbook later is data entry. |
| Missing detection | Confidence-gated slack (§6.3), inherited from parent §5.5. | A quiet list on a good day, which is what keeps it readable. |
| Irregular vendors | Explicit `on-demand` cadence that never generates an instance. | The single most important thing preventing this from becoming noise. |
| Date column | Display `invoice_date_iso` (ISO), raw string in a tooltip. | Sortable, unambiguous, provenance preserved. |

### Why not compute the missing list on the fly

A live query needs no schema and ships faster. It was rejected because it cannot remember
anything. Two things must persist:

- **Dismissal.** When a vendor legitimately skips a month, you say so once. A computed list
  re-raises the same false positive every day until the invoice arrives, which is exactly
  the warning fatigue the parent spec's §3 note is about.
- **History.** Opening July and seeing what was missing *then* is not reconstructable from
  today's data once the invoices eventually land.

---

## 4. Data model

Two tables, matching parent spec §4.1. This builds the subset the current scope needs; the
columns it omits (`depends_on`, `method`, `fixed_amount`) arrive with the workbook import
and are additive.

```
obligation                 -- the template
  id
  kind              -- EXPECT | ACTION        (VERIFY and ASK arrive with the workbook)
  title
  property_id       -- nullable, FK properties.id
  vendor_id         -- nullable, FK vendors.id
  window_rule       -- see 6.2
  cadence           -- monthly | on-demand | irregular | once
                    -- even-months | odd-months | quarterly are defined but gated (6.4)
  source            -- learned | manual
  confidence        -- high | medium | low. Manual is always high.
  active            -- 0 | 1
  notes

obligation_instance        -- one row per obligation per period
  id
  obligation_id
  period            -- "August 2026" — matches settings.month's "%B %Y" exactly
  due_from, due_to  -- resolved dates for this period, inclusive
  state             -- open | done | skipped
  satisfied_by      -- nullable; 'tick' | 'invoice:<id>'
  done_at
  note              -- free text; carries the reason on a skip
  UNIQUE (obligation_id, period)
```

`UNIQUE (obligation_id, period)` is the idempotence mechanism for rollover (§8), not an
incidental constraint. Both tables are added through `db._ADDED_COLUMNS` /
`_ensure_columns()` and the `_SCHEMA_TABLES` block, per the migration discipline the data
foundation established — `_SCHEMA` uses `CREATE TABLE IF NOT EXISTS` and will not alter an
existing table.

New query functions take an optional trailing `conn=None` routed through `db._conn_or`, so
they are testable against an in-memory database without a file on disk.

### 4.1 Why reminders are obligations

A reminder is a thing that is due in a window and either happens or does not. That is what
an obligation is. `source='manual'` already exists in the parent spec's model precisely for
this. Giving reminders their own table would mean two rollovers, two rendering paths, and a
later migration to merge them.

The only genuinely new mechanism is one-off scheduling (§5.1), because every cadence in the
parent spec recurs.

---

## 5. Reminders

`kind='ACTION'`, `source='manual'`, `confidence='high'`.

**Recurring:** `cadence='monthly'` with a relative window — `day:12`, `week:3`, `last-week`,
`month-end`. Appears in every period from creation onward, like a learned expectation.

**One-off:** `cadence='once'` with an absolute window, a new rule form: `date:2026-08-12`.

**Attachment:** setting `property_id` or `vendor_id` files the reminder into that account's
group on the Month page, beside its expected invoices. Both null makes it portfolio-wide.

### 5.1 How one-off works

Rollover resolves `date:YYYY-MM-DD` to the period containing that date and generates an
instance only there. Every other period skips it. No `active` flag flipping, no cleanup pass.

The alternative — instance rows with a null `obligation_id` — was rejected because a nullable
foreign key forces every subsequent query and join to special-case it, forever, to save one
enum value now.

---

## 6. Expected invoices

`kind='EXPECT'`, `source='learned'`, one obligation per `(property_id, vendor_id)` pair.

### 6.1 Generation

A pair qualifies at **≥2 distinct year-months** in `invoice_date_iso`. Profiles are computed
per parent spec §5.3:

| Field | Derived from |
|---|---|
| `due_day`, `due_spread` | median and range of day-of-month across observations |
| `confidence` | **high**: n ≥ 3 and spread ≤ 3 · **medium**: n ≥ 3, spread ≤ 10 · **low**: otherwise |
| `cadence` | `monthly` or `irregular` only — see below and §6.4 |

**`monthly` versus `irregular`** is decided by gaps, not by count: a pair is `monthly` when
no two consecutive observations are more than one month apart, and `irregular` otherwise.
So a vendor billing June and July is `monthly`; one billing March and July is `irregular`
even though both have two observations. `irregular` is the honest answer for "recurs, but
not on a schedule I can predict" — it still generates an instance, but only surfaces in the
last week (§6.4), because flagging it on a guessed date would be noise.

Learning never assigns `on-demand`. That classification only ever comes from you (§6.5) or
from the promotion choice (§6.6) — the system cannot tell a plumber who happened to bill
twice from a utility, and guessing wrong in that direction creates a permanent false
expectation.

Grouping is on `vendor_id`, never on the vendor string. That is what the vendor identity
layer was built for: without it, `Athens Services` and `ATHENS SERVICES` are two
half-confident expectations that each look sporadic.

### 6.2 Window rules

`window_rule` resolves to `(due_from, due_to)` for a period:

| Form | Example | Used by |
|---|---|---|
| `learned` | | EXPECT — resolves to median day ± spread |
| `day:N` | `day:1` | reminders |
| `day:N-M` | `day:28-30` | reminders |
| `week:N` | `week:3` | reminders |
| `last-week` | | reminders |
| `month-end` | | reminders |
| `date:YYYY-MM-DD` | `date:2026-08-12` | one-off reminders (§5.1) |

### 6.3 When something counts as missing

An instance is **missing** when it is still `open` and `today > due_to + slack`. Slack comes
from confidence:

- **high** → 2 days. Surfaces on or near the real date.
- **medium** → 7 days.
- **low** → does not surface until the last week of the period.

This is the mechanism that makes a warn-only design survivable. Flagging every not-yet-seen
pair early in the month would produce a list of twenty on the 6th, most of which arrive
later — and a list that is wrong twenty times is a list nobody reads.

### 6.4 Cadence, and the on-demand case

Roughly half the `(property, vendor)` pairs in the live data are not recurring at all —
locksmiths, lumber, fire testing. They bill when work is done. Treating those as monthly
expectations would generate a permanent false-missing list.

| Cadence | Generates an instance | Can be flagged missing |
|---|---|---|
| `monthly` | yes | yes, per §6.3 slack |
| **`on-demand`** | **no** | **never** |
| `irregular` | yes | only in the last week of the period |
| `once` | one period only | yes |
| `even-months` / `odd-months` / `quarterly` | yes | yes — **gated, see below** |

`on-demand` is a first-class classification, not a mute. The obligation stays visible on the
Expected page labelled *"on-demand — billed when work is done"*. A muted row looks like
something fell through; an on-demand row looks like a decision. Same silence, different
meaning to a reader six months later.

**Even/odd and quarterly are defined but never assigned by learning**, until a pair has
**≥4 observations spanning ≥4 distinct months**. The live history has two complete months,
June (even) and July (odd), so a pair seen only in June is indistinguishable from
even-months, quarterly and one-off. The detector is built and correct; it does not fire on
this data yet. Parent spec §10 records that LADWP genuinely bills Lisa Ahn and Sunggwang on
even months and Monette on odd — those will come from the Autopay Sweep import, not from
learning.

### 6.5 Editing

Each obligation has an edit form, reachable from its Month row and from the Expected page.
Editable: `cadence`, `window_rule`, `title`, `notes`.

**An edit sets `source='manual'`, which pins the obligation.** Profile recomputation (§8)
then leaves it alone. Learned values only ever overwrite values you have not touched — a
decision you made must not be silently reverted by the next recompute.

The form carries an **"apply to this vendor at every property"** checkbox. A plumber is
on-demand everywhere, and with 86 vendors, setting that pair by pair is a chore that would
not get done.

### 6.6 Promotion must not create false expectations

A pair becomes an expectation the moment it qualifies under §6.1 — two distinct months. That
is the same threshold, stated once: qualification and promotion are the same event, and
"promotion" is just what it is called the first time a pair crosses it.

The hazard is that a repair vendor billing in June and July qualifies as `monthly` under the
gap rule, and would then be flagged missing every month afterwards — the system teaching
itself to cry wolf.

So promotion does not silently create an expectation. A newly promoted pair appears badged
`new` with two one-click choices: **expect monthly** or **on-demand**. Until you choose, it
generates instances but is **never flagged missing**. A wrong guess by the system costs
nothing.

Retirement keeps parent spec §5.6's exclusion: `low` confidence and `irregular` cadence are
never auto-proposed for retirement, or the rule eats exactly the quarterly vendors it should
protect.

---

## 7. The Month page

New nav item between **Invoices** and **Month-end**. Period selector defaults to
`settings.month`; changing it is a view change and does not run rollover.

Rows group **by property**, with portfolio-wide items in an "All properties" group first.
Each row is one instance:

| State | Condition | Presentation |
|---|---|---|
| **Arrived** | `satisfied_by = 'invoice:<id>'` | ticked, dimmed, vendor links to the filed PDF |
| **Not yet due** | `today <= due_to + slack`, still open | quiet, shows the expected window |
| **Missing** | §6.3 | flagged — the answer to "what's possibly missing" |
| **Done** | ticked by you | ticked, shows `done_at` |
| **Skipped** | dismissed for this period | struck through, shows your note |

Every row shows its source and confidence — `learned from 5 months`, `you added this`,
`on-demand` — so "why is this asking me?" always has an answer on the row itself.

**Row actions:** tick done · skip for this period with a note · edit the obligation (§6.5).

**Page actions:** add a reminder · open the month (rollover, §8).

The page is a **checklist you browse**, not a warnings feed. The parent spec's Today page is
the warnings surface; this one is for looking over the month deliberately.

---

## 8. Rollover and refresh

Opening a period is explicit — a button, not a background job.

- **Idempotent**, enforced by `UNIQUE (obligation_id, period)`. Re-running inserts only
  missing instances and never resets state on an existing one.
- Resolves each active obligation's `window_rule` against the period, skipping `on-demand`
  entirely and skipping `once` obligations whose date falls outside.
- **Carry-forward is out of scope here**, and its `carried_from` column is deliberately
  absent from §4's schema rather than added-but-unused. Parent spec §4.6 defines the
  behaviour; this slice leaves instances in their own period. Adding the column later goes
  through `_ADDED_COLUMNS` like every other, so deferring it costs nothing.

**Profile recompute** (parent §5.4) runs at three explicit moments and no others: on
rollover for the period being opened; after a vendor merge or split, for affected pairs
only; and from a "recompute" action on the Expected page. It updates `confidence` and future
window resolution. It never rewrites `due_from` / `due_to` on instances that already exist,
and never touches an obligation with `source='manual'` (§6.5).

---

## 9. Date display

`templates/invoices.html:116` currently renders the raw `{{ inv.invoice_date }}`, so the
list shows `2026-06-03 00:00:00` beside `Jun 2, 2026` beside `6/11/26` — the parse already
happens, it is just never shown.

Render `invoice_date_iso` (ISO `2026-06-03`, sortable), with the raw string in a `title`
attribute so provenance stays one hover away. A row with no parsed date shows the raw text
marked as needing review, linking to the existing Fixer date queue.

The edit panel keeps editing the **raw** `invoice_date` field. That path already recomputes
`invoice_date_iso` on save and is covered by route tests.

---

## 10. Failure modes

- **The scripts have not been run.** With no `vendor_id` populated, no pair qualifies and the
  Month page is empty. It must say so plainly and point at the README runbook, not render a
  blank page that looks like "nothing is missing".
- **A vendor is merged after obligations exist.** Recompute runs for affected pairs (§8).
  Obligations against a retired `vendor_id` are deactivated, not deleted.
- **An invoice arrives for a skipped instance.** Evidence wins: the instance flips to
  arrived, and the skip note is kept for the record.
- **Two invoices match one instance.** First one satisfies it; the second is left unmatched
  rather than silently double-counted. Genuine duplicates are already the processor's job.
- **A period is opened twice.** Idempotent by constraint (§8).
- **Clock/period mismatch** — viewing August while `settings.month` says July. The page shows
  the period being viewed, and the rollover button names the period it will open.

## 11. Testing

Extends the existing suite (125 tests, `unittest`, no database file, no network, no
filesystem writes).

- **Window-rule resolution** — every form in §6.2 across month lengths, including February,
  a `date:` outside the period, and months where "week 3" and "the 15th–21st" diverge.
- **Missing eligibility** — an instance is and is not flagged at each confidence level, at
  boundary dates. `on-demand` is never flagged. `irregular` is flagged only in the last week.
- **Cadence gate** — a 2-month fixture must **not** be classified even/odd; a 4-month
  even-only fixture must be. The negative case is the one that can regress before October.
- **Rollover idempotence** — running twice produces one instance and preserves state.
  `on-demand` produces none. `once` produces exactly one, in the right period.
- **Pinning** — a recompute does not alter an obligation with `source='manual'`.
- **Promotion** — a newly promoted pair is not flagged missing before the user chooses.
- **Route tests** in `tests/test_app.py`, per the standing ruling that Flask handlers get real
  coverage rather than manual verification: add a reminder, tick, skip with a note, edit
  cadence, and the apply-to-every-property path. Reject-before-write on each.
- **Date display** — the list renders the parsed date and keeps the raw in `title`; an
  unparsed row still renders and links to the queue.

Every new test must be able to fail. Mutate the code it covers and confirm it does. Two
mutations survived the whole suite during the data foundation work; both were tests that
asserted something that could not break.

## 12. Open questions

1. **`properties` has no ownership-group column.** Grouping is by property here, which needs
   none. Adding client grouping later means parent spec §12.4's `properties.client_id`.
2. **Amount anomalies are not surfaced.** The profile can carry an amount range and the data
   supports it (some pairs are fixed to the cent, others vary 50×). Deliberately deferred —
   this slice answers "did it arrive", not "was it right".
3. **No notification exists outside the app.** Reminders are visible when you open the page.
   Whether that is sufficient is worth revisiting once it has been used for a month.
4. **Which format for the `date:` rule in the UI.** The stored form is ISO; the input should
   probably be a native date picker, matching the Fixer queue.

## 13. Build order

1. **Ledger schema and rollover** — both tables, the window-rule parser, the rollover
   operation. No UI. Testable in isolation and everything else depends on it.
2. **Expectation generation** — profiles from `invoice_date_iso` grouped on `vendor_id`,
   confidence bands, the cadence gate, EXPECT obligation creation.
3. **Evidence satisfaction** — an arriving invoice closes its instance.
4. **The Month page** — grouped rows, the five states, row actions.
5. **Reminders** — the add form, one-off and recurring, attachment.
6. **Cadence editing and on-demand** — the edit form, the pin, apply-to-every-property, the
   promotion choice.
7. **Date display** — the invoices list column and its tooltip.

Steps 1–3 have no UI and can be verified entirely by tests plus a read-only query against the
live database. Step 7 is independent of the rest and could ship first if the calendar work
stalls.

## 14. What this deliberately does not do

- No workbook import, no day-1 requests, no check runs, no rent posting, no statement
  persistence, no autopay verification. All specified in the parent; all additive.
- No carry-forward between periods (§8).
- No amount checking (§12.2).
- No notifications outside the app (§12.3).
- No gating. Warn-only, per the parent spec's standing decision.
- Nothing that writes to the live database without an explicit action — the same dry-run
  discipline the three existing scripts follow.
