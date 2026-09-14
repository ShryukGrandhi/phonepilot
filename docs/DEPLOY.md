# Deploying PhonePilot for several people

`phonepilot serve` is the multi-user mode: accounts, invite-only signup,
per-user phones and runs, encrypted per-user API keys. One process, SQLite,
no external services. Sized for a team or a class, not for the internet at
large; the scale-out path is at the end.

## 1. Run it

```bash
cp .env.example .env
phonepilot serve --generate-master-key      # paste into .env as PHONEPILOT_MASTER_KEY
echo DOMAIN=phones.example.com >> .env
docker compose up -d --build
```

Caddy terminates TLS with an automatic Let's Encrypt certificate for
`DOMAIN` and proxies to the app. The app runs as an unprivileged user with
`/data` as the only writable volume. (The Dockerfile was written but not
built on the development machine, which had no Docker daemon running; the
non-Docker path below is what was exercised.)

Without Docker:

```bash
export PHONEPILOT_MASTER_KEY=$(phonepilot serve --generate-master-key)
phonepilot serve --host 127.0.0.1 --port 8080 --data ./data
# put nginx/Caddy in front for TLS and add --secure-cookies --trust-proxy
```

First visit `https://DOMAIN/login`, create the first account (it becomes
admin, no invite needed), open Settings → "New invite code" for each person
you want to let in. Or from the shell: `phonepilot serve --data ./data --invite`.

## 2. Keys: bring-your-own or pooled

Each user pastes their own Phone Harness key and a model key (Gemini or
Anthropic) in Settings. Keys are sealed with Fernet under
`PHONEPILOT_MASTER_KEY` before they touch the database; the UI only ever
sees a hint like `pck_3…56cc`.

If you'd rather run everyone on one Phone Harness account, set
`PHONEPILOT_POOL_PHONE_KEY` (and a pooled model key). Users' own keys still
take precedence when present. Pooled mode is where the quotas matter:

| env | default | meaning |
|---|---|---|
| `PHONEPILOT_MAX_PHONES_PER_USER` | 1 | concurrent phones per account |
| `PHONEPILOT_DAILY_PHONE_MINUTES` | 90 | ready-time per user per rolling 24 h |
| `PHONEPILOT_MAX_STEPS` | 25 | agent steps per task |
| `PHONEPILOT_MAX_SESSION_TIMEOUT` | 1800 | longest phone lifetime a user may request |
| `PHONEPILOT_TRANSPORT` | http | `adb` to drive phones over adb (needs adb in the image; it is) |

Phone Harness itself caps sessions per account (6 on the beta account I
used), so pooled mode with more than a handful of simultaneous users needs
either more accounts or a queue.

## 3. What isolates users from each other

- **Identity on every request.** The session cookie (random 256-bit token,
  stored only as a SHA-256 hash, HttpOnly, SameSite=Strict, Secure over TLS)
  resolves to a user before any phone, event stream, frame, or run file is
  touched. There is no anonymous read path except `/login` and `/healthz`.
- **One runtime per user.** `UserRuntime` holds that user's Phone Harness
  client, model client, phone, agent, and event hub. Requests can only reach
  the runtime for the cookie's user.
- **Phone ownership is recorded, not inferred.** Every session id is written
  to the `phones` table with its owner at creation. Attach, frame, and task
  endpoints check that table; guessing another user's session id gets 403.
- **Run data lives per user.** Traces and screenshots are written under
  `data/users/<uid>/runs/`. Serving a file requires the run to be in the
  `runs` table for that user *and* the resolved path to stay inside the
  user's folder.
- **Events don't cross.** Each user has their own SSE hub; there is no global
  broadcast.
- **The phone is disposable.** Ending a session deletes the Android
  container and everything on it (Phone Harness guarantee); users never share
  a phone.
- **Secrets stay server-side.** Keys are encrypted at rest, decrypted only to
  construct a client, never logged, never returned to the browser. Passwords
  are scrypt-hashed with per-user salts.
- **Web hardening.** CSP without external origins, `frame-ancestors 'none'`,
  `nosniff`, no referrer, custom `X-PhonePilot` header required on every
  POST (CSRF), 64 KB body cap, login rate-limited per IP+email, access log
  disabled so URLs and bodies are not written to disk.
- **Audit trail.** `audit` table records signup/login/key changes/phone
  start/end/task start with timestamps and user ids (never key material).

What it does **not** do: the model provider sees screenshots of the user's
phone (that is inherent to a vision agent; choose the provider accordingly),
and one compromised master key exposes all stored API keys (rotate it by
re-encrypting; keep it out of the image and in a secret store).

## 4. Backups and rotation

- Back up the `/data` volume (SQLite in WAL mode: copy `phonepilot.sqlite3`
  plus `-wal`/`-shm`, or use `sqlite3 .backup`).
- Rotate `PHONEPILOT_MASTER_KEY` by decrypting each `user_keys.enc` with the
  old key and re-sealing with the new one (a 10-line script against
  `SecretBox`).
- Revoking a user: set `users.disabled = 1`; their cookies stop resolving
  immediately.

## 5. Beyond one box

When a single process is not enough: move the runtime out of the web tier
into workers fed by a queue (Redis), publish events through Redis pub/sub,
store runs in Postgres and screenshots in object storage behind signed URLs,
and keep a warm pool of provisioned phones per account to hide the ~2 min
cold start. The `UserRuntime`/`Hub` boundaries are already where those seams
go.
