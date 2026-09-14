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
- **What is the phone, exactly?** The viewer URL path starts with
  `cuttlefis…`, but Settings → About phone reports device name
  `redroid15_arm64` (Android 15), i.e. redroid (Android-in-a-container), not
  Cuttlefish. Either way it is an AOSP image: no Play Services, no SIM,
  `Contacts` has no accounts. Worth stating plainly in the docs, and the
  `device` field in the session (`"shlut Android (default)"`) could carry the
  Android version.

## What fails / platform issues

- **Ended sessions keep occupying capacity until cleanup completes, and
  the create call then fails hard.** Sequence on 2026-09-14 ~16:40 PT: two
  sessions ended via `DELETE` (state `closing`, `cleanup_pending: true`),
  then a new `POST /sessions` a minute later →
  `409 This provider is at capacity; wait for the active phone's cleanup to finish.`
  The docs do say "capacity stays occupied until cleanup is confirmed", but
  (a) cleanup took several minutes for a phone that had only been provisioning,
  (b) `/me` still showed `available_session_slots: 5`, so the account-level
  number does not reflect provider capacity, and (c) there is no way to ask
  "when will a slot free up". For a multi-user service this means every
  "Start phone" can fail for reasons unrelated to the user; I now surface the
  message verbatim. Request: a `Retry-After` header on that 409, and either a
  faster cleanup path or `available_session_slots` that accounts for it.
- **The ADB endpoint changed shape three times in one day** (which is great
  responsiveness, and also exactly what breaks clients):
  1. morning docs: ssh-ed25519 key → SSH tunnel;
  2. ~15:00: `POST /adb` wants `adbkey.pub`, answers `{transport:"adb", host, port}`, plain `adb connect` works (the guide was updated to this by ~16:30);
  3. ~17:00: `POST /adb` with a body → `400 ADB enable takes no body; use /adb/reset to rotate the code`; no body → `{…, "code": "ph_…"}`; after `adb connect`, every shell command answers `locked: run adb shell unlock <code> first` until you run exactly that; wrong code keeps it locked; `POST /adb/reset` rotates the code. The guide still describes shape 2.
  My client now tries the key registration, falls back to the code flow on that 400, and runs `adb shell unlock` after connecting. Requests: version the endpoint (or the `transport` value), and put the unlock step in the guide and OpenAPI (`AdbConnection` has no `code`).
- **The ADB docs and the ADB endpoint disagree.** The guide and the OpenAPI
  schema say: register an `ssh-ed25519` public key, receive an SSH gateway
  (`username: shlut-adb`, pinned `host_key`, `forward_host`/`forward_port`),
  open an `ssh -N -L` tunnel, then `adb connect 127.0.0.1:15555`. The live
  endpoint on 2026-09-14 (session `4bd38736221c`) rejects an ed25519 key with
  `400 expected one ADB public key (the contents of adbkey.pub)`, and on
  success answers a different shape entirely:
  `{"enabled": true, "transport": "adb", "host": "live.phone-harness.com", "port": 22220, "expires_at": …}`.
  What actually works: send `~/.android/adbkey.pub`, then `adb connect
  live.phone-harness.com:22220`. No SSH at all; adbd authenticates the client
  by its RSA key. My client now tries the adb key first and only falls back to
  the documented SSH flow if the service asks for it. The direct path is
  simpler for users (no OpenSSH, no known_hosts) but the docs should say so,
  and `AdbConnection` in the OpenAPI should gain the `transport: "adb"`
  variant. Measured over that connection: `adb connect` 0.2 s, `screencap -p`
  0.8 s (vs 2.0 s for `screen.capture` over HTTP), `uiautomator dump` 2.5 s.

- **`apps.launch` answers HTTP 500 for anything it cannot launch.** Reproduced
  on session `2d136f1b0934` with three inputs: a package that is not installed
  (`com.definitely.not.installed`), a package that does not exist on this image
  (`com.android.dialer`), and an installed package with no launcher activity
  (`com.android.phone`). All three return
  `500 {"error": "The phone service could not complete this request."}`.
  A 500 reads as "the platform broke", so my agent initially aborted the whole
  run on it; it now treats op-level 5xx as feedback for the model (up to three
  in a row). Expected: `404` / `400` with a `code` like `package_not_found` or
  `not_launchable`, and ideally the closest installed match in the body.

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
| Read Android version + device name from Settings | 12 | 121 s | ✅ "Android 15, redroid15_arm64" (scrolling System missed About phone; agent fell back to Settings search) |

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
- **The phone has real internet, and the WebView shell is usable.** Given
  "Order a pizza from DoorDash" (deliberately impossible: no Play Store), the
  agent used the launcher's Quick Search Box, got web results, opened
  doordash.com in *WebView Browser Tester*, passed a "verifying you are human"
  interstitial without doing anything, and got as far as the delivery-address
  field before the site's app-scheme redirect dropped it on "Webpage not
  available". Two takeaways: (1) network egress is unrestricted, which is great
  for real tasks and worth documenting; (2) a proper browser (Chromium) would
  make the "no app installed" fallback path far more useful than the test
  shell.
- **Idle disconnect never happened.** Three runs with 10–30 s gaps between
  ops, no 409s, no session drops. Good.
- **`apps.launch` first call took 5.3 s** (cold app start), later ones 2.5 s.
  The op blocks until Android confirms the launch, which is exactly the right
  behaviour for an agent; just noting the number.

## Dashboard

- Sign-in state carried over ("Checking your sign-in…" → workspace) and the
  balance in the header matched `/me` to the cent. Sessions → Past lists each
  session with ready duration, end reason and charge, plus an Export CSV. That
  is exactly the audit trail I wanted after a day of agent runs: the three-task
  dev session shows `6m 45s · Ended by request · $2.45`.
- Sessions started from the API show up in the dashboard within a few seconds
  as `Starting`, then `Ready`, with an `Open` action that lands on the live
  viewer. Nice for watching an agent run without writing any viewer code.
- Small thing: the Active tab has no auto-refresh; I hit Refresh to see the
  state flip. A 5 s poll while a session is `Starting` would feel better.

## Things I'd ask for, ranked

1. A pooled/prepared start for beta accounts. 137 s cold start × every task
   iteration is the dominant cost of developing against the platform.
2. A composite observe op (`capture + tree + current app`) or HTTP/2
   pipelining guidance so a step is one round trip.
3. Unicode `input.text` (IME or clipboard-backed).
4. `apps.list` returning launchable apps by default.
5. Pre-granted runtime permissions / an option to start from a clean profile.
6. Distinct error codes for "op does not exist" vs "op unsupported here".

## ADB transport, in practice

Once connected (see the docs mismatch above), stock adb against the redroid
container behaved like a local device, with a few things worth knowing:

- `monkey -p <pkg> -c LAUNCHER 1` exits 251 with no output on this image, so
  launching is `cmd package resolve-activity --brief … <pkg>` then
  `am start -W -n <component>`. Works, 1.2–2.5 s.
- `uiautomator dump /dev/tty` must be read with `adb exec-out`, not
  `adb shell` (the pty path mangled the XML into nothing).
- `adb shell input text` is slow (4.7 s for "about phone") and typing straight
  after tapping Settings' search box lost the first two characters; a ~1 s
  settle before typing fixes it. The HTTP `input.text` op never dropped
  characters in my runs.
- `pm grant com.android.contacts android.permission.POST_NOTIFICATIONS` works,
  which removes the first-launch permission dialog the HTTP-only agent has to
  click through. That alone is a reason to offer a `grant` op over HTTP.
- `logcat -d` works: app crashes become diagnosable, which the QA use case
  needs.
- Revocation is clean: `DELETE /sessions/{sid}/adb` → `GET` returns
  `{"enabled": false}` and the TCP endpoint stops accepting.

## What I did not get to

- The SSH-tunnel variant of ADB from the docs: implemented, but the live
  service never offered it, so it is untested.
- APK upload: the agent drives preinstalled apps only.
- The Anthropic adapter is written and unit-tested against a stubbed SDK but
  every live run used Gemini, because that was the working key on this
  machine.
