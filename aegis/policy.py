"""
Governance policy: the redaction firewall and the autonomy ladder.

Two independent guards sit between the agents and the outside world.

1. `RedactionPolicy` -- the disclosure firewall. The Product Owner must be able to
   make a business decision without being handed code, credentials, internal object
   names or raw records. This is enforced by machine check, not by
   asking a model nicely: a business artifact that trips any rule is rejected, the
   findings are handed back to the disclosure agent for one repair attempt, and if it
   fails again the incident escalates to a human rather than over-disclosing.

2. `AutonomyLadder` -- what the agents may do unsupervised. The default posture is
   closed: both gates, always. Two narrow exceptions earn autonomy -- a SEV4 whose
   remediation is entirely reversible with no code change (the "re-run the failed
   task" case that should not wake anyone at 3am), and a plan whose only code changes
   are cosmetic. Anything that adds, removes or alters behaviour goes to the gates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .contracts import (
    BusinessBrief,
    ChangeMagnitude,
    RemediationProposal,
    RiskTier,
    Severity,
)

# --------------------------------------------------------------------------- #
# Redaction firewall
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RedactionRule:
    name: str
    pattern: re.Pattern[str]
    explanation: str


def _rx(p: str) -> re.Pattern[str]:
    return re.compile(p, re.IGNORECASE | re.MULTILINE)


def _rx_cs(p: str) -> re.Pattern[str]:
    """Case-sensitive variant, for rules whose whole signal *is* the casing."""
    return re.compile(p, re.MULTILINE)


#: Rules applied to every BUSINESS-tier artifact. Deliberately strict: a false
#: positive costs one regeneration, a false negative leaks production internals to
#: an audience that did not ask for them and cannot act on them.
BUSINESS_TIER_RULES: tuple[RedactionRule, ...] = (
    RedactionRule(
        "sql",
        _rx(r"\b(select\s+.*\bfrom\b|insert\s+into|update\s+\w+\s+set|delete\s+from|"
            r"create\s+(or\s+replace\s+)?(table|view|task|stage)|alter\s+table|"
            r"merge\s+into|copy\s+into|coalesce\s*\(|cast\s*\(.*\bas\b)"),
        "SQL statements are technical-tier only",
    ),
    RedactionRule(
        "code_fence",
        _rx(r"```|~~~"),
        "Code blocks are technical-tier only",
    ),
    RedactionRule(
        "diff",
        # Unified-diff headers and hunk markers. Deliberately NOT matching bare
        # "+ " / "- " line prefixes: those are markdown bullets, and a rule that
        # rejects every bulleted list would be turned off within a week.
        #
        # The separator is [ \t], not \s. With \s the pattern also matched a bare
        # "---" line followed by any text -- a markdown horizontal rule. The approval
        # emails use those as section dividers, so the rule fired on six of the eight
        # scenarios' own business briefs the moment anything actually checked the
        # rendered body. A real diff header carries its path on the same line
        # ("--- a/etl/stripe_ingest.py"), which this still matches.
        _rx(r"^(---|\+\+\+)[ \t]+\S|^@@[\s\-+0-9,]*@@|^diff --git|^index [0-9a-f]{7,}"),
        "Patch/diff content is technical-tier only",
    ),
    RedactionRule(
        "qualified_object",
        # Two-part (SCHEMA.TABLE) as well as three-part (DB.SCHEMA.TABLE). The
        # original rule required three segments, which meant it never fired on this
        # warehouse at all -- every object here is SCHEMA.TABLE. A control that
        # cannot match the thing it guards against is decoration.
        #
        # Case-SENSITIVE, unlike every other rule here. The pattern is written in
        # upper case because Snowflake object names are upper case, but the shared
        # IGNORECASE flag quietly turned it into "any two dotted words", which matches
        # `approvals.example.com` -- the approval link in the Product Owner's own
        # email. A rule that blocks the button the recipient is meant to press is a
        # rule someone switches off. It stayed invisible only because nothing checked
        # a rendered email body until now.
        _rx_cs(r"\b[A-Z][A-Z0-9_]{2,}\.[A-Z][A-Z0-9_]{2,}(\.[A-Z][A-Z0-9_]{2,})?\b"),
        "Database object names are technical-tier only",
    ),
    RedactionRule(
        "uri",
        _rx(r"\b(s3://|gs://|azure://|file://|https?://[^\s]*\.(internal|local)\b)"),
        "Internal storage URIs are technical-tier only",
    ),
    RedactionRule(
        "filesystem_path",
        _rx(r"(^|\s)(/(usr|var|opt|home|etc|tmp|mnt)/|~/|[A-Z]:\\)"),
        "Filesystem paths are technical-tier only",
    ),
    RedactionRule(
        "source_file",
        _rx(r"\b[\w/\-]+\.(py|sql|yml|yaml|java|scala|json|tf|sh|jar|conf)\b"),
        "Source file names are technical-tier only",
    ),
    RedactionRule(
        "stack_trace",
        _rx(r"(traceback \(most recent call last\)|^\s*at\s+[\w.$]+\(|"
            r"\b\w+(Error|Exception)\b\s*:|line\s+\d+,\s+in\s+)"),
        "Stack traces are technical-tier only",
    ),
    RedactionRule(
        "credential",
        _rx(r"(AKIA[0-9A-Z]{16}|aws_secret|private[_\s-]?key|-----BEGIN|"
            r"\b(password|passwd|secret|api[_\s-]?key|access[_\s-]?token)\b\s*[:=]|"
            r"\bBearer\s+[A-Za-z0-9._\-]{12,})"),
        "Credentials must never appear in any artifact",
    ),
    RedactionRule(
        "pii_email",
        _rx(r"\b[\w.+\-]+@[\w\-]+\.[\w.\-]+\b"),
        "Raw email addresses look like leaked record content",
    ),
    RedactionRule(
        "pii_number",
        _rx(r"\b(\d{3}-\d{2}-\d{4}|\d{13,19})\b"),
        "Long digit sequences look like leaked identifiers or card numbers",
    ),
    RedactionRule(
        "raw_record",
        _rx(r'(\{\s*"[\w]+"\s*:|\[\s*\{\s*")'),
        "Raw JSON records are technical-tier only",
    ),
    RedactionRule(
        "query_id",
        _rx(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
        "Internal query/run identifiers are technical-tier only",
    ),
)

#: Applied to every artifact regardless of tier. Credentials are never acceptable.
UNIVERSAL_RULES: tuple[str, ...] = ("credential",)


@dataclass
class RedactionFinding:
    rule: str
    field: str
    explanation: str
    excerpt: str

    def render(self) -> str:
        return f"[{self.rule}] {self.field}: {self.explanation} (saw: {self.excerpt!r})"


class RedactionPolicy:
    """Validates that a business-tier artifact is safe to send to a business audience."""

    def __init__(self, rules: tuple[RedactionRule, ...] = BUSINESS_TIER_RULES) -> None:
        self.rules = rules

    def check_text(self, text: str, field_name: str) -> list[RedactionFinding]:
        findings: list[RedactionFinding] = []
        if not text:
            return findings
        for rule in self.rules:
            match = rule.pattern.search(text)
            if match:
                excerpt = match.group(0)
                if len(excerpt) > 60:
                    excerpt = excerpt[:57] + "..."
                findings.append(
                    RedactionFinding(
                        rule=rule.name,
                        field=field_name,
                        explanation=rule.explanation,
                        excerpt=excerpt.strip(),
                    )
                )
        return findings

    def check_business_brief(self, brief: BusinessBrief) -> list[RedactionFinding]:
        findings: list[RedactionFinding] = []
        for field_name, value in brief.model_dump().items():
            if field_name == "incident_id" or not isinstance(value, str):
                continue
            findings.extend(self.check_text(value, field_name))
        return findings

    def check_universal(self, text: str, field_name: str) -> list[RedactionFinding]:
        """Credential scan that applies even to the technical tier."""
        findings: list[RedactionFinding] = []
        for rule in self.rules:
            if rule.name not in UNIVERSAL_RULES:
                continue
            match = rule.pattern.search(text or "")
            if match:
                findings.append(
                    RedactionFinding(
                        rule=rule.name,
                        field=field_name,
                        explanation=rule.explanation,
                        excerpt="<withheld>",
                    )
                )
        return findings


# --------------------------------------------------------------------------- #
# Autonomy ladder
# --------------------------------------------------------------------------- #


@dataclass
class GateRequirement:
    business_gate: bool
    technical_gate: bool
    rationale: str


@dataclass
class AutonomyLadder:
    """
    Decides which human gates a proposal must clear.

    The default posture is closed: both gates, always. Autonomy is granted only in
    the narrow case where the blast radius is trivial, the action is reversible, and
    no code is being changed.
    """

    allow_sev4_autonomy: bool = True
    autonomous_max_tier: RiskTier = RiskTier.T1_REVERSIBLE
    #: Actions that may never run unattended regardless of severity.
    always_gated_actions: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "release_quarantine",
                "backfill_table",
                "restate_table",
                "rollback_deployment",
                "drop_table",
                "force_merge_pr",
                "alter_schema",
            }
        )
    )

    #: Code changes at or above this magnitude need a business decision. Cosmetic
    #: changes -- formatting, comments, naming -- do not: nobody's judgement is
    #: improved by being asked to approve whitespace, and asking anyway is how an
    #: approval process trains people to click through without reading.
    gated_change_magnitude: ChangeMagnitude = ChangeMagnitude.ADDITIVE

    def evaluate(
        self, severity: Severity, proposal: RemediationProposal
    ) -> GateRequirement:
        gated_actions = [
            s.action for s in proposal.data_steps if s.action in self.always_gated_actions
        ]
        if gated_actions:
            return GateRequirement(
                True,
                True,
                f"Plan contains always-gated action(s): {', '.join(sorted(set(gated_actions)))}.",
            )

        significant = [
            c
            for c in proposal.code_changes
            if c.magnitude.rank >= self.gated_change_magnitude.rank
        ]
        if significant:
            kinds = ", ".join(sorted({c.magnitude.value for c in significant}))
            return GateRequirement(
                True,
                True,
                f"Plan makes {len(significant)} {kinds} code change(s), which alter behaviour "
                "and require both gates.",
            )
        if proposal.code_changes:
            # Reached only when every change is cosmetic.
            return GateRequirement(
                False,
                False,
                f"Plan changes {len(proposal.code_changes)} file(s), all cosmetic "
                "(formatting or comments, no behaviour change). Proceeding without "
                "approval and opening a pull request for normal review.",
            )
        if proposal.requires_quarantine_release:
            return GateRequirement(
                True, True, "Releasing data from quarantine always requires both gates."
            )
        if (
            self.allow_sev4_autonomy
            and severity == Severity.SEV4
            and proposal.max_risk_tier.value <= self.autonomous_max_tier.value
        ):
            return GateRequirement(
                False,
                False,
                (
                    "SEV4 with a fully reversible, code-free remediation "
                    f"(max tier: {proposal.max_risk_tier.label}). Executing autonomously "
                    "and notifying the pipeline owner after the fact."
                ),
            )
        return GateRequirement(
            True,
            True,
            (
                f"{severity.value} incident with max risk tier "
                f"'{proposal.max_risk_tier.label}'. Both gates required."
            ),
        )


# --------------------------------------------------------------------------- #
# Severity scoring
# --------------------------------------------------------------------------- #


def score_severity(
    *,
    tier1_downstream: float,
    total_downstream: int,
    consumer_surfaces: float,
    sla_breaches: int,
    rows_affected: int,
    financial_proximity: float = 0.0,
    primary_tier: int = 3,
    data_is_wrong: bool = False,
) -> tuple[Severity, float, str]:
    """
    Deterministic severity scoring.

    Kept out of the model deliberately: severity drives paging and approval SLAs, and
    a reviewer should be able to reproduce why an incident was a SEV1 without
    re-running an LLM. The model explains the score; it does not set it.

    `data_is_wrong` separates the two failure modes that monitoring tends to conflate.
    A stalled pipeline leaves yesterday's numbers on the dashboard and everyone can
    see the timestamp. Silent corruption puts *plausible but false* numbers in front
    of people who will act on them, and nothing on the screen says so. The second is
    worse, and the score says so.
    """
    score = 0.0
    reasons: list[str] = []

    if tier1_downstream:
        # `tier1_downstream` is distance-weighted by the caller, so a tier-1 asset one
        # hop away counts roughly three times one that is four hops away. Six remote
        # dashboards no longer add up to an emergency.
        score += min(0.35, 0.12 * tier1_downstream)
        reasons.append(f"{tier1_downstream:.1f} distance-weighted tier-1 asset(s) downstream")
    if total_downstream:
        score += min(0.15, 0.02 * total_downstream)
        reasons.append(f"{total_downstream} downstream asset(s)")
    if consumer_surfaces:
        score += min(0.20, 0.05 * consumer_surfaces)
        reasons.append(f"{consumer_surfaces:.1f} distance-weighted consumer surface(s) affected")
    if sla_breaches:
        score += min(0.15, 0.05 * sla_breaches)
        reasons.append(f"{sla_breaches} SLA breach(es)")
    if rows_affected > 1_000_000:
        score += 0.10
        reasons.append(f"{rows_affected:,} rows affected")
    elif rows_affected > 100_000:
        score += 0.07
        reasons.append(f"{rows_affected:,} rows affected")
    elif rows_affected > 10_000:
        score += 0.05
        reasons.append(f"{rows_affected:,} rows affected")
    if financial_proximity > 0:
        # Weighted by how close the nearest financial asset is. Revenue reporting
        # directly downstream is a different problem from a segment field that
        # eventually reaches a KPI tile four joins later.
        score += 0.15 * financial_proximity
        reasons.append(
            f"financial reporting in the blast radius (proximity {financial_proximity:.2f})"
        )
    if primary_tier == 1:
        score += 0.15
        reasons.append("the failing asset is itself tier-1")
    if data_is_wrong:
        score += 0.15
        reasons.append("data is incorrect rather than merely late, so consumers cannot tell")

    score = min(1.0, score)

    if score >= 0.70:
        sev = Severity.SEV1
    elif score >= 0.45:
        sev = Severity.SEV2
    elif score >= 0.20:
        sev = Severity.SEV3
    else:
        sev = Severity.SEV4

    rationale = (
        f"Blast-radius score {score:.2f} -> {sev.value}. "
        + ("Drivers: " + "; ".join(reasons) + "." if reasons else "No amplifying factors.")
    )
    return sev, score, rationale


def distance_weight(depth: int, decay: float = 0.45) -> float:
    """
    How much an asset `depth` hops downstream should count.

    depth 1 -> 1.00, 2 -> 0.69, 3 -> 0.53, 4 -> 0.43, 5 -> 0.36.

    Chosen so that direct consumers dominate the score while distant ones still
    register. A pure 1/depth decay punished depth 2 too hard for a warehouse where
    raw -> staging -> mart is the normal shape and nothing interesting is one hop away.
    """
    return 1.0 / (1.0 + decay * max(0, depth - 1))
