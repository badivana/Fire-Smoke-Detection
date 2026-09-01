# Code Audit — Fire & Smoke Detection System

**Scope:** full repository at `e27c5ab` — Flask app (`app/`, `run.py`, `config.py`),
detection modules (`YOLOv8LiveCam.py`, `YOLOv8.py`), templates, packaging and docs.
**Date:** 2026-09-01

Every finding below was read out of the source and, where behaviour was in question,
reproduced locally. Line references are `file:line`.

---

## Summary

The application is a coherent, well-structured Flask project — blueprints, an ORM
layer, a service boundary around the detector, and a sensible template hierarchy.
The problems are concentrated in three places: **the detector degrades silently
when it cannot load the model**, **the deployment defaults are unsafe**, and
**several configurable settings are not actually wired to anything**.

The first of these matters most. This is life-safety software; a fire detector
that displays "Status: No suspicious activity" while running no model at all is
worse than one that refuses to start.

| Severity | Count |
|---|---|
| Critical / High | 7 |
| Medium | 13 |
| Low / hygiene | 13 |

---

## Critical & High

### H1. Model weights are an unfetched Git LFS pointer — detection silently disabled

`optimized150.pt` in a fresh clone is a 133-byte LFS pointer file, not a model:

```
version https://git-lfs.github.com/spec/v1
oid sha256:cbe5840f952adab362689edc00197a85ec7543841c9db5d91d3a09ace01c5e64
size 52026763
```

`get_detector` (`app/services/detector.py:105-116`) catches the resulting
`YOLO(...)` failure and substitutes `FallbackCameraDetector`, whose
`process_frame` (`app/services/detector.py:62-71`) **returns no detections, ever**.
The overlay then renders the green "Status: No suspicious activity" banner over a
live camera feed. The operator sees a working fire detector. It is not one.

The README never mentions `git lfs install && git lfs pull`, so this is the
default outcome of following the documented install steps.

**Fix:** fail loudly. A model that cannot be loaded should surface a red,
unmissable "DETECTION OFFLINE" state — never a silent green one. Document the LFS
step, and verify the weights file is a real checkpoint at startup (check magic
bytes / file size, not just existence).

### H2. The fallback path is itself broken — the video stream dies with a TypeError

`frame_generator` calls the detector with keyword arguments the fallback does not
accept:

- call site — `app/services/detector.py:216-220` → `process_frame(frame, fire_conf=…, smoke_conf=…)`
- fallback signature — `app/services/detector.py:62` → `process_frame(self, frame, conf=0.5)`

`TypeError: process_frame() got an unexpected keyword argument 'fire_conf'`. The
call is outside the loop's `try` (only `read_frame` is guarded, lines 199-204), so
the generator raises and the MJPEG response terminates mid-stream.

So the two failure modes compound: if the model loads, fine; if it does not, you
get either a silently-blind detector (H1) or a dead feed (H2) depending on which
code path runs first.

### H3. Debug server bound to all interfaces

`run.py:7` — `app.run(host='0.0.0.0', port=5000, debug=True)`.

The Werkzeug interactive debugger is reachable from the network. It is PIN-gated,
but the PIN is derived from predictable host values and the traceback pages leak
source, environment and configuration regardless. `debug=True` must never be the
committed default for a `0.0.0.0` bind.

### H4. Hardcoded `SECRET_KEY` fallback

`config.py:6` — `SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key'`.

Any deployment that forgets the env var signs session cookies with a value that is
public in this repository. An attacker forges a session cookie for user id 1 and
is logged in as admin. There is no signal that this has happened — the app starts
normally.

**Fix:** raise on a missing `SECRET_KEY` outside development.

### H5. Default admin credentials, recreated on every boot

`app/__init__.py:46-52` creates `admin` / `admin123` (env-overridable) inside
`create_app()` on every start, and `scripts/init_db.py:12-21` does the same.
Nothing forces a password change, and there is no UI to change one. Combined with
H4 this means a default deployment has two independent paths to full access.

### H6. No CSRF protection anywhere

`grep` for `csrf|flask_wtf|SESSION_COOKIE|SameSite` across the repo returns
nothing. Both state-changing endpoints are cookie-authenticated and unprotected:

- `POST /settings` — `app/routes/main.py:147-162`
- `POST /api/settings` — `app/routes/main.py:190-209` (accepts JSON, no origin check)

A cross-site request can repoint `email_receiver` (alerts go to the attacker),
change `camera_source`, or silence `alert_sound_enabled`. `/logout`
(`app/routes/main.py:69`) is also a GET, so it is trivially triggerable from any
page.

**Fix:** Flask-WTF `CSRFProtect`, `SESSION_COOKIE_SECURE`/`SAMESITE=Lax`, and make
logout a POST.

### H7. Unvalidated camera source reaches `cv2.VideoCapture` — SSRF and local file read

`app/routes/main.py:82` takes `?source=` straight from the query string and passes
it through `get_frame_generator` → `_normalize_source` → `cv2.VideoCapture`
(`app/services/detector.py:48`, `YOLOv8LiveCam.py:119`). `camera_source` from the
settings form (`app/routes/main.py:158`) takes the same path.

OpenCV's FFMPEG backend accepts `http://`, `https://`, `rtsp://` and filesystem
paths. An authenticated user can therefore make the server fetch arbitrary URLs
(including cloud metadata endpoints and internal hosts) or stream any video file
readable by the server process.

**Fix:** allowlist. Accept small integers for local devices and, if remote
cameras are needed, only URLs matching an operator-configured pattern.

---

## Medium

### M1. `detection_duration` is a setting that does nothing

`Settings.detection_duration` (`app/models.py:52`) is exposed in the UI
(`templates/settings.html:18`) and written by both settings endpoints — but it is
never read by the detector. `LiveDetector` hardcodes
`detection_threshold_fire = 2` / `detection_threshold_smoke = 3`
(`YOLOv8LiveCam.py:129-130`), and those thresholds are only consulted in the
standalone `__main__` block (`YOLOv8LiveCam.py:299,312`), never in the web path.

The web path instead dedupes on a hardcoded 5-second window
(`_should_log`, `app/services/detector.py:148-154`). So the accumulated
`detection_duration_fire/smoke` values are computed, packed into `meta`, and
discarded. Persistence-before-alerting — the whole point of a duration threshold,
and the main defence against false positives — is absent from the product.

### M2. Duration arithmetic assumes 30 fps that the loop cannot reach

`YOLOv8LiveCam.py:164,169` accumulate `1/30` per frame. The actual loop rate is
`time.sleep(0.03)` **plus** CPU inference (`device="cpu"`, `YOLOv8LiveCam.py:144`),
realistically 5-10 fps. Every duration is therefore understated by roughly 3-6×:
"2 seconds of fire" is really 6-12 seconds of fire before an alert fires. Measure
elapsed wall-clock time instead of counting frames.

### M3. Timezone mixing corrupts the "Today" statistic

Detections are stamped in Asia/Kolkata (`app/models.py:34-36`) while
`_get_stats` filters against `datetime.utcnow().date()` (`app/routes/main.py:20`)
and `_should_log` uses `datetime.utcnow()` (`app/services/detector.py:149`).
Measured skew: **5.5 hours**. Detections between 00:00 and 05:30 IST are counted
against the wrong day. `User.created_at` and `Alert.timestamp` use UTC
(`app/models.py:13,45`), so three different clocks coexist in one schema.

Also note SQLAlchemy's SQLite `DATETIME` silently drops the `tzinfo` — the offset
is not stored, so the ambiguity is unrecoverable after the fact.

**Fix:** store UTC everywhere (`timezone=True` columns), convert to local time in
the template layer only.

### M4. `Alert.sent_status` is never updated

`_persist_detection` writes every alert with `sent_status=False`
(`app/services/detector.py:141`), and nothing ever sets it to `True` — the email
thread (`app/services/detector.py:258-271`) does not report back. The `Alert`
table is write-only: it cannot answer "did this alert actually go out?", which is
the only question it exists to answer. Email failures inside the daemon thread are
likewise invisible (the `try` wraps `Thread.start()`, not the send).

### M5. A single module-global detector is shared across all viewers

`_detector` / `_detector_key` (`app/services/detector.py:13-14`) are module state.
With the threaded dev server or a threaded gunicorn worker, a second viewer
opening `/video_feed` with a different `?source=` causes `get_detector` to
`release()` the capture the first viewer's generator is still reading from
(`app/services/detector.py:99-104`). Each generator's `finally` also releases the
shared object (line 285). `_last_logged` is likewise global and unsynchronised.

Related: `gunicorn` is pinned in `requirements.txt`, but multiple worker
*processes* each opening the same `/dev/video0` will not work at all.

### M6. `/api/settings` writes unvalidated values straight into typed columns

`app/routes/main.py:195-200` assigns `payload.get(...)` with no coercion or range
check. Posting `{"fire_confidence": "high"}` stores a string in a `Float` column
(SQLite permits it), after which `min(fire_conf, smoke_conf)`
(`YOLOv8LiveCam.py:144`) raises `TypeError` and takes the video feed down. There
is also no clamp to `[0, 1]`.

### M7. Unhandled casts return 500s

- `app/routes/main.py:153-155` — `float()` / `int()` on form input; a blank or
  non-numeric field is an unhandled `ValueError`.
- `app/routes/main.py:97` — `int(request.args.get('page', 1))`; `?page=abc` is a 500.

### M8. Settings are snapshotted once per stream

`fire_conf` / `smoke_conf` are read before the loop starts
(`app/services/detector.py:193-196`). Changing thresholds in the UI has no effect
until the operator reloads the dashboard to restart the MJPEG stream — with no
indication that this is required. The `settings` row is also held on a session
that stays open for the entire lifetime of the stream.

### M9. The alarm plays on the server, not in the browser

`play_alert_sound` (`YOLOv8LiveCam.py:66-74`) calls `pygame.mixer` in a server-side
thread. On a headless host it is a no-op (the exception is swallowed, line 72);
on a desktop host it plays to whoever is standing next to the server, not to the
operator watching the dashboard remotely. For a web application the alarm belongs
in the client. Overlapping detections also restart the same mixer channel rather
than queueing.

### M10. Working-directory coupling makes deployment fragile

`from YOLOv8LiveCam import send_email, play_alert_sound`
(`app/services/detector.py:11`) resolves only when the repo root is on `sys.path`;
`weights='optimized150.pt'` (line 95), `Path('screenshots')` (line 76) and
`Path("app/static/screenshots")` (`YOLOv8LiveCam.py:226`) are all relative to the
current directory. Launching gunicorn from anywhere but the repo root breaks the
import and scatters snapshots. Resolve paths from `Path(__file__)`, and move the
email/sound helpers into `app/services/`.

### M11. `reports` loads the entire detections table into memory

`Detection.query.all()` (`app/routes/main.py:127`) then two full Python passes
(lines 129-140). This is a `GROUP BY date(timestamp), class_name` — one query,
constant memory. As written it degrades linearly and will eventually time out.

### M12. N+1 queries and unbounded pagination UI

`Camera.query.get()` per row in `index` (`app/routes/main.py:38`) and `history`
(line 109) — use a join. `templates/history.html:40` renders `range(1,
pagination.pages + 1)`: 10,000 detections produces 1,000 page links.

### M13. No retention policy, no rate limiting, no cookie hardening

Snapshots accumulate in `app/static/screenshots/` at up to one JPEG per 5 seconds
of detection, with no cleanup — unbounded disk growth on a long-running host.
The login form (`app/routes/main.py:51-66`) has no attempt limiting or lockout.
No `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE`, or
`login_manager.session_protection` is configured.

---

## Low / hygiene

### L1. `requirements.txt` is UTF-16LE with CRLF endings

The file begins `ff fe` — the artifact of `pip freeze > requirements.txt` in
PowerShell. pip itself copes (its `auto_decode` recognises the BOM, verified
locally), but the file renders as spaced-out garbage in editors, diffs and code
review, and non-pip consumers are not guaranteed to handle it. `python-dotenv`
is also listed twice — pinned at line 39, unpinned at line 54.

**Fix:** rewrite as UTF-8/LF and drop the duplicate.

### L2. `YOLOv8.py` runs inference at import time against a missing file

`YOLOv8.py:6-7` executes at module level, with no `if __name__ == '__main__'`
guard, against `test_pics/mumlar2.png` — a path that does not exist in the repo.
Anything that imports this module runs a model inference as a side effect. The
remaining 50 lines are commented-out code that belongs in version history.

### L3. Debug `print()` calls in the hot loop and in request handlers

`print("READING FRAME")` (`app/services/detector.py:200`) and
`print("PROCESS FRAME CALLED")` (`YOLOv8LiveCam.py:139`) run once per frame —
tens of lines per second per viewer, unbuffered, blocking. `print("Raw detections
count: …")` and the per-box print (`YOLOv8LiveCam.py:150,154`) add more.
`print("DB IMAGE PATH:", …)` sits inside the `history` handler
(`app/routes/main.py:112`). Replace with the `logging` module at appropriate
levels.

### L4. `_draw_status_overlay` is duplicated verbatim

`app/services/detector.py:163-184` and `YOLOv8LiveCam.py:77-99` are the same
function. The detector module imports two other helpers from `YOLOv8LiveCam`
already — import this one too, or move all three into `app/services/`.

### L5. Dead and misleading imports

- `from fileinput import filename` (`YOLOv8LiveCam.py:6`) and `from sys import prefix` (line 8) — IDE auto-import accidents, both unused.
- `from ultralytics import settings` (`app/services/detector.py:6`) — unused, and the name collides visually with the `Settings` model and the local `settings` variables used throughout the same file.
- `numpy` is used (line 158); `Alert`/`Camera` in routes are used.

### L6. `app/services/__init__.py` is missing

`app/routes/` has one, `app/services/` does not. It works via implicit namespace
packages, but it is inconsistent and will surprise packaging tools.

### L7. Camera records are cosmetic

`_get_or_create_camera` (`app/services/detector.py:119-125`) is always called with
the defaults `'Default Camera'` / `'0'`, regardless of the source actually in use,
so every detection is attributed to one fictional camera. `Camera.status`
(`app/models.py:26`) is set to `'online'` once at creation and never updated.

### L8. `datetime.utcnow()` is deprecated

`app/models.py:13,45`, `app/routes/main.py:20`, `app/services/detector.py:149`.
Deprecated since Python 3.12; use `datetime.now(timezone.utc)`.

### L9. `api_history` is a bare alias

`app/routes/main.py:178-181` returns `api_detections()` unchanged — either give it
distinct behaviour (filtering, pagination) or drop the route.

### L10. Login ignores the `next` parameter

`app/routes/main.py:62` always redirects to the dashboard, discarding the page
`login_required` bounced the user from.

### L11. Fallback snapshots land outside the static root

`FallbackCameraDetector.save_snapshot` (`app/services/detector.py:73-78`) writes to
`./screenshots/` and returns `screenshots/<file>`, while `LiveDetector`
(`YOLOv8LiveCam.py:226-243`) writes to `app/static/screenshots/` and returns the
same-looking relative path. The templates resolve it with
`url_for('static', filename=…)` (`templates/history.html:26`), so fallback
snapshots produce 404 links. The email attachment path
(`app/services/detector.py:268`) has the same ambiguity.

### L12. README is out of step with the code

- `cd Fire-and-Smoke-Detection` after cloning `Fire-Smoke-Detection` (line 59) — wrong directory name.
- No `git lfs install && git lfs pull` step, which is what makes H1 the default experience.
- Documents only `ALERT_EMAIL` / `ALERT_EMAIL_PASS`; omits `SECRET_KEY`, `ADMIN_USER`, `ADMIN_PASS`, `ADMIN_EMAIL`, `DATABASE_URL`, `RECEIVER_EMAIL`.
- Windows-only venv activation; no POSIX equivalent.
- "Epochs: 100" (line 49) against weights named `optimized150`.
- The dataset table (lines 39-44) is mangled pandoc output, not Markdown.
- No `.env.example`, despite `.gitignore:4` explicitly allowlisting one.

### L13. No tests, no CI, no lint configuration

No test suite, no `.github/workflows`, no ruff/flake8/mypy config. Every finding
above was found by reading; none of them would have been caught automatically.
`_normalize_source`, `_should_log` and the settings-validation paths are pure
functions with obvious test cases and are the cheapest place to start.

---

## Recommended order of work

1. **H1 + H2** — make model-load failure loud and fix the crashing fallback. Nothing else matters if the detector can be silently blind.
2. **H3, H4, H5, H6** — deployment and auth defaults. Small, mechanical, high value.
3. **H7, M6, M7** — input validation at the three entry points that reach OpenCV and the database.
4. **M1, M2, M3** — make the advertised settings real and the timestamps consistent.
5. **M4, M5, M8, M10** — alerting truthfulness and the concurrency/deployment model.
6. **L1, L2, L3, L12** — packaging and docs, so the next person can run this correctly.
7. **L13** — tests around the pure functions, then a CI job.
