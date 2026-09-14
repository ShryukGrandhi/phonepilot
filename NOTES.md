# NOTES — Phone Harness Cloud from a first user's seat

Written while building PhonePilot on 2026-09-14 against `api.phone-harness.com`
(OpenAPI version `2026-09-14`, provider `shlut`). Everything below was observed
directly; numbers are from my own sessions, not marketing.

## What works well

- **The API contract is unusually honest.** The OpenAPI descriptions say what
  the endpoint does *not* do ("Ready does not mean a browser frame has been
  displayed", "`steps` accepted but unused", "not an OCR score"). I built the
  whole client from `/docs/openapi.json` + `/docs/llms.txt` without opening a
  browser once. `llms.txt` is exactly what an agent-building user wants.
- **Idempotent create + recovery receipt.** `Idempotency-Key` on `POST /sessions`
  and `GET /sessions/requests/{key}` make a crash-safe "start phone" trivial.
  I verified the receipt still resolves after `DELETE` (state `closing`,
  `cleanup_complete: false`).
- **`tree` is good.** The accessibility hierarchy comes back with center
  coordinates you can pass straight to `input.tap`; my agent taps by node
  index instead of guessing pixels, which removed nearly all mis-taps.
- **Op latency is fine for an agent loop** (measured on session `e045035bcd20`):

  | op | round trip |
  |---|---|
  | `input.tap` | 0.5 s |
  | `nav.home` / `nav.back` / `nav.recents` | 0.8–1.0 s (includes the documented settle) |
  | `apps.launch` | 2.7 s |
  | `screen.capture` (720x1280 PNG, ~186 KB) | 2.0 s |
  | `GET frame.png` | 1.7 s |
  | `screen.text` | 3.0 s |
  | `tree` | 1.5–5.1 s |

- **Billing is transparent.** `/me` exposes `price_cents_per_minute` (35¢) and
  `balance_cents`; `/history` shows `cost_cents` per session. My probe session:
  172 s wall, 32 s ready → billed 1 minute. Provisioning is not billed, as
  documented.
- **Error bodies are actionable.** `unsupported: true` for chords / unknown ops,
  `409` with `state` while provisioning, `400` naming the op that rejected args.
- **Session ends are fast and safe.** `DELETE` returned `202 cleanup_pending`
  within ~1 s; `GET` afterwards shows `closing` with `close_requested` set.

## What's confusing

- **Cold start is ~2 min 15 s** (`startup: {mode: "cold", pool: "disabled"}`,
  measured 137 s and 139 s). The docs mention prepared/pooled starts but the
  pool is disabled for my account. A 2-minute wait before the first action is
  the single biggest UX cost; `session.startup` telling me *why* it was slow is
  nice, but I'd rather have the pool.
- **`apps.list` returns `[]` by default.** The launcher, Settings, Contacts,
  Clock etc. are all "system" apps on this image, so without
  `include_system: true` the list is empty. Suggest either defaulting
  `include_system` to true or adding a `launchable: true` filter that returns
  what the launcher shows.
- **Two "Contacts" icons on the home screen** (Lawnchair shows the same app
  twice) and a "Tap to set up" smartspace widget. Harmless, but it confused the
  model on the first turn ("I see two Contacts icons").
- **"Google" and "Play Store" icons are present but there are no Google
  services** (`com.google.*` absent, no Play, no Chrome). The only browser is
  the WebView shell. Either preinstall Chromium/Fennec or remove the dead icons.
- **`unsupported: true` is used for two different things**: an op this
  provider can't do (`input.keys` with a chord) and an op that doesn't exist at
  all (`{"op":"shell"}`). Same message for both. A distinct `code` would help.
- **`screen.text` `min_confidence` is accepted and ignored**, and `input.text`
  `delay` / `keystrokes` and `input.drag` `steps` are likewise no-ops. The docs
  say so, which is honest, but vestigial parameters invite bugs. Consider
  dropping them from the public schema.
- **`/me` vs `/account`.** The docs' "Get your account" page maps to `/me`; my
  first guess (`/account`) 404s with `no route GET /account`. Fine, but an
  alias would cost nothing.
- **Viewer URL is Cuttlefish** (`live.phone-harness.com/cuttlefis…`), so the
  "phone" is an AOSP emulator. Worth stating plainly in the docs: no Play
  Services, no SIM, `Contacts` has no accounts.

## What fails / platform issues

- **`input.text` cannot type non-ASCII.** Documented, but for a general phone
  agent it is a real gap: any name with an accent, any emoji, any non-Latin
  script silently loses characters (I fold to ASCII client-side). Request:
  accept UTF-8 and route through the IME, or add an `input.text_unicode` op
  that uses a clipboard paste.
- **Literal `%s` is rejected** with `shlut rejected the operation arguments`.
  I split it into `%` and `s` chunks client-side. This smells like an
  `adb shell input text` format-string quirk leaking through.
- **Snapshot coalescing bites the verify step.** `frame.png` is cached for up
  to 2 s, so a snapshot taken right after a tap can be the *pre-tap* frame. I
  use `screen.capture` (always fresh) for before/after diffs and pay ~2 s per
  step for it. A `?fresh=1` on `frame.png` or a `screen.capture` variant that
  returns JPEG would halve my per-step cost.

## Findings from live agent runs

Session `5d7008187bb9` (1200 s, three tasks back to back, Gemini 2.5 Flash):

| task | steps | wall | outcome |
|---|---|---|---|
| Add contact "Ada Lovelace" 555-0199 and confirm in list | 8 | 74 s | ✅ |
| Set a 6:30 AM alarm, list all alarms | 12 | 108 s | ✅ (self-corrected PM→AM) |
| Read Android version + device name from Settings | see run folder | | |

- **Per-step cost is dominated by the phone, not the model.** Flash answered in
  1.7–3 s; the phone side (action + settle + `screen.capture` + `tree` +
  `apps.current`) was 5–8 s. `tree` alone varies 1.5–5 s. If I could get one
  endpoint that returns `{capture, tree, current}` in a single round trip, an
  agent step would drop by ~3 s. **Feature request: a composite `observe` op.**
- **First launch of Contacts shows a runtime-permission dialog** ("Allow
  Contacts to send you notifications?"). The agent handled it (tapped ALLOW),
  but a QA harness would want a way to pre-grant permissions, e.g. a session
  create option `grant_all_permissions: true` or an ADB-free `pm grant` op.
- **The AOSP image ships with three preset alarms** (8:30 AM, 9:00 AM, and a
  weekend one). Fine, but "temporary phone" made me expect a clean profile.
  Document the image's starting state, or ship it empty.
- **Hair spaces in accessibility text.** Alarm labels come back as
  `'6:30 PM'` (U+200A between time and meridiem). Anyone matching on
  `"6:30 PM"` will miss. Not a bug in the API, but worth a note in the
  operations guide since `screen.text` is pitched as the matching surface.
- **Transitions are capturable mid-flight.** After tapping SAVE in Contacts,
  `screen.capture` returned a crossfade frame (the old form fading into the
  list, plus a toast). Since captures are already coalesced server-side, a
  `settle_ms` parameter on `screen.capture` ("wait until N consecutive frames
  match, up to a cap") would let clients stop guessing. I do that client-side
  now at ~2 s per extra capture.
- **Idle disconnect never happened.** Three runs with 10–30 s gaps between
  ops, no 409s, no session drops. Good.
- **`apps.launch` first call took 5.3 s** (cold app start), later ones 2.5 s.
  The op blocks until Android confirms the launch, which is exactly the right
  behaviour for an agent; just noting the number.

## Things I'd ask for, ranked

1. A pooled/prepared start for beta accounts. 137 s cold start × every task
   iteration is the dominant cost of developing against the platform.
2. A composite observe op (`capture + tree + current app`) or HTTP/2
   pipelining guidance so a step is one round trip.
3. Unicode `input.text` (IME or clipboard-backed).
4. `apps.list` returning launchable apps by default.
5. Pre-granted runtime permissions / an option to start from a clean profile.
6. Distinct error codes for "op does not exist" vs "op unsupported here".

## What I did not get to

- ADB over the SSH tunnel: read the guide and the schema, did not open a tunnel
  (the HTTP ops covered everything the agent needed).
- APK upload: the agent drives preinstalled apps only.
- The Anthropic adapter is written and unit-tested against a stubbed SDK but
  every live run used Gemini, because that was the working key on this
  machine.
