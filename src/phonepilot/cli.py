"""Command-line entry points.

    phonepilot run "Open Settings and turn on dark theme"
    phonepilot chat                      # several tasks on one phone
    phonepilot web                       # browser UI with live phone view
    phonepilot start | sessions | end SID | shot SID out.png | op SID tree | viewer SID
    phonepilot account | history
    phonepilot report RUN_DIR | video RUN_DIR
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from rich.console import Console

from . import __version__
from .cloud import CloudError, PhoneHarnessClient

console = Console(highlight=False, soft_wrap=True)


def log(msg: str) -> None:
    console.print(msg, markup=False)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles/redirects default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(find_dotenv(usecwd=True))
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 2
    try:
        return args.func(args) or 0
    except CloudError as exc:
        console.print(f"[red]API error:[/red] {exc}")
        if exc.payload:
            console.print(json.dumps(exc.payload, indent=2), markup=False)
        return 1
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1


# ---------------------------------------------------------------- commands
def cmd_run(args) -> int:
    from .agent import Agent, AgentConfig, new_trace
    from .brain import make_brain
    from .device import Device
    from .sessions import leased

    brain = make_brain(args.brain, args.model)
    log(f"phonepilot {__version__} · brain {brain.name}/{brain.model}")
    with PhoneHarnessClient() as client:
        price = _price(client)
        with leased(client, args.session, args.timeout, args.keep, log) as lease:
            device = Device(client, lease.session)
            trace = new_trace(args.task, lease.session.id, brain, Path(args.runs_dir), price, lease.ready_wait_s)
            agent = Agent(device, brain, trace, AgentConfig(max_steps=args.max_steps), log)
            outcome = agent.run(args.task)
    _print_outcome(outcome)
    return 0 if outcome.success else 1


def cmd_chat(args) -> int:
    from .agent import Agent, AgentConfig, new_trace
    from .brain import make_brain
    from .device import Device
    from .sessions import leased

    brain = make_brain(args.brain, args.model)
    log(f"phonepilot {__version__} · brain {brain.name}/{brain.model}")
    with PhoneHarnessClient() as client:
        price = _price(client)
        with leased(client, args.session, args.timeout, args.keep, log) as lease:
            device = Device(client, lease.session)
            log("Phone ready. Type a task, or /shot, /apps, /viewer, /end. Ctrl-D to quit.")
            while True:
                left = device.seconds_left()
                try:
                    task = console.input(f"[bold cyan]phone[/bold cyan] ({int(left) if left else '?'}s left) › ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not task:
                    continue
                if task in ("/end", "/quit", "/exit"):
                    break
                if task == "/shot":
                    p = Path("shot.png")
                    device.screenshot().save(p)
                    log(f"saved {p.resolve()}")
                    continue
                if task == "/apps":
                    log("\n".join(device.apps(include_system=True)))
                    continue
                if task == "/viewer":
                    log(client.owner_viewer(device.sid)["url"])
                    continue
                trace = new_trace(task, lease.session.id, brain, Path(args.runs_dir), price, lease.ready_wait_s)
                outcome = Agent(device, brain, trace, AgentConfig(max_steps=args.max_steps), log).run(task)
                _print_outcome(outcome)
    return 0


def cmd_web(args) -> int:
    from .brain import make_brain
    from .web.server import serve

    brain = make_brain(args.brain, args.model)
    log(f"phonepilot {__version__} · brain {brain.name}/{brain.model}")
    with PhoneHarnessClient() as client:
        serve(client, brain, Path(args.runs_dir), args.host, args.port, args.session, args.max_steps,
              open_browser=not args.no_open, log=log)
    return 0


def cmd_start(args) -> int:
    """Provision a phone. Progress goes to stderr; stdout carries only the session id,
    so `SID=$(phonepilot start)` works in scripts."""
    from .sessions import acquire

    err = Console(highlight=False, soft_wrap=True, stderr=True)
    with PhoneHarnessClient() as client:
        lease = acquire(client, None, args.timeout, lambda m: err.print(m, markup=False))
    sys.stdout.write(lease.session.id + "\n")
    sys.stdout.flush()
    return 0


def cmd_sessions(args) -> int:
    with PhoneHarnessClient() as client:
        sessions = client.list_sessions()
    if not sessions:
        log("no active sessions")
        return 0
    for s in sessions:
        left = s.seconds_left()
        log(f"{s.id}  {s.state:<12} screen={s.screen}  left={int(left) if left is not None else '?'}s")
    return 0


def cmd_end(args) -> int:
    from .sessions import release

    with PhoneHarnessClient() as client:
        for sid in args.session_ids:
            release(client, sid, log)
    return 0


def cmd_shot(args) -> int:
    from .device import Device

    with PhoneHarnessClient() as client:
        device = Device(client, client.get_session(args.session_id))
        img = device.screenshot()
        if args.marks:
            from .observe import draw_marks, select_elements

            elements = select_elements(device.tree(), (device.width, device.height))
            img = draw_marks(img, elements)
            for e in elements:
                log(e.describe())
        img.save(args.out)
    log(f"saved {Path(args.out).resolve()}")
    return 0


def cmd_op(args) -> int:
    kw = {}
    for pair in args.kw:
        k, _, v = pair.partition("=")
        kw[k] = _literal(v)
    with PhoneHarnessClient() as client:
        result = client.op(args.session_id, args.op, **kw)
    if isinstance(result, dict) and "png_b64" in result:
        result = {**result, "png_b64": f"<{len(result['png_b64'])} chars>"}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_viewer(args) -> int:
    with PhoneHarnessClient() as client:
        v = client.owner_viewer(args.session_id)
    print(v["url"])
    if args.open:
        webbrowser.open(v["url"])
    return 0


def cmd_account(args) -> int:
    with PhoneHarnessClient() as client:
        print(json.dumps(client.account(), indent=2))
    return 0


def cmd_history(args) -> int:
    with PhoneHarnessClient() as client:
        items = client.history(args.limit)
    for h in items:
        log(f"{h['sid']}  {h['usage_minutes']:>3} min  {h['cost_cents']/100:>6.2f} USD  {h.get('end_reason') or ''}")
    return 0


def cmd_report(args) -> int:
    p = Path(args.run_dir) / "report.html"
    if not p.exists():
        raise ValueError(f"no report at {p}")
    webbrowser.open(p.resolve().as_uri())
    return 0


def cmd_video(args) -> int:
    from .video import render_video

    out = render_video(Path(args.run_dir), Path(args.out) if args.out else None, seconds_per_step=args.seconds)
    log(f"wrote {out}")
    return 0


# ----------------------------------------------------------------- helpers
def _print_outcome(outcome) -> None:
    color = "green" if outcome.success else ("red" if outcome.success is False else "yellow")
    console.print(f"\n[{color}]{'SUCCESS' if outcome.success else ('FAILED' if outcome.success is False else 'INCOMPLETE')}[/{color}] "
                  f"after {outcome.steps} steps: {outcome.summary}", markup=True)
    if outcome.result:
        console.print(f"[bold]Result:[/bold] {outcome.result}", markup=True)
    if outcome.error:
        console.print(f"[red]Error:[/red] {outcome.error}", markup=True)
    console.print(f"trace: {outcome.run_dir / 'report.html'}", markup=False)


def _price(client: PhoneHarnessClient) -> int | None:
    try:
        return int(client.account().get("price_cents_per_minute"))
    except (CloudError, TypeError, ValueError):
        return None


def _literal(v: str):
    try:
        return json.loads(v)
    except ValueError:
        return v


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="phonepilot", description="Natural-language phone agent on Phone Harness Cloud")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    def brain_opts(sp):
        sp.add_argument("--brain", choices=["anthropic", "gemini"], default=os.environ.get("PHONEPILOT_BRAIN"))
        sp.add_argument("--model", default=None)
        sp.add_argument("--max-steps", type=int, default=25)
        sp.add_argument("--runs-dir", default="runs")

    def session_opts(sp):
        sp.add_argument("--session", "-s", default=None, help="reuse an existing ready session id")
        sp.add_argument("--timeout", type=int, default=900, help="new session lifetime in seconds")
        sp.add_argument("--keep", action="store_true", help="do not end a session this command created")

    r = sub.add_parser("run", help="run one natural-language task")
    r.add_argument("task")
    brain_opts(r); session_opts(r)
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("chat", help="interactive: many tasks on one phone")
    brain_opts(c); session_opts(c)
    c.set_defaults(func=cmd_chat)

    w = sub.add_parser("web", help="browser UI: live phone view, chat box, step log")
    brain_opts(w)
    w.add_argument("--session", "-s", default=None, help="attach an existing ready session")
    w.add_argument("--host", default="127.0.0.1"); w.add_argument("--port", type=int, default=8765)
    w.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
    w.set_defaults(func=cmd_web)

    s = sub.add_parser("start", help="create a session, wait for ready, print its id")
    s.add_argument("--timeout", type=int, default=900)
    s.set_defaults(func=cmd_start)

    sub.add_parser("sessions", help="list active sessions").set_defaults(func=cmd_sessions)

    e = sub.add_parser("end", help="end session(s)")
    e.add_argument("session_ids", nargs="+")
    e.set_defaults(func=cmd_end)

    sh = sub.add_parser("shot", help="save a screenshot (optionally with element marks)")
    sh.add_argument("session_id"); sh.add_argument("out", nargs="?", default="shot.png")
    sh.add_argument("--marks", action="store_true")
    sh.set_defaults(func=cmd_shot)

    o = sub.add_parser("op", help="run a raw phone operation, e.g. op SID input.tap x=360 y=640")
    o.add_argument("session_id"); o.add_argument("op"); o.add_argument("kw", nargs="*")
    o.set_defaults(func=cmd_op)

    v = sub.add_parser("viewer", help="print (and optionally open) the live viewer URL")
    v.add_argument("session_id"); v.add_argument("--open", action="store_true")
    v.set_defaults(func=cmd_viewer)

    sub.add_parser("account", help="show account, balance, price").set_defaults(func=cmd_account)
    h = sub.add_parser("history", help="recent finished sessions and cost")
    h.add_argument("--limit", type=int, default=20)
    h.set_defaults(func=cmd_history)

    rp = sub.add_parser("report", help="open a run's HTML report")
    rp.add_argument("run_dir")
    rp.set_defaults(func=cmd_report)

    vd = sub.add_parser("video", help="render a run to mp4 (needs ffmpeg)")
    vd.add_argument("run_dir"); vd.add_argument("--out", default=None)
    vd.add_argument("--seconds", type=float, default=2.5, help="seconds per step")
    vd.set_defaults(func=cmd_video)
    return p


if __name__ == "__main__":
    sys.exit(main())
