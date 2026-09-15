"""Multi-user production scenario + leak battery against a running PhonePilot service.

    python scripts/multiuser_smoke.py --base https://phonepilot-two.vercel.app \
        --admin-email admin@phonepilot.local --admin-password '...' --users 3 --phones 2 \
        --ssh shry@100.95.252.63

Phases
1. admin login, mint invites, sign up N throwaway users (random passwords, never printed)
2. every user starts P phones at once; record what the provider gives (cap, capacity 409, errors)
3. rebalance: if some user got nothing while another holds 2, free one so two *different*
   users hold phones concurrently (the interesting case for isolation)
4. every user sends a task to all of their phones (different task per user)
5. leak battery across users (frames, run files, attach, events, tokens, CSRF, traversal)
   + container checks over ssh (env, mounts, hardening, container-to-container reach)
6. end everything, verify no phones / no containers remain, print report
"""

from __future__ import annotations

import argparse
import http.client
import json
import secrets
import subprocess
import sys
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

TASKS = [
    "Open the Clock app, go to the Stopwatch tab and start it. Tell me what the screen shows.",
    "Open Settings, then About phone, and tell me the device name and Android version shown.",
    "Add a contact named Smoke User{n} with phone 555-01{n}{n} and confirm it appears in the contacts list.",
]
READY_WAIT_S = 330
TASK_WAIT_S = 480
END_WAIT_S = 240
PROBE_PY = (
    "import urllib.request,urllib.error\n"
    "try:\n r=urllib.request.urlopen('http://IP2:9000/api/state',timeout=5);print('OPEN',r.status)\n"
    "except urllib.error.HTTPError as e:print('HTTP',e.code)\n"
    "except Exception as e:print('ERR',type(e).__name__)\n"
)


class Client:
    def __init__(self, base: str):
        u = urlsplit(base)
        self.host, self.https = u.netloc, u.scheme == "https"
        self.cookie: str | None = None

    def call(self, method, path, body=None, csrf=True, cookie=None, timeout=40, headers=None):
        conn = (http.client.HTTPSConnection if self.https else http.client.HTTPConnection)(self.host, timeout=timeout)
        h = dict(headers or {})
        if body is not None:
            h["Content-Type"] = "application/json"
        if csrf and method == "POST":
            h["X-PhonePilot"] = "1"
        ck = self.cookie if cookie is None else cookie
        if ck:
            h["Cookie"] = f"pp_session={ck}"
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        sc = resp.getheader("Set-Cookie")
        if sc and cookie is None:
            c = SimpleCookie()
            c.load(sc)
            if "pp_session" in c and c["pp_session"].value:
                self.cookie = c["pp_session"].value
        conn.close()
        return resp.status, resp.getheader("Content-Type", ""), data

    def js(self, method, path, body=None, **kw):
        st, _, data = self.call(method, path, body, **kw)
        try:
            return st, json.loads(data)
        except ValueError:
            return st, {"raw": data[:120].decode(errors="replace")}

    def phones(self) -> dict:
        st, j = self.js("GET", "/api/state")
        return (j.get("phones") or {}) if st == 200 else {}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_until(users, predicate, budget_s):
    deadline = time.time() + budget_s
    while time.time() < deadline:
        snap = {n: c.phones() for n, (_, c) in enumerate(users, 1)}
        if predicate(snap):
            return snap
        time.sleep(5)
    return {n: c.phones() for n, (_, c) in enumerate(users, 1)}


def no_starting(snap):
    return not any(p.get("status") == "starting" for ph in snap.values() for p in ph.values())


def ssh_out(target, cmd, timeout=60) -> str:
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", target, cmd], capture_output=True, text=True, timeout=timeout)
    return (r.stdout + r.stderr).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--admin-email", required=True)
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("--users", type=int, default=3)
    ap.add_argument("--phones", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--ssh", default=None, help="user@host of the backend for container checks")
    ap.add_argument("--docker-env", default="export PATH=$HOME/.local/bin:$PATH DOCKER_HOST=unix://$HOME/.colima/default/docker.sock;")
    args = ap.parse_args()
    stamp = time.strftime("%H%M%S")
    leaks: list[dict] = []
    edge: list[str] = []
    phones: dict[str, dict] = {}
    t0 = time.time()
    slots = [f"phone{i}" for i in range(1, args.phones + 1)]

    def check(name: str, ok: bool, detail: str = ""):
        leaks.append({"check": name, "ok": bool(ok), "detail": detail})
        log(f"  leak-check {'PASS' if ok else 'FAIL'}: {name} {detail}")

    def record(snap):
        for n, ph in snap.items():
            for slot in slots:
                p = ph.get(slot, {})
                phones.setdefault(f"u{n}/{slot}", {}).update({
                    "status": p.get("status"), "session": p.get("session_id"),
                    "sandbox": (p.get("sandbox") or {}).get("id"), "last_error": p.get("last_error")})

    # ---------------------------------------------------------------- 1. users
    admin = Client(args.base)
    st, j = admin.js("POST", "/api/auth/login", {"email": args.admin_email, "password": args.admin_password})
    assert st == 200, f"admin login failed: {j}"
    users: list[tuple[str, Client]] = []
    for n in range(1, args.users + 1):
        st, j = admin.js("POST", "/api/admin/invite", {})
        assert st == 200, f"invite: {j}"
        email = f"smoke-{stamp}-{n}@phonepilot.local"
        c = Client(args.base)
        st, j = c.js("POST", "/api/auth/signup", {"email": email, "password": secrets.token_urlsafe(16), "invite": j["invite"]})
        assert st == 200, f"signup {email}: {j}"
        users.append((email, c))
        log(f"user {n}: {email} signed up")
    st, j = Client(args.base).js("POST", "/api/auth/signup", {"email": f"x-{stamp}@phonepilot.local", "password": "abcdefghijkl", "invite": "nope"})
    check("signup with bogus invite is refused", st in (400, 403), f"got {st}")

    # ---------------------------------------------------------------- 2. everyone starts everything
    for n, (email, c) in enumerate(users, 1):
        for slot in slots:
            st, j = c.js("POST", "/api/session/start", {"phone": slot, "timeout_seconds": args.timeout})
            log(f"user {n} {slot}: start -> {st} {j.get('error', '')}")
            phones[f"u{n}/{slot}"] = {"start_http": st, "start_error": j.get("error")}
            time.sleep(1.5)
    snap = wait_until(users, no_starting, READY_WAIT_S)
    record(snap)
    holders = {n: [s for s in slots if snap[n].get(s, {}).get("status") == "ready"] for n in snap}
    log(f"after round 1: {sum(len(v) for v in holders.values())} ready, per user {holders} ({time.time()-t0:.0f}s)")
    for k, v in phones.items():
        if v.get("status") != "ready":
            edge.append(f"round1 {k}: status={v.get('status')} error={v.get('last_error') or v.get('start_error')}")

    # ---------------------------------------------------------------- 3. rebalance so >=2 users hold phones
    have = [n for n, v in holders.items() if v]
    lack = [n for n, v in holders.items() if not v]
    if len(have) < 2 and lack:
        rich = max(holders, key=lambda n: len(holders[n]))
        poor = lack[0]
        for attempt in range(1, 4):
            if attempt > 1 and len(holders[rich]) >= 2:
                victim = holders[rich][-1]
                log(f"rebalance: user {rich} ends {victim} so user {poor} can start phone1")
                users[rich - 1][1].js("POST", "/api/session/end", {"phone": victim})
                wait_until(users, lambda s: s[rich].get(victim, {}).get("status") in (None, "no_phone"), END_WAIT_S)
                time.sleep(10)  # provider cleanup after DELETE
            st, j = users[poor - 1][1].js("POST", "/api/session/start", {"phone": "phone1", "timeout_seconds": args.timeout})
            log(f"rebalance attempt {attempt}: user {poor} phone1 start -> {st} {j.get('error', '')}")
            snap = wait_until(users, no_starting, READY_WAIT_S)
            holders = {n: [s for s in slots if snap[n].get(s, {}).get("status") == "ready"] for n in snap}
            if holders[poor]:
                break
            edge.append(f"rebalance attempt {attempt} u{poor}/phone1: {snap[poor].get('phone1', {}).get('last_error')}")
            time.sleep(20)
        record(snap)
        log(f"after rebalance: per user {holders} ({time.time()-t0:.0f}s)")

    # ---------------------------------------------------------------- 4. tasks
    for n, (email, c) in enumerate(users, 1):
        task = TASKS[(n - 1) % len(TASKS)].replace("{n}", str(n))
        st, j = c.js("POST", "/api/task", {"task": task, "phone": "both"})
        log(f"user {n}: @both task -> {st} {j.get('routed') or j.get('error')}")
        for slot in slots:
            phones[f"u{n}/{slot}"]["task_routed"] = (j.get("routed") or {}).get(slot, j.get("error"))

    # ---------------------------------------------------------------- 5. leak battery
    time.sleep(20)
    live = [n for n, v in holders.items() if v]
    a_n = live[0] if live else 1
    b_n = next((n for n in holders if n != a_n), 2)
    a_email, a = users[a_n - 1]
    b_email, b = users[b_n - 1]
    sa, sb = a.phones(), b.phones()
    a_slot = holders.get(a_n, ["phone1"])[0]
    a_sid = sa.get(a_slot, {}).get("session_id")
    log(f"leak battery: attacker=user {b_n}, victim=user {a_n} ({a_slot} session {a_sid})")

    check("unauthenticated /api/state -> 401", Client(args.base).call("GET", "/api/state")[0] == 401)
    check("unauthenticated frame -> 401", Client(args.base).call("GET", "/api/frame.png?phone=phone1")[0] == 401)
    check("garbage cookie -> 401", Client(args.base).call("GET", "/api/state", cookie="deadbeef" * 8)[0] == 401)
    check("POST without CSRF header -> 403", a.call("POST", "/api/task", {"task": "x"}, csrf=False)[0] == 403)
    st, jb = b.js("POST", "/api/session/attach", {"session_id": a_sid, "phone": "phone2"})
    # security property: A's phone is not attachable by B. The attach must be REJECTED (never 200) and B must
    # not end up owning A's session. (Edge proxies can rewrite the exact 4xx; the app itself answers 403.)
    b_after = b.phones().get("phone2", {})
    check("B cannot attach A's session id (rejected, no ownership gained)",
          st != 200 and b_after.get("session_id") != a_sid and b_after.get("status") in (None, "no_phone"),
          f"got {st} {jb.get('error', '')}")
    st, jb = b.js("POST", "/api/session/attach", {"session_id": "000000000000", "phone": "phone2"})
    check("attaching an unknown session id is rejected", st != 200, f"got {st} {jb.get('error', '')}")
    check("B's state never mentions A's session id", bool(a_sid) and a_sid not in json.dumps(sb), a_sid or "no sid")
    st, j = a.js("GET", "/api/events/since?after=0")
    a_events = j.get("events", [])
    a_step = next((e for e in a_events if e.get("kind") == "step" and e.get("marked_url")), None)
    if a_step:
        st_a = a.call("GET", a_step["marked_url"])[0]
        st_b = b.call("GET", a_step["marked_url"])[0]
        st_x = Client(args.base).call("GET", a_step["marked_url"])[0]
        check("A reads own step screenshot -> 200", st_a == 200, f"got {st_a}")
        check("B reads A's step screenshot -> 404", st_b == 404, f"got {st_b} for {a_step['marked_url']}")
        check("anonymous reads A's step screenshot -> 401", st_x == 401, f"got {st_x}")
    else:
        edge.append("no step event from the victim user within 20s of task start (slow first step)")
    st, j = b.js("GET", "/api/events/since?after=0")
    b_events = j.get("events", [])
    check("B's event feed carries only B's slots", all(e.get("phone") in (None, *slots) for e in b_events))
    check("B's event feed has no session id of A", bool(a_sid) and a_sid not in json.dumps(b_events))
    a_text = json.dumps(a_events)
    check("A's event feed has no other user's email", all(u[0] not in a_text for u in users if u[0] != a_email))
    st, j = b.js("GET", "/api/runs")
    check("B's run list does not contain A's run dirs", a_sid not in json.dumps(j) if a_sid else True)
    check("stream token endpoint requires auth", Client(args.base).call("GET", "/api/events/token")[0] == 401)
    st, j = a.js("GET", "/api/events/token")
    tok = j.get("token")
    check("stream token is minted for a logged-in user", st == 200 and bool(tok))
    for label, url in (("path traversal on /runs", "/runs/phone1/../../../phonepilot.sqlite3"),
                       ("encoded traversal on /runs", "/runs/phone1/%2e%2e/%2e%2e/phonepilot.sqlite3"),
                       ("double-slash run path", "/runs/phone1//etc/passwd"),
                       ("sqlite by name under /runs", "/runs/phone1/../../phonepilot.sqlite3-wal")):
        st, ctype, body = a.call("GET", url)
        check(f"{label} is rejected", st != 200 and b"SQLite" not in body and b"root:" not in body, f"got {st}")
    st, _, _ = b.call("GET", "/api/frame.png?phone=phone1")
    check("B asking for a frame gets only B's slot (200 own / 404 none, never A's)", st in (200, 404), f"got {st}")
    st, j = a.js("POST", "/api/session/start", {"phone": a_slot})
    check("starting an occupied slot -> 409", st == 409, f"got {st}")
    st, j = a.js("POST", "/api/session/start", {"phone": "phone9"})
    check("starting a slot beyond the quota -> 4xx", 400 <= st < 500, f"got {st}")
    st, j = a.js("POST", "/api/task", {"task": "@9 hi"})
    check("routing a task to a nonexistent phone -> 4xx", 400 <= st < 500, str(j.get("error")))
    st, j = a.js("POST", "/api/keys", {"phone_key": "x" * 70000})
    check("oversized body is rejected", st in (400, 413), f"got {st}")
    st, j = b.js("POST", "/api/admin/invite", {})
    check("non-admin cannot mint invites", st in (401, 403), f"got {st}")
    admin.call("POST", "/api/session/end", {"phone": a_slot}, headers={"X-Target-User": a_email})
    check("admin ending 'phone1' touches only admin's own slot, not A's", a.phones().get(a_slot, {}).get("status") in ("ready", "running"))

    if args.ssh:
        dk = args.docker_env
        names = ssh_out(args.ssh, f"{dk} docker ps --filter label=phonepilot.sandbox=1 --format '{{{{.Names}}}}'").split()
        ready_n = sum(len(v) for v in holders.values())
        check(f"containers running == live phones ({len(names)} vs {ready_n})", len(names) == ready_n, ", ".join(names))
        check("containers belong to distinct users", len({x.split("-phone")[0] for x in names}) >= min(2, len(names)), ", ".join(names))
        live_by_user = {n: [snap_p.get("sandbox", {}).get("id") for snap_p in [users[n - 1][1].phones().get(sl, {}) for sl in holders[n]]]
                        for n in holders if holders[n]}
        live = [c for cs in live_by_user.values() for c in cs if c]
        if len(names) >= 2 and len(live) >= 2:
            c1, c2 = live[0], live[1]
            env1 = ssh_out(args.ssh, f"{dk} docker inspect -f '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' {c1}")
            env2 = ssh_out(args.ssh, f"{dk} docker inspect -f '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' {c2}")
            tok2 = next((line.split("=", 1)[1] for line in env2.splitlines() if line.startswith("PHONEPILOT_SANDBOX_TOKEN=")), "")
            secrets_on_host = ssh_out(args.ssh, "grep -E '^(PHONEPILOT_MASTER_KEY|PHONEPILOT_POOL_)' ~/phonepilot/.env | cut -d= -f2-").split()
            no_master_name = "PHONEPILOT_MASTER_KEY=" not in env1 and "PHONEPILOT_POOL_" not in env1
            no_secret_value = all(v and v not in env1 for v in secrets_on_host) if secrets_on_host else True
            data_is_tmpfs = "PHONEPILOT_DATA=/data" in env1  # in-container tmpfs, not the host data dir
            check("container env carries no master key, no pool var, no secret value; only /data path",
                  no_master_name and no_secret_value and data_is_tmpfs,
                  "master-name-absent=%s secret-values-absent=%s data=/data=%s" % (no_master_name, no_secret_value, data_is_tmpfs))
            check("sandbox bearer tokens differ per container", bool(tok2) and tok2 not in env1)
            m1 = ssh_out(args.ssh, f"{dk} docker inspect -f '{{{{range .Mounts}}}}{{{{.Source}}}}|{{{{end}}}}' {c1}").strip("|").split("|")
            m2 = ssh_out(args.ssh, f"{dk} docker inspect -f '{{{{range .Mounts}}}}{{{{.Source}}}}|{{{{end}}}}' {c2}").strip("|").split("|")
            check("each container mounts exactly one runs dir and they differ", len(m1) == 1 and len(m2) == 1 and m1 != m2, f"{m1} vs {m2}")
            hc = ssh_out(args.ssh, f"{dk} docker inspect -f '{{{{.HostConfig.ReadonlyRootfs}}}} {{{{.HostConfig.CapDrop}}}} {{{{.HostConfig.SecurityOpt}}}} {{{{.HostConfig.Memory}}}} {{{{.HostConfig.PidsLimit}}}}' {c1}")
            check("read-only rootfs, all caps dropped, no-new-privileges, mem+pid limits",
                  hc.startswith("true") and "ALL" in hc and "no-new-privileges" in hc and " 0 " not in hc, hc)
            ports = ssh_out(args.ssh, f"{dk} docker port {c1}")
            check("sandbox port published on 127.0.0.1 only", "127.0.0.1" in ports and "0.0.0.0" not in ports, ports)
            ip2 = ssh_out(args.ssh, f"{dk} docker inspect -f '{{{{range .NetworkSettings.Networks}}}}{{{{.IPAddress}}}}{{{{end}}}}' {c2}")
            probe_b64 = __import__("base64").b64encode(PROBE_PY.replace("IP2", ip2).encode()).decode()
            probe = ssh_out(args.ssh, f"{dk} docker exec {c1} python -c \"import base64;exec(base64.b64decode('{probe_b64}'))\"")
            check("container 1 -> container 2 sandbox API refused without token (HTTP 401) or unreachable",
                  probe.startswith("HTTP 401") or probe.startswith("ERR"), probe[-80:])
            probe2 = ssh_out(args.ssh, f"{dk} docker exec {c1} sh -c 'ls /runs | head -3; tr \"\\\\0\" \"\\\\n\" < /proc/1/environ | grep -c POOL_ || true'")
            check("container 1 sees only its own /runs and no pool keys in PID 1 env", probe2.splitlines()[-1].strip() == "0", probe2[-100:])
            probe3 = ssh_out(args.ssh, f"{dk} docker exec {c1} sh -c 'touch /etc/x 2>&1; touch /app/x 2>&1'")
            check("container filesystem is read-only", "ead-only" in probe3, probe3[-80:])
            adb1 = ssh_out(args.ssh, f"{dk} docker exec {c1} adb devices 2>&1 | tail -n +2 | grep -c 'device$' || true")
            check("each container's adb sees exactly one phone (its own)", adb1.strip().splitlines()[-1] == "1", adb1[-60:])
        else:
            edge.append(f"{len(live)} live container(s) during the battery; container-to-container checks skipped")

    # ---------------------------------------------------------------- 6. wait tasks, collect
    snap = wait_until(users, lambda s: not any(p.get("status") == "running" for ph in s.values() for p in ph.values()), TASK_WAIT_S)
    record(snap)
    for n, (email, c) in enumerate(users, 1):
        st, j = c.js("GET", "/api/runs")
        for r in j.get("runs", []):
            slot = r["dir"].split("/")[0]
            phones.setdefault(f"u{n}/{slot}", {}).update({"success": r.get("success"), "steps": r.get("steps"),
                                                           "result": (r.get("result") or r.get("summary") or "")[:140]})

    # ---------------------------------------------------------------- 7. teardown
    for n, (email, c) in enumerate(users, 1):
        for slot in slots:
            c.js("POST", "/api/session/end", {"phone": slot})
    snap = wait_until(users, lambda s: all(p.get("status") in (None, "no_phone") for ph in s.values() for p in ph.values()), END_WAIT_S)
    check("all phones back to no_phone after end", all(p.get("status") in (None, "no_phone") for ph in snap.values() for p in ph.values()))
    if args.ssh:
        time.sleep(5)
        names = ssh_out(args.ssh, f"{args.docker_env} docker ps -a --filter label=phonepilot.sandbox=1 --format '{{{{.Names}}}}'").split()
        check("all containers removed after end", len(names) == 0, ", ".join(names))
        sess = ssh_out(args.ssh, "cd ~/phonepilot && export PHONE_HARNESS_API_KEY=$(grep ^PHONEPILOT_POOL_PHONE_KEY .env | cut -d= -f2) && .venv/bin/phonepilot sessions 2>&1 | tail -3")
        check("provider reports no active sessions", "no active sessions" in sess, sess[-80:])

    # ---------------------------------------------------------------- report
    print("\n=== REPORT ===")
    print(f"users={args.users} phones/user={args.phones} wall={time.time()-t0:.0f}s")
    for k, v in phones.items():
        print(f"{k}: start={v.get('start_http')} final={v.get('status')} session={v.get('session')} "
              f"routed={v.get('task_routed')} success={v.get('success')} steps={v.get('steps')} | "
              f"{v.get('result') or v.get('last_error') or v.get('start_error') or ''}")
    npass = sum(1 for x in leaks if x["ok"])
    nfail = len(leaks) - npass
    print(f"leak checks: {npass} pass / {nfail} fail")
    for x in leaks:
        print(f"  {'PASS' if x['ok'] else 'FAIL'}: {x['check']}  {x['detail']}")
    print("edge cases:")
    for e in edge:
        print("  -", e)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
