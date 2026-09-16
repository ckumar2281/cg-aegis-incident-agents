"""
Command-line entry point.

    python -m aegis.cli list                       what scenarios exist
    python -m aegis.cli run schema_drift           one incident, live
    python -m aegis.cli run --all                  the whole suite
    python -m aegis.cli run null_explosion -i      you play the approvers
    python -m aegis.cli run --all --report         write the HTML trace
    python -m aegis.cli graph                      print the graph as Mermaid

"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .approvals import ConsoleResponder, ScriptedResponder
from .audit import TraceBus
from .config import Settings, load_settings
from .contracts import IncidentOutcome, IncidentState
from .graph import build_graph, build_runtime, run_incident
from .platform import SimulatedPlatform, load_scenario
from .platform.scenarios import ALL_SCENARIOS, SCENARIOS_BY_KEY
from .precedent import PrecedentStore

console = Console()

#: Colour per trace event kind. Keeps the live output scannable rather than a wall.
_STYLES = {
    "quarantine": "yellow",
    "triage": "bold cyan",
    "specialist": "cyan",
    "reasoning": "dim",
    "rca": "bold magenta",
    "planner": "blue",
    "autonomy": "yellow",
    "disclosure": "bold green",
    "disclosure_blocked": "bold red",
    "disclosure_released": "bold green",
    "gate_open": "bold yellow",
    "gate_response": "yellow",
    "gate_closed": "bold yellow",
    "execute": "green",
    "rollback": "bold red",
    "pull_request": "bold blue",
    "execution_complete": "bold green",
    "escalate": "bold red",
    "rejected": "bold red",
    "deferred": "bold yellow",
    "precedent": "bold magenta",
    "autonomous": "bold green",
    "handoff": "dim white",
}

_TERMINAL_STYLE = {
    IncidentState.RESOLVED: "bold green",
    IncidentState.REJECTED_QUARANTINED: "bold red",
    IncidentState.DEFERRED_BACKLOG: "bold yellow",
    IncidentState.ESCALATED: "bold red",
}


def _printer(verbose: bool):
    def show(event) -> None:
        if event.kind == "reasoning" and not verbose:
            return
        if event.kind == "handoff" and not verbose:
            return
        style = _STYLES.get(event.kind, "white")
        timing = f" [dim]{event.duration_ms}ms[/dim]" if event.duration_ms > 30 else ""
        console.print(
            f"  [{style}]{event.kind:<20}[/{style}] "
            f"[dim]{event.actor:<24}[/dim] {event.message}{timing}"
        )

    return show


def _run_one(
    key: str,
    settings: Settings,
    *,
    interactive: bool,
    verbose: bool,
    quiet: bool = False,
) -> tuple[IncidentOutcome, Any, dict]:
    scenario, world, signals = load_scenario(key)
    trace = TraceBus()
    if not quiet:
        console.print()
        console.print(Rule(f"[bold]{scenario.key}[/bold]  ·  {scenario.title}", style="dim"))
        console.print(f"  [dim]{scenario.narrative}[/dim]")
        console.print()
        trace.subscribe(_printer(verbose))

    responder = ConsoleResponder() if interactive else ScriptedResponder(
        scenario.scripted_responses
    )
    store = PrecedentStore(
        precedents=list(scenario.seed_precedents(world.now)) if scenario.seed_precedents else []
    )
    outcome, rt, final = run_incident(
        settings=settings,
        platform=SimulatedPlatform(world),
        signals=signals,
        incident_id=f"INC-{key.upper()}",
        responder=responder,
        trace=trace,
        precedents=store,
    )

    if not quiet:
        _summary(outcome, rt, final, scenario)
    return outcome, rt, final


def _summary(outcome: IncidentOutcome, rt, final, scenario) -> None:
    gt = scenario.ground_truth
    style = _TERMINAL_STYLE.get(outcome.final_state, "white")

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column()

    table.add_row("outcome", f"[{style}]{outcome.final_state.value.replace('_', ' ')}[/{style}]")
    table.add_row(
        "severity",
        f"{outcome.severity.value if outcome.severity else '-'}  "
        f"[dim](expected {gt.expected_severity.value})[/dim]",
    )
    table.add_row(
        "root cause",
        f"{outcome.root_cause_tag}  [dim]({outcome.confidence:.0%} confidence)[/dim]",
    )
    packet = final.get("packet")
    if packet:
        table.add_row(
            "alerts",
            f"{len(packet.correlated_alert_ids) + len(packet.suppressed_alert_ids)} in "
            f"→ 1 incident  [dim]({len(packet.suppressed_alert_ids)} folded as cascade, "
            f"{packet.dedup_ratio:.0%} dedup)[/dim]",
        )
        table.add_row("blast radius", packet.blast_radius.summary())
    table.add_row("rounds", str(final.get("round", 1)))

    bundle = final.get("bundle")
    if bundle:
        table.add_row(
            "redaction",
            "[green]business brief passed the firewall[/green]"
            if bundle.redaction_passed
            else f"[red]BLOCKED: {len(bundle.redaction_findings)} findings[/red]",
        )
        table.add_row(
            "technical tier",
            "[green]released after business approval[/green]"
            if bundle.technical_released
            else "[yellow]withheld — never sent[/yellow]",
        )

    for gate in (outcome.business_gate, outcome.technical_gate):
        if gate:
            table.add_row(
                gate.gate.value.replace("_", " "),
                f"{gate.verdict.value.upper()}  [dim]{gate.rationale}[/dim]",
            )

    if outcome.execution:
        ex = outcome.execution
        table.add_row(
            "remediation",
            f"{ex.executed}/{len(ex.step_results)} steps verified"
            + (f"  ·  PR {ex.pull_request.url}" if ex.pull_request else ""),
        )
        table.add_row("residual risk", f"[dim]{ex.residual_risk}[/dim]")
    if outcome.ticket:
        table.add_row("ticket", f"{outcome.ticket.key}  [dim]{outcome.ticket.url}[/dim]")
    if outcome.backlog_entry:
        table.add_row("backlog", outcome.backlog_entry.high_level_change[:90])

    table.add_row(
        "audit chain",
        f"{outcome.audit_events} events  "
        + ("[green]valid[/green]" if outcome.audit_chain_valid else "[red]BROKEN[/red]"),
    )
    ledger = rt.ledger.summary()
    table.add_row(
        "cost",
        f"${ledger['usd']:.4f}  ·  {ledger['llm_calls']} model calls"
        + ("  [yellow](degraded — budget ceiling hit)[/yellow]" if ledger["degraded"] else ""),
    )
    table.add_row("elapsed", f"{outcome.elapsed_ms}ms")

    console.print()
    console.print(Panel(table, title="[bold]outcome[/bold]", border_style=style, expand=False))

    # Show who was told what -- the tiered-disclosure evidence.
    if rt.email.sent:
        mail = Table(box=None, padding=(0, 2), show_header=True, header_style="dim")
        mail.add_column("recipient", style="dim")
        mail.add_column("tier")
        mail.add_column("subject")
        for message in rt.email.sent:
            tier_style = "green" if message.tier.value == "business" else "blue"
            mail.add_row(
                escape(message.role or "notification"),
                f"[{tier_style}]{message.tier.value}[/{tier_style}]",
                # Escaped: subjects start with "[resolved]" / "[deferred]", which Rich
                # would otherwise swallow as markup tags.
                escape(message.subject[:70]),
            )
        console.print(Panel(mail, title="[bold]who was told what[/bold]",
                            border_style="dim", expand=False))


def _grade(outcome: IncidentOutcome, scenario) -> tuple[bool, str]:
    gt = scenario.ground_truth
    problems = []
    if outcome.root_cause_tag != gt.root_cause_tag:
        problems.append(f"cause={outcome.root_cause_tag or 'none'}")
    if outcome.final_state != gt.expected_final_state:
        problems.append(f"state={outcome.final_state.value}")
    if not outcome.severity or abs(outcome.severity.rank - gt.expected_severity.rank) > gt.severity_tolerance:
        problems.append(f"sev={outcome.severity.value if outcome.severity else 'none'}")
    if not outcome.audit_chain_valid:
        problems.append("audit chain broken")
    return not problems, ", ".join(problems)


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aegis", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list the seeded scenarios")
    sub.add_parser("graph", help="print the orchestration graph as Mermaid")

    run = sub.add_parser("run", help="run one or more incidents end to end")
    run.add_argument("scenario", nargs="?", help="scenario key (see `list`)")
    run.add_argument("--all", action="store_true", help="run every scenario")
    run.add_argument("-i", "--interactive", action="store_true",
                     help="answer the approval gates yourself")
    run.add_argument("-v", "--verbose", action="store_true",
                     help="show reasoning and hand-off events too")
    run.add_argument("--bedrock", action="store_true",
                     help="force the Bedrock backend regardless of .env")
    run.add_argument("--report", metavar="PATH", nargs="?", const="runs/report.html",
                     help="write an HTML trace report")

    args = parser.parse_args(argv)

    if args.command == "list":
        table = Table(title="Seeded incident scenarios")
        table.add_column("key", style="bold cyan")
        table.add_column("tests")
        table.add_column("ends as", style="dim")
        for scenario in ALL_SCENARIOS:
            table.add_row(
                scenario.key,
                scenario.title,
                scenario.ground_truth.expected_final_state.value.replace("_", " "),
            )
        console.print(table)
        return 0

    if args.command == "graph":
        settings = load_settings()
        scenario, world, signals = load_scenario("schema_drift")
        rt = build_runtime(
            settings=settings, platform=SimulatedPlatform(world),
            incident_id="X", responder=ScriptedResponder({}),
        )
        try:
            console.print(build_graph(rt).get_graph().draw_mermaid())
        except Exception as exc:
            console.print(f"[red]could not render: {exc}[/red]")
            return 1
        return 0

    settings = load_settings()
    if args.bedrock:
        settings.model_backend = "bedrock"

    backend = Text()
    backend.append("reasoning: ", style="dim")
    backend.append(
        settings.model_backend,
        style="bold green" if settings.model_backend == "bedrock" else "bold yellow",
    )
    if settings.model_backend == "bedrock":
        backend.append(f"  ({settings.aws_region})", style="dim")
    else:
        backend.append("  (deterministic, $0 — use --bedrock for real models)", style="dim")
    backend.append(f"    platform: {settings.platform_backend}", style="dim")
    console.print(backend)

    keys = (
        [s.key for s in ALL_SCENARIOS]
        if args.all
        else [args.scenario] if args.scenario else None
    )
    if not keys:
        console.print("[red]give a scenario key, or --all[/red]")
        return 2
    for key in keys:
        if key not in SCENARIOS_BY_KEY:
            console.print(f"[red]unknown scenario {key!r}[/red]")
            return 2

    runs: list[tuple[str, IncidentOutcome, Any, dict]] = []
    for key in keys:
        outcome, rt, final = _run_one(
            key, settings, interactive=args.interactive, verbose=args.verbose
        )
        runs.append((key, outcome, rt, final))
    outcomes = [(k, o) for k, o, _, _ in runs]

    if len(outcomes) > 1:
        console.print()
        console.print(Rule("[bold]suite[/bold]", style="dim"))
        table = Table(box=None, padding=(0, 2))
        table.add_column("scenario", style="bold cyan")
        table.add_column("outcome")
        table.add_column("sev", justify="center")
        table.add_column("confidence", justify="right")
        table.add_column("", style="dim")
        passed = 0
        for key, outcome in outcomes:
            ok, why = _grade(outcome, SCENARIOS_BY_KEY[key])
            passed += ok
            table.add_row(
                key,
                f"[{_TERMINAL_STYLE.get(outcome.final_state, 'white')}]"
                f"{outcome.final_state.value.replace('_', ' ')}[/]",
                outcome.severity.value if outcome.severity else "-",
                f"{outcome.confidence:.0%}",
                "[green]PASS[/green]" if ok else f"[red]FAIL[/red] {why}",
            )
        console.print(table)
        console.print()
        style = "bold green" if passed == len(outcomes) else "bold red"
        console.print(f"  [{style}]{passed}/{len(outcomes)} scenarios matched ground truth[/{style}]")

    if getattr(args, "report", None):
        from .report import write_report  # noqa: PLC0415 -- optional path

        written = write_report(Path(args.report), runs)
        console.print(f"\n  [dim]trace report written to[/dim] {written}")

    return 0 if all(_grade(o, SCENARIOS_BY_KEY[k])[0] for k, o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
