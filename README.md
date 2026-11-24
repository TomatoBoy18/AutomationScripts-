# Human-like typing automation for web typing lessons

A small Playwright-based helper for **web typing lessons** (the TypingClub / EdClub
family of sites). You log in by hand and pick the article by hand — the script only
reads the text of the lesson that is currently open, types it with a human-like
rhythm, and checks whether the lesson's required speed was reached.

Everything is driven by an external JSON config, so the same code can be pointed at a
different site instance, tuned to a different speed range, or adapted to a redesigned
front end **without touching the source**.

> **Educational use.** This project exists to explore browser automation, cross-origin
> iframes, synthetic keyboard events and timing models. Use it only where automation is
> permitted by the site's / your institution's terms, and never to impersonate a human
> where that matters (graded assessments, certificates, etc.).

---

## What it does

1. Opens a browser (your installed Chrome/Edge, or Playwright's Chromium) at the login page.
2. **You** log in, open the course, and open an article. Then press Enter in the terminal.
3. The script locates the typing iframe, rebuilds the article text from the DOM, and types
   it character by character:
   - one random target speed per run (e.g. 55–75 WPM), not a fixed metronome,
   - Gaussian jitter, word-boundary hesitation, occasional distracted pause,
   - a slow drift of the speed factor (stamina curve),
   - optional "typo + backspace" slips (off by default),
   - a hard minimum gap between keystrokes so the lesson engine does not drop keys.
4. Reads the result screen (`actual speed / required speed / target speed / accuracy / score`).
5. If the required speed was missed, it re-opens the same lesson and types it again faster
   (configurable number of retries).

---

## Quick start

```bash
git clone <this-repo>
cd typingclub-automation

python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -r requirements.txt
python typing_auto.py --init-config     # writes config.json from the built-in defaults
# edit config.json: set your site URLs and speed range
python typing_auto.py
```

On Windows you can also just double-click:

| File | Purpose |
| --- | --- |
| `setup.bat` | creates `.venv` and installs the dependencies |
| `run.bat` | runs the script and keeps the window open afterwards |

No credentials are ever stored in this repository. If you want the login form filled
automatically for your own local testing, set the environment variables named in
`login.username_env` / `login.password_env` (default `TC_USER` / `TC_PASS`); otherwise
just log in manually, which is the recommended flow.

---

## Configuration — the external interface

Precedence, lowest to highest:

```
built-in defaults  <  config.json (or --config file)  <  command-line flags
```

`config.json` is **git-ignored**, so site-specific settings (school sub-domain, tenant
name, personal speed range) never end up in the repository. `config.example.json` is the
committed template.

### `site`

| Key | Meaning |
| --- | --- |
| `home_url` | Login entry point opened on start |
| `course_url` | Course page the script expects you to be on |
| `lesson_url_template` | Used by `--lesson`; supports `{course_url}`, `{home_url}`, `{lesson}` |

### `selectors`

| Key | Meaning |
| --- | --- |
| `frame_keyword` | Substring of the typing iframe's URL — how the frame is found |
| `unit_selector` | One element per character cell |
| `line_selector` | Clicked once to move keyboard focus into the iframe |
| `typed_selector` | Cells already typed (used for real progress) |
| `min_units` | Minimum cell count before the page counts as "loaded" |

### `typing`

| Key | Meaning |
| --- | --- |
| `wpm_min` / `wpm_max` | Random target range, picked once per run |
| `jitter` | Gaussian noise on each interval (0 = metronome) |
| `mistake_rate` | Probability of a deliberate typo + backspace |
| `max_retry` | Retries when the required speed is missed |
| `retry_boost_min/max` | Retry target = required speed + random(lo, hi) |
| `overhead_compensation` | Base interval multiplier; compensates for the extra pauses |
| `min_key_gap` | Hard floor between keystrokes (below ~60 ms the engine drops keys) |
| `drift_sigma`, `drift_floor`, `drift_ceil` | Slow random walk of the speed factor |
| `distracted_*` | Chance and length of an occasional long pause, in units |
| `word_pause_*` | Hesitation around spaces, in units (never in seconds) |

### `browser`

| Key | Meaning |
| --- | --- |
| `channel` | `auto` / `chrome` / `edge` / `chromium` |
| `exe_path` | Explicit browser executable (used by attach mode) |
| `profile_dir` | Profile directory; empty = `./browser_profile` |
| `debug_port` | Debug port for the attach mode |
| `viewport_*`, `locale` | Window size and UI language |
| `no_proxy` / `proxy_server` | Bypass or force a proxy |
| `extra_args` | Extra browser switches |

### `login`, `network`, `messages`, `result`

`login`: form selectors, submit label, env-var names, success URL fragment, timeout.
`network`: frame wait timeout, page timeout, retry-click/reload thresholds, hosts whose
failed requests are logged.
`messages`: the engine's own texts (error hints, retry button labels) — translate them for
a non-English instance.
`result`: the label words used on the result card (`字/分钟`, `要求速度`, …), so the parser
works on English instances too, plus the screenshot path.

---

## Command-line reference

| Flag | Effect |
| --- | --- |
| `--config PATH` | Use a specific config file instead of `./config.json` |
| `--print-config` | Print the effective config (after merging) and exit |
| `--init-config` | Write `config.json` from the built-in defaults and exit |
| `--wpm N` | Fixed target speed; disables the random range |
| `--wpm-range A,B` | Random target range for this run |
| `--jitter X` | Override rhythm jitter |
| `--mistake-rate X` | Override typo probability |
| `--max-retry N` | Override retry count |
| `--lesson ID` | Open a specific lesson directly (needs `site.lesson_url_template`) |
| `--auto-start` | Skip the "press Enter" confirmation |
| `--dry-run` | Extract the text only, send no keystrokes |
| `--browser …` | `auto` / `edge` / `chrome` / `chromium` |
| `--profile DIR` | Persistent profile (do not point it at your daily browser) |
| `--attach-browser` | Fallback: start a normal local browser with a debug port and attach |
| `--attach-kind …` | Which local browser the attach mode starts (`edge` / `chrome`) |
| `--attach-port N` | Attach to an already running debug port |
| `--no-proxy` / `--proxy-server …` | Force proxy behaviour |

---

## Notes from building it

**1. The text lives in a cross-origin iframe.** The course page is just a shell; the typing
engine runs in its own frame. `page.locator()` on the top document finds nothing — you must
walk `page.frames`, match the frame by URL substring, and query that frame instead.

**2. Rebuild the text from character nodes, not from `textContent`.** The structure is
`span.line > span.token > span.token_unit`. A word-final space is rendered as
`&nbsp;<i> </i>`, whose `textContent` is *two* characters (U+00A0 + space), so naive joining
inserts a stray space. Read only the first text node and map U+00A0 to a normal space.

**3. `keyboard.type()` beats `keyboard.press()` for characters.** `type()` emits
keydown / keypress / char / keyup and handles Shift for capitals and symbols — exactly what
the engine expects. Keep `press()` for functional keys (Enter, Backspace).

**4. A believable rhythm = base interval × key cost + noise + slow drift.** Base interval is
`60 / (WPM × 5)`; space costs 0.85×, capitals 1.35×, punctuation 1.45×. The important trick:
**every extra pause is expressed as a multiple of the unit interval, not as a fixed number of
seconds.** With fixed pauses, the faster you ask for, the larger the share of the time they
eat — you ask for 90 and measure 72. Expressed in units, the ratio stays constant.

**5. Progress is read from the DOM, not guessed.** Typed cells get a class (`_vld` / `_err`).
Counting them every N characters gives a real progress read-out *and* detects a lost keyboard
focus, which can be fixed by clicking the text area again.

---

## Troubleshooting

**"This Activity Didn't Load" (or the localized equivalent)**

Usually a proxy problem, not a script problem. A proxy client that was closed does **not**
mean the setting is gone: the system proxy can still point at a dead local port, and every
request routed through it fails — cross-origin frames are the last to fail, so they are the
first thing you notice.

The script prints a proxy health line at startup and can bypass the proxy:

```bash
python typing_auto.py --no-proxy
```

It also self-heals the common UI case: it clicks the retry button on the failed activity
screen, and reloads the lesson page after repeated failures.

**Eyeball control — the attach mode**

If the scripted browser still cannot load the activity, start a *normal* browser with a
debug port and let the script drive it. This process carries no automation flags, so iframe
behaviour matches your everyday browser:

```bash
python typing_auto.py --attach-browser            # starts a local Edge/Chrome
python typing_auto.py --attach-port 9222          # or attach to one you started yourself
```

**"No typing content found"** — often a video/game lesson rather than an article lesson, or
the page has not reached the article yet.

**Wrong text with `--dry-run`** — the front end probably changed; update
`selectors.unit_selector` (and `frame_keyword`) in `config.json`, no code change needed.

**Always "not passed"** — raise `typing.wpm_min/max`, or raise `max_retry`.

---

## Layout

```
typingclub-automation/
├─ typing_auto.py          # the whole automation, config-driven
├─ config.example.json     # template (committed)
├─ config.json             # your local settings (git-ignored)
├─ requirements.txt
├─ setup.bat / run.bat     # Windows helpers
├─ LICENSE                 # MIT
└─ .gitignore
```

## License

MIT — see [LICENSE](LICENSE).
