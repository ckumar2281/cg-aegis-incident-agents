"""
HTML trace report.

Produces one self-contained file per run: no external assets, no CDN, opens offline.
Written for someone reviewing an incident after the fact who needs to answer "why did
it do that" without reading the code.

The centrepiece is the **side-by-side disclosure view**: the business brief and the
technical packet rendered next to each other, from the same verdict. Describing tiered
disclosure takes a paragraph and invites scepticism; showing the Product Owner's email
with no object names beside the developer's with the diff in it settles the question in
about four seconds.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import TraceBus
from .contracts import DisclosureTier, GateVerdict, IncidentOutcome

_CSS = """
:root {
  --bg: #fbfaf8; --panel: #ffffff; --ink: #1f1e1d; --muted: #6b6864;
  --line: rgba(31,30,29,.12); --accent: #b8562f; --ok: #2f7d4f; --warn: #a8791b;
  --bad: #b2372a; --biz: #2f6d7d; --tech: #5a4b8a;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #171613; --panel: #201e1b; --ink: #f0eee6; --muted: #a29d94;
    --line: rgba(240,238,230,.14); --accent: #e08a5f; --ok: #6cc08a; --warn: #d9ad55;
    --bad: #e07a6b; --biz: #6bb4c6; --tech: #a596d8;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.6 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1120px; margin: 0 auto; padding: 48px 16px 96px; }
h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -.02em; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .09em;
     color: var(--muted); margin: 44px 0 14px; font-weight: 600; }
h3 { font-size: 15px; margin: 0 0 10px; }
.sub { color: var(--muted); margin: 0 0 28px; }
.card { background: var(--panel); border: 1px solid var(--line);
        border-radius: 10px; padding: 18px 20px; margin-bottom: 14px; }
.grid { display: grid; gap: 14px; }
@media (min-width: 820px) { .two { grid-template-columns: 1fr 1fr; } }
.kv { display: grid; grid-template-columns: 170px 1fr; gap: 6px 18px; font-size: 14px; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; }
.pill { display: inline-block; padding: 2px 9px; border-radius: 999px;
        font-size: 11px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; }
.pill.ok { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }
.pill.bad { background: color-mix(in srgb, var(--bad) 16%, transparent); color: var(--bad); }
.pill.warn { background: color-mix(in srgb, var(--warn) 18%, transparent); color: var(--warn); }
.pill.biz { background: color-mix(in srgb, var(--biz) 16%, transparent); color: var(--biz); }
.pill.tech { background: color-mix(in srgb, var(--tech) 18%, transparent); color: var(--tech); }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th { text-align: left; font-weight: 600; color: var(--muted); font-size: 11px;
     text-transform: uppercase; letter-spacing: .07em; padding: 0 10px 8px 0;
     border-bottom: 1px solid var(--line); }
td { padding: 9px 10px 9px 0; border-bottom: 1px solid var(--line); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
code, .mono { font-family: var(--mono); font-size: 12.5px; }
pre { font-family: var(--mono); font-size: 12px; line-height: 1.5; overflow-x: auto;
      background: color-mix(in srgb, var(--ink) 5%, transparent);
      padding: 12px 14px; border-radius: 7px; margin: 10px 0 0; }
.timeline { position: relative; padding-left: 22px; }
.timeline::before { content: ""; position: absolute; left: 5px; top: 5px; bottom: 5px;
                    width: 1px; background: var(--line); }
.ev { position: relative; padding: 6px 0; font-size: 13.5px; }
.ev::before { content: ""; position: absolute; left: -21px; top: 13px; width: 7px;
              height: 7px; border-radius: 50%; background: var(--muted); }
.ev.hl::before { background: var(--accent); box-shadow: 0 0 0 3px
                 color-mix(in srgb, var(--accent) 22%, transparent); }
.ev .who { color: var(--muted); font-family: var(--mono); font-size: 11.5px; }
.tier-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.note { color: var(--muted); font-size: 13px; }
.bar { height: 5px; border-radius: 3px; background: color-mix(in srgb, var(--ink) 10%, transparent);
       overflow: hidden; margin-top: 5px; }
.bar > i { display: block; height: 100%; background: var(--accent); }
.redact { border-left: 2px solid var(--biz); padding-left: 14px; }
.reveal { border-left: 2px solid var(--tech); padding-left: 14px; }
footer { color: var(--muted); font-size: 12.5px; margin-top: 56px;
         border-top: 1px solid var(--line); padding-top: 18px; }
"""

_HIGHLIGHT = {
    "quarantine", "rca", "disclosure", "disclosure_released", "disclosure_blocked",
    "gate_closed", "autonomy", "autonomous", "rollback", "rejected", "deferred",
    "escalate", "execution_complete",
}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _pill(text: str, kind: str = "") -> str:
    return f'<span class="pill {kind}">{_esc(text)}</span>'


def _verdict_pill(verdict: GateVerdict | None) -> str:
    if verdict is None:
        return _pill("not opened", "warn")
    return _pill(
        verdict.value,
        {"approve": "ok", "reject": "bad", "defer": "warn", "timeout": "bad"}[verdict.value],
    )


# --------------------------------------------------------------------------- #


def _disclosure_section(bundle) -> str:
    """The side-by-side view. One verdict, two audiences."""
    if bundle is None:
        return (
            '<div class="card"><p class="note">No disclosure was produced — this '
            "incident resolved autonomously, so no brief was written and no approval "
            "was requested.</p></div>"
        )

    brief, tech = bundle.business, bundle.technical
    business_rows = "".join(
        f"<dt>{_esc(label)}</dt><dd>{_esc(value)}</dd>"
        for label, value in [
            ("what happened", brief.what_happened),
            ("who is affected", brief.who_is_affected),
            ("business impact", brief.business_impact),
            ("data at risk", brief.data_at_risk),
            ("proposed fix", brief.proposed_fix_in_plain_terms),
            ("time to fix", brief.time_to_fix),
            ("risk of fixing", brief.risk_of_fixing),
            ("risk of NOT fixing", brief.risk_of_not_fixing),
        ]
    )

    steps = "".join(
        f"<tr><td class='mono'>{_esc(s.step_id)}</td>"
        f"<td class='mono'>{_esc(s.action)}</td>"
        f"<td>{_pill(s.risk_tier.label, 'bad' if s.risk_tier.value >= 3 else 'warn' if s.risk_tier.value == 2 else 'ok')}</td>"
        f"<td>{_esc(s.intent)}</td></tr>"
        for s in tech.data_steps
    )
    changes = "".join(
        f"<h3 class='mono'>{_esc(c.path)}</h3>"
        f"<p class='note'>{_pill(c.magnitude.value, 'bad' if c.magnitude.rank >= 3 else 'warn' if c.magnitude.rank == 2 else 'ok')} "
        f"{_esc(c.rationale)}</p>"
        f"<pre>{_esc(c.diff)}</pre>"
        for c in tech.code_changes
    )

    released = (
        _pill("released after business approval", "ok")
        if bundle.technical_released
        else _pill("withheld — never sent", "warn")
    )
    redaction = (
        _pill("passed the firewall", "ok")
        if bundle.redaction_passed
        else _pill(f"BLOCKED · {len(bundle.redaction_findings)} findings", "bad")
    )

    return f"""
<p class="note">Both panels below are rendered from the <em>same</em> root-cause
verdict. The difference is not emphasis — it is enforced. A business artifact
containing SQL, a diff, a stack trace, an object name or anything resembling a raw
record is rejected by an automated policy before it can be sent.</p>
<div class="grid two">
  <div class="card redact">
    <div class="tier-head">{_pill("business tier", "biz")} {redaction}</div>
    <h3>{_esc(brief.headline)}</h3>
    <dl class="kv">{business_rows}</dl>
    <p class="note" style="margin-top:14px">Sent to the Product Owner. This is the
    entire contents of what the business decision was made on.</p>
  </div>
  <div class="card reveal">
    <div class="tier-head">{_pill("technical tier", "tech")} {released}</div>
    <h3>{_esc(tech.root_cause)}</h3>
    <dl class="kv">
      <dt>confidence</dt><dd>{tech.confidence:.0%}</dd>
      <dt>blast radius</dt><dd>{_esc(tech.blast_radius)}</dd>
      <dt>rollback</dt><dd>{_esc(tech.rollback_plan)}</dd>
      <dt>verification</dt><dd>{_esc(tech.verification_plan)}</dd>
    </dl>
    <h2 style="margin-top:22px">remediation steps</h2>
    <table><thead><tr><th></th><th>action</th><th>risk</th><th>intent</th></tr></thead>
    <tbody>{steps or '<tr><td colspan="4" class="note">none</td></tr>'}</tbody></table>
    <h2 style="margin-top:22px">code changes</h2>
    {changes or '<p class="note">none</p>'}
  </div>
</div>"""


def _incident_section(key: str, outcome: IncidentOutcome, rt, final) -> str:
    packet = final.get("packet")
    verdict = final.get("verdict")
    bundle = final.get("bundle")
    execution = final.get("execution")
    requirement = final.get("gate_requirement", {})

    state_kind = {
        "resolved": "ok",
        "rejected_quarantined": "bad",
        "deferred_backlog": "warn",
        "escalated": "bad",
    }.get(outcome.final_state.value, "")

    # -- hypothesis ranking ------------------------------------------------- #
    ranking = ""
    if verdict and verdict.ranked_hypotheses:
        rows = "".join(
            f"<tr><td class='mono'>{_esc(h.tag)}</td>"
            f"<td style='width:180px'>{h.posterior:.0%}"
            f"<div class='bar'><i style='width:{h.posterior * 100:.0f}%'></i></div></td>"
            f"<td>{len(h.supporting_evidence_ids)} for / {len(h.refuting_evidence_ids)} against</td>"
            f"<td>{_esc(h.statement)}</td></tr>"
            for h in verdict.ranked_hypotheses[:5]
        )
        contradictions = (
            "<p class='note'><strong>Contradictions surfaced:</strong> "
            + "; ".join(_esc(c) for c in verdict.contradictions)
            + "</p>"
            if verdict.contradictions
            else ""
        )
        ranking = f"""
<h2>hypotheses considered</h2>
<div class="card">
  <table><thead><tr><th>cause</th><th>posterior</th><th>evidence</th><th></th></tr></thead>
  <tbody>{rows}</tbody></table>
  <p class="note" style="margin-top:12px">{_esc(verdict.reasoning)}</p>
  {contradictions}
</div>"""

    # -- evidence ------------------------------------------------------------ #
    evidence_rows = "".join(
        f"<tr><td class='mono'>{_esc(e.evidence_id)}</td>"
        f"<td class='mono'>{_esc(e.author.value)}</td>"
        f"<td>{e.strength:.2f}</td>"
        f"<td>{_esc(e.claim)}"
        + (f"<br><span class='note'>{_esc(e.detail)}</span>" if e.detail else "")
        + (
            f"<br><span class='note mono'>via: "
            + ", ".join(_esc(tc.tool) for tc in e.provenance[:4])
            + "</span>"
            if e.provenance
            else ""
        )
        + "</td></tr>"
        for report in final.get("reports", [])
        for e in report.evidence
    )

    # -- timeline ------------------------------------------------------------ #
    events = "".join(
        f'<div class="ev {"hl" if ev.kind in _HIGHLIGHT else ""}">'
        f'<span class="who">{_esc(ev.kind)} · {_esc(ev.actor)}</span><br>{_esc(ev.message)}</div>'
        for ev in rt.trace.events
        if ev.kind not in ("reasoning",)
    )

    # -- gates ---------------------------------------------------------------- #
    gate_rows = ""
    for gate in (outcome.business_gate, outcome.technical_gate):
        if not gate:
            continue
        responses = "".join(
            f"<br><span class='note'>{_esc(r.role.value)}: <strong>{_esc(r.verdict.value)}</strong>"
            + (f" — {_esc(r.comment)}" if r.comment else "")
            + "</span>"
            for r in gate.responses
        )
        gate_rows += (
            f"<tr><td>{_esc(gate.gate.value.replace('_', ' '))}</td>"
            f"<td>{_verdict_pill(gate.verdict)}</td>"
            f"<td>{_esc(gate.rationale)}{responses}</td></tr>"
        )
    gates = (
        f"""<h2>approval chain</h2><div class="card"><table>
        <thead><tr><th>gate</th><th>verdict</th><th>detail</th></tr></thead>
        <tbody>{gate_rows}</tbody></table></div>"""
        if gate_rows
        else f"""<h2>approval chain</h2><div class="card">
        <p class="note">{_pill("autonomous", "ok")} No gate was opened.
        {_esc(requirement.get("rationale", ""))}</p></div>"""
    )

    # -- execution ------------------------------------------------------------ #
    execution_html = ""
    if execution:
        rows = "".join(
            f"<tr><td class='mono'>{_esc(r.step_id)}</td><td class='mono'>{_esc(r.action)}</td>"
            f"<td>{_pill(r.status, 'ok' if r.status == 'succeeded' else 'bad')}</td>"
            f"<td>{_esc(r.verification_detail or r.detail)}</td></tr>"
            for r in execution.step_results
        )
        pr = (
            f"<p class='note'>Pull request: <a href='{_esc(execution.pull_request.url)}'>"
            f"{_esc(execution.pull_request.url)}</a></p>"
            if execution.pull_request
            else ""
        )
        execution_html = f"""
<h2>execution &amp; verification</h2>
<div class="card">
  <table><thead><tr><th></th><th>action</th><th>status</th><th>independent check</th></tr></thead>
  <tbody>{rows}</tbody></table>
  {pr}
  <p class="note">Residual risk: {_esc(execution.residual_risk)}</p>
</div>"""

    chain = rt.chain.verify()
    ledger = rt.ledger.summary()
    alerts_in = (
        len(packet.correlated_alert_ids) + len(packet.suppressed_alert_ids) if packet else 0
    )

    return f"""
<section id="{_esc(key)}">
<h1>{_esc(packet.title if packet else key)}</h1>
<p class="sub">{_esc(key)} · {_pill(outcome.final_state.value.replace("_", " "), state_kind)}</p>

<div class="card">
  <dl class="kv">
    <dt>severity</dt><dd>{_esc(outcome.severity.value if outcome.severity else "—")}
      <span class="note">{_esc(packet.severity_rationale if packet else "")}</span></dd>
    <dt>root cause</dt><dd class="mono">{_esc(outcome.root_cause_tag)}
      <span class="note">({outcome.confidence:.0%} confidence, {final.get("round", 1)} round(s))</span></dd>
    <dt>alerts</dt><dd>{alerts_in} in → 1 incident
      <span class="note">({len(packet.suppressed_alert_ids) if packet else 0} folded as
      downstream cascade)</span></dd>
    <dt>blast radius</dt><dd>{_esc(packet.blast_radius.summary() if packet else "—")}</dd>
    <dt>gates required</dt><dd>{_esc(requirement.get("rationale", "—"))}</dd>
    <dt>audit chain</dt><dd>{_pill(f"{chain.events} events · {'valid' if chain.valid else 'BROKEN'}",
      "ok" if chain.valid else "bad")}</dd>
    <dt>cost</dt><dd>${ledger["usd"]:.4f} · {ledger["llm_calls"]} model calls
      {_pill("degraded", "warn") if ledger["degraded"] else ""}</dd>
  </dl>
</div>

{ranking}

<h2>tiered disclosure</h2>
{_disclosure_section(bundle)}

{gates}
{execution_html}

<h2>evidence</h2>
<div class="card"><table>
<thead><tr><th>id</th><th>author</th><th>strength</th><th>finding</th></tr></thead>
<tbody>{evidence_rows or '<tr><td colspan="4" class="note">none</td></tr>'}</tbody>
</table></div>

<h2>timeline</h2>
<div class="card"><div class="timeline">{events}</div></div>
</section>
"""


def write_report(path: Path, runs: list[tuple[str, IncidentOutcome, Any, dict]]) -> Path:
    """Render one HTML file covering every incident in `runs`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    nav = " · ".join(f'<a href="#{_esc(k)}">{_esc(k)}</a>' for k, _, _, _ in runs)
    sections = "\n".join(_incident_section(k, o, rt, f) for k, o, rt, f in runs)
    total_cost = sum(rt.ledger.usd for _, _, rt, _ in runs)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    document = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aegis incident trace</title>
<style>{_CSS}</style>
</head><body><div class="wrap">
<p class="sub"><strong>Aegis</strong> — governed agentic incident management ·
{len(runs)} incident(s) · {generated} · ${total_cost:.4f}</p>
<p class="sub">{nav}</p>
{sections}
<footer>
Generated by <code>aegis.report</code>. Every claim in this report traces to a recorded
tool call, and every state transition is a link in a SHA-256 hash chain — the audit
line on each incident reports whether that chain verifies.
</footer>
</div></body></html>"""

    path.write_text(document, encoding="utf-8")
    return path
