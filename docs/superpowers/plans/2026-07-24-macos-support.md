# macOS Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the existing invoice processor on macOS with the same feature set it has on Windows, set up and operated by a non-technical coworker.

**Architecture:** One codebase, no fork. Platform knowledge is consolidated in `state.py` (which already owns the file-manager integration) and exposed to templates through the existing `inject_shell` context processor, so templates never test the platform themselves. `core/bankrec.py` stays self-contained and only gains extra Tesseract search paths. A new `start.command` mirrors what `start.bat` does on Windows, and a new Settings-page field writes the API key to `.env` so nobody has to find a hidden dotfile in Finder.

**Tech Stack:** Python 3.10+, Flask 3, SQLite, pypdf, anthropic SDK, Jinja2 templates, plain bash for the macOS launcher, `unittest` for tests.

## Global Constraints

- **Python floor is 3.10** — the real floor from `python-dotenv`, `PyMuPDF`, and `Pillow`. The launcher must check for `>= 3.10`, matching the README.
- **Windows behavior must not change.** Every edit is additive or platform-gated. The full suite plus all eight pages returning 200 on Windows is a release gate.
- **The implementer works on Windows and has no macOS access.** No macOS item may be reported as verified. Tasks 1–8 are Windows-verifiable; Task 9 is the checklist a human with a Mac runs.
- **`start.command` must be LF-only and mode `100755`.** A CRLF shebang fails on macOS with `bad interpreter: /bin/bash^M`; without the exec bit a double-click does nothing.
- **macOS has no `python` command** — only `python3`, which may be an Xcode CLT stub that merely prompts to install. Existence checks are insufficient; the launcher must run it and read the version.
- **The API key is never displayed back** — not in a field value, not in a flash message, not in logs. Pages show only whether a key is saved.
- **UI copy says "Finder" on macOS, "File Explorer" on Windows** — never a hardcoded "Windows File Explorer".
- **Existing test command:** `python -m unittest discover -s tests -t .` run from the repo root. All new tests go in `tests/`.
- **Commit style:** imperative subject line, and every commit ends with the trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `state.py` | Platform flags, file-manager name, wizard example path, `.env` key writer | Modify |
| `app.py` | Inject `file_manager`; `POST /settings/api-key` route; pass example path to wizard | Modify |
| `core/bankrec.py` | Tesseract discovery gains Homebrew paths | Modify |
| `core/processor.py` | One hardcoded backslash in a completion message | Modify |
| `templates/invoices.html` | Reveal-button tooltip uses `{{ file_manager }}` | Modify |
| `templates/wizard.html` | Server-supplied path; `{{ file_manager }}` copy | Modify |
| `templates/settings.html` | API key field | Modify |
| `.gitattributes` | Pin `*.command` / `*.sh` to LF | Create |
| `start.command` | macOS launcher | Create |
| `README.md` | macOS setup section | Modify |
| `tests/test_platform.py` | Platform helpers + wizard example path | Create |
| `tests/test_env_writer.py` | `.env` key writer | Create |
| `docs/superpowers/plans/2026-07-24-macos-support.md` | This plan | Created |

Two files carry the most risk and are deliberately isolated: `state.py` gains all platform branching (so a reader has one place to look), and `start.command` is pure bash with no Python dependency (so its failure modes are readable by a non-technical user).

---

## Task 1: Platform flags and file-manager name

**Files:**
- Modify: `state.py:158` (insert constants above `reveal_in_explorer`)
- Test: `tests/test_platform.py` (create)

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `state.IS_WINDOWS: bool`, `state.IS_MAC: bool`, `state.FILE_MANAGER: str`, and `state.file_manager_name(platform: str) -> str` — the pure function the constants are built from, so tests can exercise both branches without monkeypatching module state.

- [ ] **Step 1: Write the failing test**

Create `tests/test_platform.py`:

```python
# -*- coding: utf-8 -*-
"""Unit tests for the platform seam in state.py. These must pass on any OS - the pure
helpers take the platform string as an argument so both branches are exercised from
whichever machine runs the suite."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state


class FileManagerName(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(state.file_manager_name("win32"), "File Explorer")

    def test_mac(self):
        self.assertEqual(state.file_manager_name("darwin"), "Finder")

    def test_other_platforms_get_a_generic_name(self):
        self.assertEqual(state.file_manager_name("linux"), "your file manager")

    def test_module_constants_match_this_machine(self):
        self.assertEqual(state.FILE_MANAGER, state.file_manager_name(sys.platform))
        self.assertEqual(state.IS_WINDOWS, sys.platform == "win32")
        self.assertEqual(state.IS_MAC, sys.platform == "darwin")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_platform -v`
Expected: FAIL with `AttributeError: module 'state' has no attribute 'file_manager_name'`

- [ ] **Step 3: Write minimal implementation**

In `state.py`, insert directly above `def reveal_in_explorer` (currently line 158):

```python
# --------------------------------------------------------------------------- platform seam
# All platform branching for the UI lives here, so templates never test the platform
# themselves (app.py injects FILE_MANAGER into every page via inject_shell).

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def file_manager_name(platform: str) -> str:
    """Display name of the OS file manager, for UI copy ('Show this file in ...').
    Takes the platform string so both branches are testable from either OS."""
    if platform == "win32":
        return "File Explorer"
    if platform == "darwin":
        return "Finder"
    return "your file manager"


FILE_MANAGER = file_manager_name(sys.platform)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_platform -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full suite to confirm nothing regressed**

Run: `python -m unittest discover -s tests -t .`
Expected: OK, 30 tests (26 existing + 4 new)

- [ ] **Step 6: Commit**

```bash
git add state.py tests/test_platform.py
git commit -m "Add platform seam for file-manager naming

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: Wizard example path built server-side

**Files:**
- Modify: `state.py` (append after `assembled_output_dir`, currently line 187)
- Test: `tests/test_platform.py` (add a test class)

**Interfaces:**
- Consumes: `config.BANK_REC_ROOT`, `core.processor._safe_folder_name`
- Produces: `state.bank_rec_example_path(month: str, property_name: str = "") -> str` — the fully-joined absolute path the wizard displays as its "drop documents here" hint, using the OS separator. Returns the month-root path when `property_name` is empty. Task 6 consumes this.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_platform.py`, above the `if __name__` block:

```python
class BankRecExamplePath(unittest.TestCase):
    def test_includes_month_folder(self):
        p = state.bank_rec_example_path("July 2026")
        self.assertTrue(p.endswith("July 2026 Bank Rec"), p)

    def test_appends_property_folder(self):
        p = state.bank_rec_example_path("July 2026", "Solair")
        self.assertTrue(p.endswith("July 2026 Bank Rec" + os.sep + "Solair"), p)

    def test_uses_os_separator_not_hardcoded_backslash(self):
        p = state.bank_rec_example_path("July 2026", "Solair")
        self.assertIn(os.sep, p)
        if os.sep == "/":
            self.assertNotIn("\\", p)

    def test_property_name_is_made_folder_safe(self):
        # ':' is illegal in a Windows folder name and awkward in Finder; _safe_folder_name
        # strips it, and the hint must show the real folder the user will see on disk.
        p = state.bank_rec_example_path("July 2026", "Foo: Bar")
        self.assertNotIn(":", p.split("Bank Rec")[-1])

    def test_blank_month_does_not_crash(self):
        self.assertIsInstance(state.bank_rec_example_path("", ""), str)
```

Add `import os` to the imports at the top of `tests/test_platform.py` (the file currently imports only `sys`, `unittest`, and `Path`):

```python
import os
import sys
import unittest
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_platform.BankRecExamplePath -v`
Expected: FAIL with `AttributeError: module 'state' has no attribute 'bank_rec_example_path'`

- [ ] **Step 3: Write minimal implementation**

In `state.py`, insert directly below `assembled_output_dir` (which ends at line 189):

```python
def bank_rec_example_path(month: str, property_name: str = "") -> str:
    """The folder the user drops bank documents into, as a display string for the wizard's
    hint. Joined with the OS separator - the template used to concatenate backslashes in
    JavaScript, which reads wrong on macOS."""
    folder = config.BANK_REC_ROOT / f"{month} Bank Rec"
    if property_name:
        folder = folder / ip._safe_folder_name(property_name)
    return str(folder)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_platform -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add state.py tests/test_platform.py
git commit -m "Build the wizard's drop-folder hint server-side

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: `.env` API key writer

**Files:**
- Modify: `state.py` (append after `api_key_present`, currently ends line 403)
- Test: `tests/test_env_writer.py` (create)

**Interfaces:**
- Consumes: `config.ENV_FILE`
- Produces:
  - `state._api_key_in_env_file() -> str` — the key currently stored in `.env`, or `""`.
  - `state.save_api_key(key: str) -> None` — writes/replaces the `ANTHROPIC_API_KEY` line in `.env`. Raises `ValueError` on empty input or input containing newlines/control characters. Creates `.env` if absent. Preserves all other lines. Writes atomically, sets mode `0600` on POSIX, **and updates `os.environ` so the next job subprocess uses the new key**.
  - `state.api_key_from_env_var() -> bool` — True when the environment supplies a *different* key than `.env` holds, i.e. an external override that will win. Task 5 consumes both.

> **Two traps this task exists to avoid.** Both were found while reviewing this plan, and the naive
> implementations look correct but are not:
>
> 1. **`os.environ` cannot be read directly to detect an override.** Importing `state` imports
>    `core.processor`, which calls `load_dotenv()` at module level — so `.env`'s key is already
>    *in* `os.environ` by the time any request runs. A plain `os.environ.get(...)` check would
>    report "a system environment variable is overriding your key" every single time a key is
>    saved. The override check must therefore *compare* the environment value against the file
>    value; they differ only when something outside the app really is supplying a different key.
> 2. **Saving a key would not take effect until restart.** `runner.py` launches jobs with
>    `env={**os.environ, ...}`, and `load_dotenv()` does not override variables that already
>    exist. A freshly-saved key sitting only in `.env` would be shadowed by the stale value the
>    parent process loaded at import. So `save_api_key()` must update `os.environ` too.

- [ ] **Step 1: Write the failing test**

Create `tests/test_env_writer.py`:

```python
# -*- coding: utf-8 -*-
"""Unit tests for the .env API-key writer. Uses a temp .env - never touches the real one."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import state

FAKE_KEY = "sk-ant-test-0000000000000000"
OTHER_KEY = "sk-ant-test-1111111111111111"


class SaveApiKey(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ipw_env_"))
        self._real_env_file = config.ENV_FILE
        config.ENV_FILE = self.tmp / ".env"

    def tearDown(self):
        config.ENV_FILE = self._real_env_file

    def test_creates_env_file_when_absent(self):
        self.assertFalse(config.ENV_FILE.exists())
        state.save_api_key(FAKE_KEY)
        self.assertIn(f"ANTHROPIC_API_KEY={FAKE_KEY}",
                      config.ENV_FILE.read_text(encoding="utf-8"))

    def test_replaces_existing_key_without_duplicating(self):
        config.ENV_FILE.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        state.save_api_key(OTHER_KEY)
        text = config.ENV_FILE.read_text(encoding="utf-8")
        self.assertEqual(text.count("ANTHROPIC_API_KEY="), 1)
        self.assertIn(OTHER_KEY, text)
        self.assertNotIn(FAKE_KEY, text)

    def test_preserves_other_lines(self):
        config.ENV_FILE.write_text(
            "# my config\nCLAUDE_MODEL=claude-sonnet-5\n"
            f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        state.save_api_key(OTHER_KEY)
        text = config.ENV_FILE.read_text(encoding="utf-8")
        self.assertIn("# my config", text)
        self.assertIn("CLAUDE_MODEL=claude-sonnet-5", text)
        self.assertIn(OTHER_KEY, text)

    def test_appends_when_file_exists_without_the_key(self):
        config.ENV_FILE.write_text("CLAUDE_MODEL=claude-sonnet-5\n", encoding="utf-8")
        state.save_api_key(FAKE_KEY)
        text = config.ENV_FILE.read_text(encoding="utf-8")
        self.assertIn("CLAUDE_MODEL=claude-sonnet-5", text)
        self.assertIn(f"ANTHROPIC_API_KEY={FAKE_KEY}", text)

    def test_file_ends_with_newline(self):
        state.save_api_key(FAKE_KEY)
        self.assertTrue(config.ENV_FILE.read_text(encoding="utf-8").endswith("\n"))

    def test_rejects_empty(self):
        for bad in ("", "   ", "\t"):
            with self.assertRaises(ValueError):
                state.save_api_key(bad)

    def test_rejects_newline_injection(self):
        # Without this, a pasted value could inject arbitrary extra .env lines.
        for bad in (f"{FAKE_KEY}\nEVIL=1", f"{FAKE_KEY}\r\nEVIL=1", f"{FAKE_KEY}\rEVIL=1"):
            with self.assertRaises(ValueError):
                state.save_api_key(bad)
        self.assertFalse(config.ENV_FILE.exists())

    def test_rejects_control_characters(self):
        with self.assertRaises(ValueError):
            state.save_api_key("sk-ant-\x00-null")

    def test_strips_surrounding_whitespace(self):
        state.save_api_key(f"  {FAKE_KEY}  ")
        self.assertIn(f"ANTHROPIC_API_KEY={FAKE_KEY}\n",
                      config.ENV_FILE.read_text(encoding="utf-8"))

    def test_does_not_leave_temp_files_behind(self):
        state.save_api_key(FAKE_KEY)
        leftovers = [p.name for p in self.tmp.iterdir() if p.name != ".env"]
        self.assertEqual(leftovers, [])

    @unittest.skipIf(os.name == "nt", "POSIX file modes only")
    def test_permissions_are_owner_only_on_posix(self):
        state.save_api_key(FAKE_KEY)
        self.assertEqual(config.ENV_FILE.stat().st_mode & 0o777, 0o600)

    def test_saved_key_is_then_reported_present(self):
        state.save_api_key(FAKE_KEY)
        self.assertTrue(state.api_key_present())

    def test_updates_os_environ_so_the_next_job_uses_it(self):
        # runner.py passes env={**os.environ} to job subprocesses, and load_dotenv() does NOT
        # override an existing variable - so without this the saved key is shadowed by the
        # stale one until the app restarts.
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-stale-9999999999999999"
        try:
            state.save_api_key(FAKE_KEY)
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], FAKE_KEY)
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)


class ApiKeyFromEnvVar(unittest.TestCase):
    """The override check must COMPARE env against file. Importing state imports
    core.processor, which calls load_dotenv() - so .env's key is always in os.environ and a
    bare os.environ check would cry 'override' every time."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ipw_envvar_"))
        self._real_env_file = config.ENV_FILE
        config.ENV_FILE = self.tmp / ".env"
        self._had = os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        config.ENV_FILE = self._real_env_file
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if self._had is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._had

    def test_false_when_environment_has_no_key(self):
        config.ENV_FILE.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        self.assertFalse(state.api_key_from_env_var())

    def test_false_when_environment_merely_mirrors_the_file(self):
        # This is what load_dotenv() does on every import - it is NOT an override.
        config.ENV_FILE.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.assertFalse(state.api_key_from_env_var())

    def test_true_when_environment_supplies_a_different_key(self):
        config.ENV_FILE.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
        os.environ["ANTHROPIC_API_KEY"] = OTHER_KEY
        self.assertTrue(state.api_key_from_env_var())

    def test_true_when_env_has_a_key_and_no_file_exists(self):
        os.environ["ANTHROPIC_API_KEY"] = OTHER_KEY
        self.assertTrue(state.api_key_from_env_var())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_env_writer -v`
Expected: FAIL with `AttributeError: module 'state' has no attribute 'save_api_key'`

- [ ] **Step 3: Write minimal implementation**

In `state.py`, append below `api_key_present` (currently ends line 403):

```python
def _api_key_in_env_file() -> str:
    """The ANTHROPIC_API_KEY value currently stored in .env, or '' if absent/unreadable."""
    if not config.ENV_FILE.exists():
        return ""
    try:
        for line in config.ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def api_key_from_env_var() -> bool:
    """True when the environment supplies a DIFFERENT key than .env holds - an outside
    override (shell profile, system variable) that python-dotenv won't replace, so it wins.

    Deliberately a comparison, not a bare os.environ check: importing this module imports
    core.processor, which calls load_dotenv(), so .env's own key is always in os.environ by
    now. A bare check would report an override every single time."""
    env_val = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not env_val:
        return False
    return env_val != _api_key_in_env_file()


def save_api_key(key: str) -> None:
    """Write ANTHROPIC_API_KEY into the app's .env, preserving every other line.

    The value lands in a config file, so it's treated as untrusted input: a newline would
    otherwise inject arbitrary extra .env lines. Deliberately NOT validated against an
    'sk-ant-' prefix - key formats change, and rejecting a valid key is worse than accepting
    a bad one, which the processor already reports clearly ("Claude rejected your API key").

    Raises ValueError if the key is empty or contains newlines/control characters.
    """
    key = (key or "").strip()
    if not key:
        raise ValueError("The API key can't be blank.")
    if any(c in key for c in "\r\n") or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ValueError("The API key can't contain line breaks or control characters. "
                         "Paste just the key itself.")

    path = config.ENV_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            if not replaced:                       # collapse any duplicates to one line
                out.append(f"ANTHROPIC_API_KEY={key}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(f"ANTHROPIC_API_KEY={key}")

    # Atomic write: a truncated .env would lose the key AND any other settings.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)                       # no-op in practice on Windows
    except OSError:
        pass
    os.replace(tmp, path)

    # runner.py hands jobs env={**os.environ, ...}, and load_dotenv() will NOT overwrite a
    # variable that already exists - so without this line the subprocess keeps using the key
    # loaded at import and the save silently does nothing until the app restarts.
    os.environ["ANTHROPIC_API_KEY"] = key
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_env_writer -v`
Expected: PASS (17 tests; the permissions test is skipped on Windows)

- [ ] **Step 5: Verify the real `.env` was never touched**

Run: `git status --short .env`
Expected: no output — `.env` is gitignored and unmodified. Also confirm the file still has your real key by checking it is non-empty: `python -c "import config; print('bytes:', config.ENV_FILE.stat().st_size)"`

- [ ] **Step 6: Commit**

```bash
git add state.py tests/test_env_writer.py
git commit -m "Add .env API key writer with injection guards

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Inject the file-manager name into every page

**Files:**
- Modify: `app.py:45-51` (the `inject_shell` context processor)
- Modify: `templates/invoices.html:139` (reveal-button tooltip)
- Test: manual page check (this is template wiring; the helper itself is covered by Task 1)

**Interfaces:**
- Consumes: `state.FILE_MANAGER` from Task 1
- Produces: `file_manager` available in every template. Task 6 consumes it.

- [ ] **Step 1: Add the value to the context processor**

In `app.py`, replace the body of `inject_shell` (lines 45-51) with:

```python
@app.context_processor
def inject_shell():
    """The sidebar badge and period pill are part of base.html, so every page needs them.
    `file_manager` is here too so no template has to test the platform itself."""
    return {
        "shell_needs_review": db.counts()["needs_review"],
        "shell_month": state.load_settings()["month"],
        "file_manager": state.FILE_MANAGER,
    }
```

- [ ] **Step 2: Use it in the invoices template**

In `templates/invoices.html` line 139, replace:

```html
                    title="Show this file in Windows File Explorer">
```

with:

```html
                    title="Show this file in {{ file_manager }}">
```

- [ ] **Step 3: Verify no hardcoded file-manager copy remains**

Run: `git grep -n "Windows File Explorer" -- templates/ static/`
Expected: no output.

- [ ] **Step 4: Verify the page renders with the injected value**

Start the app (`python app.py`), then run:

```bash
curl -s http://127.0.0.1:5057/invoices | grep -o 'Show this file in [A-Za-z ]*' | head -1
```

Expected on Windows: `Show this file in File Explorer`

- [ ] **Step 5: Commit**

```bash
git add app.py templates/invoices.html
git commit -m "Inject file-manager name into templates

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 5: API key field on the Settings page

**Files:**
- Modify: `app.py:549-555` (`settings_page`), and add a new route after `save_settings_route` (currently ends line 564)
- Modify: `templates/settings.html:8-14` (the "Anthropic API key" section)
- Test: exercised through the running app in Step 4 below; the writer's logic is covered by Task 3

**Interfaces:**
- Consumes: `state.save_api_key()` and `state.api_key_from_env_var()` from Task 3
- Produces: `POST /settings/api-key` route named `save_api_key_route`

- [ ] **Step 1: Pass the override flag to the template**

In `app.py`, replace `settings_page` (lines 549-555) with:

```python
@app.route("/settings")
def settings_page():
    return render_template("settings.html",
                           settings=state.load_settings(),
                           api_key_present=state.api_key_present(),
                           api_key_from_env=state.api_key_from_env_var(),
                           env_file=str(config.ENV_FILE),
                           data_dir=str(config.DATA_DIR))
```

- [ ] **Step 2: Add the save route**

In `app.py`, insert directly below `save_settings_route` (which ends at line 564):

```python
@app.route("/settings/api-key", methods=["POST"])
def save_api_key_route():
    """Save the Anthropic API key into .env. The value is never echoed back to the page,
    put in a flash message, or logged - only whether a key is now saved."""
    # Check for an outside override BEFORE saving: save_api_key() updates os.environ itself,
    # which would hide the evidence that a shell/system variable was supplying a different key.
    had_override = state.api_key_from_env_var()
    try:
        state.save_api_key(request.form.get("api_key", ""))
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("settings_page"))
    except OSError as e:
        flash(f"Could not write the .env file: {e}")
        return redirect(url_for("settings_page"))
    if had_override:
        flash("API key saved and in use now. Note: ANTHROPIC_API_KEY is also set as a system "
              "environment variable on this machine — remove it, or that one will take over "
              "again the next time the app restarts.")
    else:
        flash("API key saved. The next run will use it (no restart needed).")
    return redirect(url_for("settings_page"))
```

- [ ] **Step 3: Add the form to the Settings page**

In `templates/settings.html`, replace the whole first `<section>` (lines 8-14) with:

```html
<section>
  <h2>Anthropic API key</h2>
  <p>Status: {% if api_key_present %}<span class="ok">Saved</span>{% else %}<span class="warn">Missing</span>{% endif %}</p>
  {% if api_key_from_env %}
    <p class="muted"><strong>Note:</strong> <code>ANTHROPIC_API_KEY</code> is set as a system
    environment variable, which takes priority over anything saved here.</p>
  {% endif %}
  <form method="post" action="{{ url_for('save_api_key_route') }}" class="inline-form">
    <input type="password" name="api_key" placeholder="sk-ant-..." autocomplete="off" required
           size="34">
    <button class="btn" type="submit">{% if api_key_present %}Replace key{% else %}Save key{% endif %}</button>
  </form>
  <p class="muted">Get a key at
  <a href="https://console.anthropic.com" target="_blank" rel="noopener">console.anthropic.com</a>.
  It is stored in this app's own <code>.env</code> file and never shown again:<br><code>{{ env_file }}</code></p>
</section>
```

- [ ] **Step 4: Verify against the running app, without disturbing the real key**

Back up the real `.env` first, then test, then restore:

```bash
cp .env .env.backup-manual-test
```

Start the app, open <http://127.0.0.1:5057/settings>, paste `sk-ant-test-0000000000000000`, submit. Then check:

```bash
grep -c "ANTHROPIC_API_KEY=" .env
curl -s http://127.0.0.1:5057/settings | grep -c "sk-ant-test-0000000000000000"
```

Expected: `1` from the first command (exactly one key line, no duplicate), and `0` from the second — **the key must never appear in the rendered page.** If the second command returns anything other than 0, stop and fix before continuing.

Now restore:

```bash
cp .env.backup-manual-test .env && rm .env.backup-manual-test
```

Confirm the restore: `python -c "import state; print('key present:', state.api_key_present())"` → `key present: True`

- [ ] **Step 5: Verify blank and injection input are rejected by the UI**

Submit the form with a value containing a line break (paste `abc` then Shift+Enter then `EVIL=1` into the field if the browser allows, or run):

```bash
curl -s -X POST -H "Origin: http://127.0.0.1:5057" --data-urlencode "api_key=abc
EVIL=1" http://127.0.0.1:5057/settings/api-key -o /dev/null -w "%{http_code}\n"
grep -c "EVIL" .env
```

Expected: `302` (redirect with a flash, not a 500) and `0` occurrences of EVIL in `.env`.

- [ ] **Step 6: Commit**

```bash
git add app.py templates/settings.html
git commit -m "Add API key entry to the Settings page

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 6: Wizard uses the server-supplied path and Finder wording

**Files:**
- Modify: `app.py` — the `wizard()` route (search for `def wizard`), to pass the example path
- Modify: `templates/wizard.html:29` (copy), `:33` (the path element), `:99-107` (the JS that rebuilds it), `:109` (comment)
- Test: page render check in Step 4

**Interfaces:**
- Consumes: `state.bank_rec_example_path()` from Task 2, `file_manager` from Task 4
- Produces: nothing downstream

- [ ] **Step 1: Pass an example-path template to the page**

The path must update live when the user changes the month or property dropdown, so the server sends the *pieces* and the JS joins them with the OS separator rather than a hardcoded backslash. In `app.py`, in the `wizard()` route, add two values to the `render_template` call:

```python
@app.route("/wizard")
def wizard():
    return render_template("wizard.html",
                           settings=state.load_settings(),
                           properties=state.per_property_pending(),
                           months=state.month_options(),
                           bank_rec_root=str(config.BANK_REC_ROOT),
                           path_sep=os.sep,
                           example_path=state.bank_rec_example_path(
                               state.load_settings()["month"]))
```

Add `import os` to `app.py`'s imports if it is not already there — check with `git grep -n "^import os" app.py` first; if there is no match, add `import os` alongside the existing `import datetime`.

- [ ] **Step 2: Use them in the template**

In `templates/wizard.html` line 29, replace `(you do this in File Explorer)` with `(you do this in {{ file_manager }})`.

Replace line 33:

```html
    <p><code id="droppath">{{ bank_rec_root }}\&lt;Month&gt; Bank Rec\&lt;property&gt;\</code></p>
```

with:

```html
    <p><code id="droppath">{{ example_path }}</code></p>
```

In the script block, replace the `destRoot` constant (line 69) and the `updatePath` function (lines 99-102) with:

```javascript
  const destRoot = {{ bank_rec_root|tojson }};
  const pathSep = {{ path_sep|tojson }};

  function updatePath() {
    const parts = [destRoot, `${monthSel.value} Bank Rec`];
    if (propSel.value) parts.push(propSel.value);
    dropPath.textContent = parts.join(pathSep) + pathSep;
  }
```

In line 109, replace the comment `// "Open this folder" — jumps to the staged month/property folder in File Explorer.` with `// "Open this folder" — jumps to the staged month/property folder in the OS file manager.`

- [ ] **Step 3: Verify no hardcoded separators remain in the template**

Run: `git grep -n '\\\\' -- templates/wizard.html`
Expected: no output.

- [ ] **Step 4: Verify the rendered page**

With the app running:

```bash
curl -s "http://127.0.0.1:5057/wizard" | grep -o 'id="droppath">[^<]*'
curl -s "http://127.0.0.1:5057/wizard" | grep -c "File Explorer"
```

Expected: a full absolute path ending in `Bank Rec` on Windows, and `1` for the second (the injected `{{ file_manager }}` renders as "File Explorer" on Windows — the point is that it is no longer hardcoded, which Step 3 of Task 4 already proved for `templates/`).

Then open <http://127.0.0.1:5057/wizard> in the browser, change the property dropdown, and confirm the displayed path updates and uses `\` on Windows.

- [ ] **Step 5: Commit**

```bash
git add app.py templates/wizard.html
git commit -m "Build the wizard drop path with the OS separator

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 7: Mac Tesseract paths and the processor's backslash

**Files:**
- Modify: `core/bankrec.py:48-58` (`_find_tesseract`)
- Modify: `core/processor.py:1117` (the completion message)
- Test: `tests/test_platform.py` (add a test class)

**Interfaces:**
- Consumes: nothing
- Produces: nothing downstream (both are leaf changes)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_platform.py`, above the `if __name__` block:

```python
class TesseractDiscovery(unittest.TestCase):
    def test_homebrew_paths_are_candidates(self):
        # Apple Silicon installs to /opt/homebrew, Intel to /usr/local. shutil.which()
        # covers the PATH case; these fallbacks cover a launcher with a minimal PATH.
        from core import bankrec
        src = inspect.getsource(bankrec._find_tesseract)
        self.assertIn("/opt/homebrew/bin/tesseract", src)
        self.assertIn("/usr/local/bin/tesseract", src)

    def test_windows_paths_still_present(self):
        from core import bankrec
        src = inspect.getsource(bankrec._find_tesseract)
        self.assertIn(r"C:\Program Files\Tesseract-OCR\tesseract.exe", src)

    def test_returns_none_or_a_real_path(self):
        from core import bankrec
        found = bankrec._find_tesseract()
        self.assertTrue(found is None or os.path.exists(found), found)
```

Add `import inspect` to the imports at the top of `tests/test_platform.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_platform.TesseractDiscovery -v`
Expected: FAIL on `test_homebrew_paths_are_candidates` — `'/opt/homebrew/bin/tesseract' not found in ...`

- [ ] **Step 3: Write minimal implementation**

In `core/bankrec.py`, replace `_find_tesseract` (lines 48-58) with:

```python
def _find_tesseract():
    """Path to the tesseract binary, or None. PATH first (covers Homebrew and the Windows
    installer when it registered itself), then the usual install locations - a launcher
    started from Finder can have a minimal PATH that misses Homebrew."""
    import shutil
    p = shutil.which("tesseract")
    if p:
        return p
    for c in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
              "/opt/homebrew/bin/tesseract",        # macOS, Apple Silicon
              "/usr/local/bin/tesseract",           # macOS, Intel
              "/opt/local/bin/tesseract"):          # macOS, MacPorts
        if os.path.exists(c):
            return c
    return None
```

In `core/processor.py` line 1117, replace:

```python
    print(f"         {moved} file(s) moved to '{PROCESSED_FOLDER.name}\\<property>'")
```

with:

```python
    print(f"         {moved} file(s) moved to '{os.path.join(PROCESSED_FOLDER.name, '<property>')}'")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_platform -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Verify the processor still imports and the message renders**

Run: `python -c "import os; from core import processor as ip; print(os.path.join(ip.PROCESSED_FOLDER.name, '<property>'))"`
Expected on Windows: `processed\<property>`

- [ ] **Step 6: Commit**

```bash
git add core/bankrec.py core/processor.py tests/test_platform.py
git commit -m "Find Tesseract on macOS and drop a hardcoded path separator

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 8: The macOS launcher

**Files:**
- Create: `.gitattributes`
- Create: `start.command`
- Test: `bash -n` syntax check plus git plumbing checks (Steps 3-5)

**Interfaces:**
- Consumes: nothing
- Produces: nothing downstream

This task has no unit test — it is a shell script for an OS the implementer cannot run. The verification below is what *is* checkable from Windows: syntax, line endings, and the exec bit. Behavior is Task 9, item 1.

- [ ] **Step 1: Create `.gitattributes`**

```
# The macOS launcher must keep LF endings and its shebang intact. A CRLF shebang fails on
# macOS with: bad interpreter: /bin/bash^M
*.command text eol=lf
*.sh      text eol=lf

# The Windows launcher is the mirror image - it needs CRLF.
*.bat     text eol=crlf
```

- [ ] **Step 2: Create `start.command`**

```bash
#!/bin/bash
# macOS launcher - the twin of start.bat. Double-click it in Finder.
# Keep this file LF-only and executable (see .gitattributes); a CRLF shebang fails on macOS.

# Double-clicking a .command starts in the user's home folder, not the project.
cd "$(dirname "$0")" || exit 1

PORT=5057
URL="http://127.0.0.1:$PORT/"

# Any error should leave the window readable instead of flashing shut.
fail() {
  echo ""
  echo "  $1"
  echo ""
  echo "Press any key to close this window."
  read -r -n 1 -s
  exit 1
}

# Already running? Don't stack a second server on the same port - that's how you end up
# with a stale copy still serving old code. Just open the browser instead.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Invoice Processor is already running - opening it in your browser."
  echo "If you just changed the code, close the other Terminal window first, then run this again."
  open "$URL"
  exit 0
fi

# macOS has no "python" command, only "python3" - and that can be an Xcode stub that just
# prompts to install developer tools. So run it and read the version rather than trusting
# that the command exists.
if ! command -v python3 >/dev/null 2>&1; then
  fail "Python 3 is not installed.
  Download it from https://www.python.org/downloads/macos/ (get the latest 3.x),
  run the installer, then double-click this file again."
fi

if ! PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"; then
  fail "Python 3 is not fully installed yet.
  If macOS just offered to install the Command Line Developer Tools, that is not enough.
  Download Python from https://www.python.org/downloads/macos/ and run the installer,
  then double-click this file again."
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  fail "This app needs Python 3.10 or newer, but this Mac has Python $PYVER.
  Download a newer version from https://www.python.org/downloads/macos/,
  then double-click this file again."
fi

# Create the virtual environment on first run.
if [ ! -d ".venv" ]; then
  echo "First run: setting up. This takes a couple of minutes..."
  python3 -m venv .venv || fail "Could not create the virtual environment (.venv)."
fi

VPY=".venv/bin/python"
[ -x "$VPY" ] || fail "The .venv folder looks damaged. Delete it and double-click this file again."

# Install dependencies if anything is missing.
if ! "$VPY" -c "import flask, anthropic, openpyxl, pypdf, dotenv" >/dev/null 2>&1; then
  echo "Installing required packages..."
  "$VPY" -m pip install --quiet --upgrade pip
  "$VPY" -m pip install -r requirements.txt || fail "Could not install the required packages.
  Check your internet connection and try again."
fi

# Only import the legacy spreadsheet data if that old tool is actually sitting next door.
if [ ! -f "data/invoices.db" ] && [ -f "../invoice_processor/invoices.xlsx" ]; then
  echo "First run: importing your existing data..."
  "$VPY" migrate_to_db.py
fi

echo "Starting Invoice Processor at $URL"
open "$URL"
"$VPY" app.py
```

- [ ] **Step 3: Syntax-check the script (this works on Windows via Git Bash)**

Run: `bash -n start.command`
Expected: no output (no output means no syntax errors).

- [ ] **Step 4: Mark it executable and confirm the mode is recorded in git**

```bash
git add .gitattributes start.command
git update-index --chmod=+x start.command
git ls-files --stage start.command
```

Expected: the mode is `100755` (not `100644`). If it shows `100644`, re-run the `--chmod=+x` line.

- [ ] **Step 5: Confirm the committed blob has no CR bytes**

```bash
git stash --keep-index --include-untracked --quiet 2>/dev/null || true
git show :start.command | od -c | grep -c '\\r'
git stash pop --quiet 2>/dev/null || true
```

Expected: `0`. A non-zero count means CRLF made it into the index and the script will not run on macOS — fix `.gitattributes`, then `git rm --cached start.command && git add start.command` and re-check.

Simpler equivalent if the stash dance is awkward: `git show :start.command | wc -c` and compare against `wc -c < start.command`; on a correctly configured repo the staged blob should be the smaller or equal size (no added CRs).

- [ ] **Step 6: Commit**

```bash
git commit -m "Add macOS launcher and pin launcher line endings

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 9: README macOS section and the Mac verification checklist

**Files:**
- Modify: `README.md` — the "Requirements" section and the "Setup on a new machine" section
- Create: `docs/superpowers/checklists/2026-07-24-macos-acceptance.md`

**Interfaces:**
- Consumes: everything above
- Produces: the document a human with a Mac fills in

- [ ] **Step 1: Update the Requirements section of `README.md`**

Replace the `- **Windows** for the one-click...` bullet with:

```markdown
- **Windows or macOS.** Windows users double-click `start.bat`; macOS users double-click
  `start.command`. The app also runs on Linux via `python app.py`, though that is untested.
```

- [ ] **Step 2: Add a macOS setup section to `README.md`**

Insert directly after the existing "### 5. Start it" subsection:

````markdown
### macOS notes

Steps 1–4 above are the same on a Mac. Two differences:

- Use `python3` everywhere the steps say `python`. macOS has no `python` command.
  Activate the virtual environment with `source .venv/bin/activate`.
- Instead of `start.bat`, double-click **`start.command`** in Finder. On first run it creates
  the virtual environment and installs everything for you.

If `start.command` opens in a text editor instead of running it, its executable bit was lost —
restore it with:

```bash
chmod +x start.command
```

**API key:** rather than editing the hidden `.env` file (Finder hides dotfiles), open the
**Settings** page in the app and paste the key into the API key field.

**OCR (optional):** scanned deposit slips need Tesseract. Install
[Homebrew](https://brew.sh) once, then:

```bash
brew install tesseract
```

Without it the app still runs — scanned files fall back to their filename amount and are flagged
for review.
````

- [ ] **Step 3: Create the acceptance checklist**

Create `docs/superpowers/checklists/2026-07-24-macos-acceptance.md`:

```markdown
# macOS acceptance checklist

Run these on a Mac and record pass/fail. The implementer works on Windows and cannot run any
of them, so nothing here is verified until a human fills it in.

**Tester:** ____________  **Date:** ____________
**Mac model / chip:** ____________  (Apple Silicon or Intel)
**macOS version:** ____________

| # | Check | Expected | Pass/Fail | Notes |
|---|---|---|---|---|
| 1 | Clone the repo, double-click `start.command` | Terminal window opens, dependencies install, browser opens the dashboard | | |
| 2 | Double-click `start.command` again while it is running | Says "already running", opens the browser, does NOT start a second server | | |
| 3 | Settings page → paste API key → save | Status shows "Saved"; the key is never displayed back | | |
| 4 | Process page → drop one invoice PDF → Run processor | Extraction succeeds; the invoice appears on the Invoices page | | |
| 5 | Invoices page → click the reveal (folder) button | Finder opens with that file selected | | |
| 6 | Month-end page → "Open this folder" | Finder opens the month/property folder | | |
| 7 | Month-end page → change the property dropdown | The displayed path updates and uses forward slashes (`/`), no backslashes | | |
| 8 | Any page with a reveal button → hover it | Tooltip reads "Finder", never "Windows File Explorer" | | |
| 9 | Month-end → Stage, then Assemble on a real property folder | An ASSEMBLED PDF, a manifest.txt, and a matched.csv are all written; the PDF page count is plausible | | |
| 10 | Assemble BEFORE installing Tesseract | The log line reads `OCR: NOT available (...)` and the run still completes | | |
| 11 | `brew install tesseract`, then Assemble again | The log line reads `OCR: available` | | |
| 12 | Run the test suite: `python3 -m unittest discover -s tests -t .` | OK, all tests pass | | |

Repeat items 1, 9, 10, and 11 on **both** an Apple Silicon Mac and an Intel Mac.

## Known risks to watch for

- **Property names with accented characters.** macOS stores filenames decomposed (NFD),
  Windows composed (NFC), so an invoice for a property named with non-ASCII characters could
  show as "no file" in the UI even though the PDF is on disk. If you hit this, report the exact
  property name — the fix is a `unicodedata.normalize` at the comparison points in
  `state.resolve_invoice_file()` and `core/db.export_amount_sidecars()`.
- **PyMuPDF on Apple Silicon.** If `pip install` fails on PyMuPDF, OCR is unavailable but
  everything else works. Report it rather than working around it.
```

- [ ] **Step 4: Verify the README renders sensibly**

Run: `git grep -n "start.command" README.md`
Expected: at least three matches (requirements bullet, macOS notes, chmod fix).

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/checklists/2026-07-24-macos-acceptance.md
git commit -m "Document macOS setup and add the Mac acceptance checklist

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 10: Windows non-regression gate

**Files:** none modified — this is the release gate.

**Interfaces:**
- Consumes: every task above
- Produces: the evidence that Windows still works

- [ ] **Step 1: Run the full test suite**

Run: `python -m unittest discover -s tests -t .`
Expected: OK. Count should be 26 original + 12 platform (4 file-manager + 5 example-path + 3 Tesseract) + 17 env-writer = **55 tests** (the POSIX permissions test is skipped on Windows).

- [ ] **Step 2: Confirm every page still returns 200**

Start the app, then:

```bash
for p in / /invoices /process /wizard /fixer /properties /vendors /settings; do
  printf "%-14s %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5057$p)"
done
```

Expected: `200` for all eight.

- [ ] **Step 3: Confirm the cross-site guard still works**

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H 'Sec-Fetch-Site: cross-site' http://127.0.0.1:5057/settings/api-key
```

Expected: `403` — the new route inherits the existing guard.

- [ ] **Step 4: Confirm the real `.env` and database are untouched**

```bash
python -c "import state; print('key present:', state.api_key_present())"
python -c "import sqlite3; c=sqlite3.connect('file:data/invoices.db?mode=ro', uri=True); print('invoices:', c.execute('SELECT COUNT(*) FROM invoices').fetchone()[0])"
git status --short
```

Expected: `key present: True`, an invoice count matching what you started with (181 at the time of writing), and a clean working tree.

- [ ] **Step 5: Push**

```bash
git push origin main
```

- [ ] **Step 6: Hand the checklist to a Mac**

Send `docs/superpowers/checklists/2026-07-24-macos-acceptance.md` to whoever has the Mac. macOS support is **not** complete until that document comes back filled in.

---

## Self-review notes

**Spec coverage:** platform seam → Task 1; wizard path helper → Tasks 2, 6; `.env` writer requirements (no echo, newline rejection, preserve other lines, atomic, `0600`, no restart, env-var override reported) → Tasks 3, 5; `bankrec` Homebrew paths → Task 7; processor backslash → Task 7; `start.command` + `.gitattributes` → Task 8; README → Task 9; Windows non-regression → Task 10; macOS ten-point verification → Task 9 checklist (expanded to 12 rows: the spec's ten checks plus a test-suite run and a split of the OCR before/after cases). Every spec section maps to a task.

**Deliberate spec deviation:** the spec said "no hard-reject on an `sk-ant-` prefix"; the plan implements no prefix check at all rather than a non-blocking warning, since the app already reports a bad key clearly at run time and a warning on a value the page cannot display is noise.

**Two bugs found in this plan during self-review, corrected above.** Both are traps a
straightforward reading of the spec walks into, so they are called out at the top of Task 3 as
well:

1. *The env-var override check cannot read `os.environ` directly.* `state` imports
   `core.processor`, which calls `load_dotenv()` at module level, so `.env`'s key is already in
   `os.environ`. The spec's requirement to "report the environment-variable override" therefore
   needs a comparison against the file's value — a bare check reports a false override on every
   save. Fixed via `_api_key_in_env_file()`.
2. *The spec's "no app restart required" was not true as written.* `runner.py` passes
   `env={**os.environ, ...}` to job subprocesses and `load_dotenv()` never overrides an existing
   variable, so a newly-saved key would be shadowed by the stale one until restart. `save_api_key()`
   now updates `os.environ` as well, and Task 5 captures the override state *before* saving, since
   the save overwrites the evidence.

**Type consistency check:** `file_manager_name`/`FILE_MANAGER` (Task 1 → Task 4),
`bank_rec_example_path` (Task 2 → Task 6), `save_api_key`/`api_key_from_env_var`/
`_api_key_in_env_file` (Task 3 → Task 5), template variable `api_key_from_env` and route name
`save_api_key_route` (Task 5, consistent between route and `url_for`). No name appears in two
spellings.
