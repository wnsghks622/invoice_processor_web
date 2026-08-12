# Month calendar — deferred findings

Raised by the whole-branch review of `worktree-month-calendar` (merged 2026-08-12, 285
tests). One Critical and three Importants were fixed before merge as Task 14; everything
below was deliberately deferred. Recorded here because the SDD ledger lived in the worktree
and does not survive it.

The review's own framing is worth keeping: **every one of these passed its per-task review.**
The Critical it found — learned billing dates computed, never stored, so every window spanned
the whole month and the page reported nothing missing on any day — sat in a hand-off between
two tasks that were each individually correct. The unit test for the arithmetic passed
`due_day` by hand from the profile dict, proving the maths while leaving the wiring untested.

## Important

1. **Quarterly anchor comes from the viewed period, not the observations.**
   `app.py`'s `edit_obligation` derives `anchor = month % 3` from whichever month is on
   screen. Spec §6.4.1 says the anchor is "derived from the observations, **never assumed
   from the calendar**". Setting a vendor quarterly while viewing August gives Feb/May/Aug/Nov;
   the same click in September gives Mar/Jun/Sep/Dec. An off-anchor month generates no
   instance at all, so a wrong anchor silently stops watching eight months a year.
   Fix: derive from the profile's observed months, or add an explicit anchor control.

2. **Nothing is ever retired.** `expectations.sync` only creates and updates. Reassign an
   invoice's property on the Fixer page, or re-bind its vendor, and the old
   `(property, vendor)` obligation stays `active=1` and generates an instance every month
   forever. Delete a vendor and the row renders as the literal string `(vendor)`, still
   flagged. Delete a property and the row silently relocates into the "All properties"
   group. Spec §10 promises "obligations against a retired `vendor_id` are deactivated, not
   deleted"; that is not implemented, and there is no deactivate route at all.

3. **Reminder vendor-attachment is dead on both ends.** `add_reminder` reads a `vendor_id`
   form field that `templates/month.html` never renders, and grouping is by `property_id`
   only, so a vendor-attached reminder would land in "All properties" regardless. Spec §5
   says attachment files the reminder "beside its expected invoices". Build it or drop the
   parameter.

4. **A one-off reminder dated outside the viewed month vanishes silently.** `open_period`
   creates nothing, because `resolve_window` returns `None` for an out-of-period `date:`
   rule — but the route still flashes "Added: {title}". Detect the `None` and say which
   month it will appear in.

5. **`notes` is overloaded** as both the `unconfirmed` promotion sentinel and user free text.
   Any cadence edit destroys user notes, and a note that happens to read `unconfirmed` makes
   the row permanently un-flaggable. Give the marker its own column.

6. **`sync` is not concurrency-safe.** There is no UNIQUE constraint on
   `obligation(property_id, vendor_id)` — only a plain index — and Flask's dev server is
   threaded, so two simultaneous `/month/open` posts would duplicate every expectation.
   Rollover is protected by `obligation_instance`'s UNIQUE; sync is not.

7. **The two closing paths disagree.** `ledger.set_instance_state` blanks `note`, so ticking
   a row by hand destroys a prior skip note; `expectations.satisfy_period` preserves it.
   Spec §10 wants the note kept.

8. **Neither profiles nor satisfaction exclude `status='DUPLICATE'`.** A duplicate row would
   inflate a profile and could satisfy an instance. Latent — zero duplicates in the current
   data.

## Minor

- `ledger.active_obligations` is called only by tests.
- `ledger.INSTANCE_COLUMNS` is defined and never referenced.
- `periods.CADENCE_GATE_SPAN_MONTHS` is a dead condition: four distinct `YYYY-MM` values
  always span ≥4 months, so `span >= 4` can never bind while `CADENCE_GATE_OBSERVATIONS` is 4.
  It becomes live only if the observation bar is lowered.
- `add_reminder` validates against a hardcoded `"August 2026"` when no period is submitted.
- Spec §7 wants "All properties" **first**; `sorted(groups.items())` puts it alphabetically.
- Learned expectations all have `title=''`, so `ORDER BY o.property_id, o.title` leaves rows
  in obligation-id order rather than alphabetical by the label the user actually sees.
- `once` is in neither `VALID_CADENCES` nor the template's cadence list, so pressing "Set" on
  a one-off rewrites its cadence to `monthly`. Harmless only because the `date:` rule still
  confines it.
- Spec §9 wants the unparsed-date cell on the invoices list to link to the Fixer date queue;
  it renders a bare `<span>`.
- Spec §6.5 lists `cadence`, `window_rule`, `title` and `notes` as editable; only `cadence`
  shipped.
- `_instance_or_redirect` neither redirects nor uses its `dict(row)` result — it is an
  existence check with a misleading name.
- `sync` opens a fresh connection per obligation (51 transactions on the current data), so a
  crash midway leaves a partial learn.
- Spec §4 calls `property_id`/`vendor_id` foreign keys, but the DDL has no `REFERENCES` — which
  is why finding 2 is silent rather than an error.

## Method notes worth carrying

- **Mutation testing needs `PYTHONDONTWRITEBYTECODE=1` and a `__pycache__` purge.** CPython
  validates a cached `.pyc` on `(source_size, source_mtime_in_whole_seconds)`, so a
  byte-length-preserving mutation restored inside the same second reuses bytecode compiled
  from the mutated source. It fails in both directions; the dangerous one is a surviving
  mutant that looks caught.
- **Assert the anchor match count on every mutation.** Line endings differ per file in this
  repo (`app.py` CRLF, `core/*.py` and templates LF, and git may flip them on checkout). A
  multi-line anchor that fails to match reports as a false SURVIVED. This produced three fake
  survivors in one task and two more later.
- **A caught mutation can be caught by the wrong assertion**, by an artefact of the fixture,
  or only in its naive form. A fixture with one property and one vendor cannot see a key
  collapsed onto half the pair; a fixture leaving a column empty cannot tell "reads the right
  column" from "reads a blank one".
- **Never assert on a value the page also echoes.** A route that flashes `Added: {title}` makes
  `assertIn(title, html)` pass even when the item is absent from the page. Assert structure.
