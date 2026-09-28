"""CLI for the Pod 2 AIOps auto-healing prototype.

  python -m autoheal scenarios
  python -m autoheal run --scenario stale-cache
  python -m autoheal run --scenario oom-hotfix                       # interactive SRE prompt
  python -m autoheal run --scenario oom-hotfix --decision pause      # leave it paused, approve later:
  python -m autoheal approve INC-... --approver sre.alice            #   ...from another terminal/process
  python -m autoheal run --scenario oom-hotfix --decision modify --memory 2Gi
  python -m autoheal run --scenario stale-cache --repeat 3           # semantic cache demo
  python -m autoheal demo                                            # every scenario, non-interactive
  python -m autoheal audit --verify
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

from . import agents  # noqa: E402
from .audit import AUDIT  # noqa: E402
from .graph import ensure_cloud_for, open_app, pending_approval, resume_incident, start_incident  # noqa: E402
from .infra_sim import list_scenarios, load_scenario  # noqa: E402
from .telemetry import METRICS, render_trace_tree  # noqa: E402

BAR = "─" * 100


def print_card(card: dict) -> None:
    pa = card["proposed_action"]
    print(f"\n┌{BAR}\n│ 🔒 SRE APPROVAL REQUIRED  {card['title']}")
    print(f"│ incident: {card['incident_id']}   target: {card['target']}   risk: {card['risk']}")
    print(f"│ why a human: {card.get('hitl_reason')}")
    print(f"│ root cause ({card['confidence']}): {card['root_cause']}")
    print(f"│ action: {pa['tool']} [{pa['intent']}]   patch_id: {pa['patch_id']}")
    print(f"│ command: {pa['command_preview']}")
    for line in (card.get("diff") or "").splitlines():
        print(f"│   {line}")
    print(f"│ blast radius: {card['blast_radius']}\n│ rollback: {card['rollback_plan']}")
    if card.get("guardrail_findings"):
        print(f"│ ⚠ guardrail findings: {len(card['guardrail_findings'])} quarantined injection line(s)")
    print(f"│ options: {' / '.join(card['options'])}   (expires in {card['expires_in_s']}s)\n└{BAR}")


def ask_decision() -> dict:
    while True:
        d = input("SRE decision [approve/reject/modify/pause]: ").strip().lower()
        if d in ("approve", "a"):
            return {"decision": "approve", "approver": input("approver id [sre.alice]: ").strip() or "sre.alice"}
        if d in ("reject", "r"):
            return {"decision": "reject", "approver": "sre.alice", "comment": input("reason: ").strip()}
        if d in ("modify", "m"):
            mem = input("new memory limit [2Gi]: ").strip() or "2Gi"
            return {"decision": "modify", "approver": "sre.alice", "changes": {"memory_limit": mem}}
        if d == "pause":
            return {"decision": "pause"}


def scripted(decision: str, approver: str, memory: str, comment: str) -> list[dict]:
    approve = {"decision": "approve", "approver": approver, "comment": comment or "LGTM, rollback ready"}
    if decision == "approve":
        return [approve]
    if decision == "reject":
        return [{"decision": "reject", "approver": approver, "comment": comment or "Change freeze window"}]
    if decision == "modify":
        return [{"decision": "modify", "approver": approver, "changes": {"memory_limit": memory}}, approve]
    return [{"decision": "pause"}]


async def drive(app, inc_id: str, result: dict, decisions: list[dict] | None) -> dict:
    queue = list(decisions or [])
    while "__interrupt__" in result:
        card = result["__interrupt__"][0].value
        print_card(card)
        decision = queue.pop(0) if queue else (ask_decision() if decisions is None else {"decision": "pause"})
        if decision["decision"] == "pause":
            print(f"\n⏸  Paused. State is checkpointed in SQLite; safe to exit. Resume later with:\n"
                  f"   python -m autoheal approve {inc_id} --approver sre.alice\n"
                  f"   python -m autoheal reject  {inc_id} --comment \"...\"\n"
                  f"   python -m autoheal modify  {inc_id} --memory 2Gi")
            return result
        print(f"   ↳ SRE decision: {decision}")
        result = await resume_incident(app, inc_id, decision)
    return result


def final_status(result: dict) -> str:
    return "PAUSED (awaiting SRE approval)" if "__interrupt__" in result else result.get("status", "?")


def report(show_trace: bool, metrics_out: str | None = None) -> None:
    print(f"\n{BAR}\n📊 Metrics")
    summary = METRICS.summary()
    print(json.dumps(summary, indent=2))
    ok, n, msg = AUDIT.verify_chain()
    if metrics_out:
        from datetime import datetime, timezone
        summary |= {"audit_chain_valid": ok, "audit_records": n,
                    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with open(metrics_out, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"💾 metrics written to {metrics_out}")
    print(f"🔗 Audit chain: {'VALID' if ok else 'BROKEN'} ({n} records, {msg}) → {AUDIT.path}")
    if show_trace:
        print(f"\n🧭 OpenTelemetry trace\n{render_trace_tree()}")


async def cmd_run(args) -> None:
    decisions = None if args.decision == "ask" else scripted(args.decision, args.approver, args.memory, args.comment)
    async with open_app() as app:
        for i in range(args.repeat):
            sc = load_scenario(args.scenario)
            print(f"\n{BAR}\n▶ Scenario '{args.scenario}' run {i + 1}/{args.repeat}: {sc['description']}\n{BAR}")
            inc_id, result = await start_incident(app, args.scenario)
            result = await drive(app, inc_id, result, decisions)
            print(f"■ Final status: {final_status(result)}")
    report(args.trace)


async def cmd_resume(args, kind: str) -> None:
    async with open_app() as app:
        await ensure_cloud_for(app, args.incident)
        if not await pending_approval(app, args.incident):
            sys.exit(f"{args.incident} is not waiting for approval")
        decision = {"approve": {"decision": "approve", "approver": args.approver, "comment": args.comment},
                    "reject": {"decision": "reject", "approver": args.approver, "comment": args.comment},
                    "modify": {"decision": "modify", "approver": args.approver,
                               "changes": {"memory_limit": args.memory}}}[kind]
        print(f"▶ Resuming {args.incident} from checkpoint with {decision}")
        result = await resume_incident(app, args.incident, decision)
        result = await drive(app, args.incident, result, [])
        print(f"■ Final status: {final_status(result)}")
    report(args.trace)


async def cmd_pending(args) -> None:
    async with open_app() as app:
        card = await pending_approval(app, args.incident)
        print_card(card) if card else print("nothing pending")


async def cmd_demo(args) -> None:
    async with open_app() as app:
        plan = [("stale-cache", scripted("approve", "sre.alice", "", "")),
                ("stale-cache", scripted("approve", "sre.alice", "", "")),   # second run → semantic cache hit
                ("oom-hotfix", scripted("approve", "sre.alice", "", "")),
                ("oom-hotfix", scripted("modify", "sre.bob", "2Gi", "")),
                ("prompt-injection", scripted("approve", "sre.alice", "", "")),
                ("unknown-alert", [])]
        for name, decisions in plan:
            print(f"\n{BAR}\n▶ {name}: {load_scenario(name)['description']}\n{BAR}")
            inc_id, result = await start_incident(app, name)
            result = await drive(app, inc_id, result, decisions)
            print(f"■ Final status: {final_status(result)}")
    report(args.trace, args.metrics_out)


def cmd_check_llm(args) -> None:
    """One cheap call per tier to prove the key, models and JSON mode work before a live demo."""
    import os
    import time
    from .config import SETTINGS
    from .llm import LiteLLMBackend

    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set in this terminal. Set it first, e.g.  $env:OPENAI_API_KEY = \"sk-...\"")
    backend = LiteLLMBackend()
    for tier, model in ((1, SETTINGS.tier1_model), (2, SETTINGS.tier2_model)):
        t0 = time.perf_counter()
        try:
            out = backend.complete(model, "classify", 'Return JSON {"ok": true, "confidence": 1}.', {})
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"✖ Tier {tier} ({model}) failed: {type(exc).__name__}: {exc}")
        usage = out.pop("_usage", {})
        print(f"✔ Tier {tier} {model}: {out} · {usage.get('prompt', 0)}+{usage.get('completion', 0)} tokens · "
              f"{time.perf_counter() - t0:.2f}s")
    print("Ready: run with  $env:AUTOHEAL_LLM = \"litellm\"")


def cmd_audit(args) -> None:
    for e in AUDIT.entries(args.incident)[-args.tail:]:
        print(f"{e['ts']} {e['incident_id']} {e['actor']:<18} {e['event']:<34} {json.dumps(e['details'])[:140]}")
    ok, n, msg = AUDIT.verify_chain()
    print(f"\n🔗 chain {'VALID' if ok else 'BROKEN'}: {n} records ({msg})")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    p = argparse.ArgumentParser(prog="autoheal", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scenarios")
    sub.add_parser("check-llm", help="verify OPENAI_API_KEY and both model tiers with one tiny call each")
    u = sub.add_parser("ui", help="open the local web console (http://127.0.0.1:8080)")
    u.add_argument("--port", type=int, default=8080)
    u.add_argument("--no-browser", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--scenario", required=True, choices=list_scenarios())
    r.add_argument("--decision", default="ask", choices=["ask", "approve", "reject", "modify", "pause"])
    r.add_argument("--approver", default="sre.alice")
    r.add_argument("--memory", default="2Gi")
    r.add_argument("--comment", default="")
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--trace", action="store_true", help="print the OpenTelemetry span tree")
    for kind in ("approve", "reject", "modify"):
        s = sub.add_parser(kind)
        s.add_argument("incident")
        s.add_argument("--approver", default="sre.alice")
        s.add_argument("--comment", default="")
        s.add_argument("--memory", default="2Gi")
        s.add_argument("--trace", action="store_true")
    pe = sub.add_parser("pending")
    pe.add_argument("incident")
    d = sub.add_parser("demo")
    d.add_argument("--trace", action="store_true")
    d.add_argument("--metrics-out", help="write the metrics summary as JSON (feeds the NFR workbook)")
    a = sub.add_parser("audit")
    a.add_argument("--incident")
    a.add_argument("--tail", type=int, default=60)
    a.add_argument("--verify", action="store_true")

    args = p.parse_args()
    if args.cmd == "scenarios":
        for name in list_scenarios():
            print(f"{name:<18} {load_scenario(name)['description']}")
    elif args.cmd == "check-llm":
        cmd_check_llm(args)
    elif args.cmd == "ui":
        from .web import serve
        serve(args.port, open_browser=not args.no_browser)
    elif args.cmd == "run":
        asyncio.run(cmd_run(args))
    elif args.cmd in ("approve", "reject", "modify"):
        asyncio.run(cmd_resume(args, args.cmd))
    elif args.cmd == "pending":
        asyncio.run(cmd_pending(args))
    elif args.cmd == "demo":
        asyncio.run(cmd_demo(args))
    elif args.cmd == "audit":
        cmd_audit(args)


if __name__ == "__main__":
    agents.set_emitter(print)
    main()
