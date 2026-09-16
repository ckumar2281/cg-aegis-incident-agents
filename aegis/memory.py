"""
Incident memory: recurrence detection without a vector database.

The single highest-leverage thing an incident system can say is "this has happened
before, here is what fixed it". Aegis gets that from a small, explainable similarity
function over structured incident records rather than embeddings, for three reasons:
it needs no extra infrastructure or spend, the match is inspectable (you can see
*why* two incidents were judged similar), and at the volume a single data platform
produces -- hundreds of incidents a year, not millions -- lexical and structural
overlap is genuinely competitive with semantic search.

Recurrence also changes the *framing* of an incident, which is what makes it worth
surfacing to a business audience. One late vendor file is a nuisance. The same vendor
late three times in a month is an integration that needs redesigning, and that is a
backlog conversation, not a 3am one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

_STOPWORDS = frozenset(
    """a an the and or of to in on for with is are was were be been it its this that
    at by from as not no than then so if""".split()
)


def _tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z_]{3,}", (text or "").lower()) if w not in _STOPWORDS
    }


@dataclass
class PastIncident:
    incident_id: str
    occurred_at: datetime
    asset: str
    incident_type: str
    root_cause_tag: str
    symptom: str
    resolution: str
    final_state: str
    time_to_resolve_minutes: int
    recurrence_of: str | None = None

    def to_dict(self, score: float = 0.0) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "occurred_at": self.occurred_at.isoformat(),
            "days_ago": None,
            "asset": self.asset,
            "incident_type": self.incident_type,
            "root_cause_tag": self.root_cause_tag,
            "symptom": self.symptom,
            "resolution": self.resolution,
            "final_state": self.final_state,
            "time_to_resolve_minutes": self.time_to_resolve_minutes,
            "similarity": round(score, 3),
        }


@dataclass
class IncidentMemory:
    """An append-only store of resolved incidents with structural similarity search."""

    incidents: list[PastIncident] = field(default_factory=list)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.incidents:
            self.incidents = _seed_history(self.now)

    def remember(self, incident: PastIncident) -> None:
        self.incidents.append(incident)

    # -- similarity ---------------------------------------------------------- #

    def _score(
        self, candidate: PastIncident, *, asset: str, incident_type: str, symptom: str
    ) -> float:
        score = 0.0
        # Same asset is the strongest structural signal.
        if candidate.asset == asset:
            score += 0.45
        elif candidate.asset.split(".")[-1] == asset.split(".")[-1]:
            score += 0.20
        elif candidate.asset.split(".")[0] == asset.split(".")[0]:
            score += 0.08
        if candidate.incident_type == incident_type:
            score += 0.25
        # Lexical overlap on the symptom description.
        a, b = _tokens(candidate.symptom), _tokens(symptom)
        if a and b:
            score += 0.30 * (len(a & b) / len(a | b))
        # Recency decay: a match from 18 months ago is weaker evidence.
        age_days = max(0.0, (self.now - candidate.occurred_at).total_seconds() / 86400)
        score *= 1.0 if age_days <= 45 else max(0.55, 1.0 - (age_days - 45) / 365)
        return min(1.0, score)

    def search(
        self, *, asset: str, incident_type: str, symptom: str, limit: int = 3, floor: float = 0.45
    ) -> list[dict[str, Any]]:
        scored: list[tuple[float, PastIncident]] = []
        for candidate in self.incidents:
            score = self._score(
                candidate, asset=asset, incident_type=incident_type, symptom=symptom
            )
            if score >= floor:
                scored.append((score, candidate))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        out: list[dict[str, Any]] = []
        for score, candidate in scored[:limit]:
            row = candidate.to_dict(score)
            row["days_ago"] = int((self.now - candidate.occurred_at).total_seconds() // 86400)
            out.append(row)
        return out

    def recurrence(
        self, *, asset: str, incident_type: str, symptom: str, window_days: int = 30
    ) -> dict[str, Any]:
        """
        Is this a repeat rather than a one-off?

        Two or more structurally similar incidents on the same asset inside the window
        promotes the incident from "fix it" to "the integration is unreliable".
        """
        cutoff = self.now - timedelta(days=window_days)
        matches = [
            c
            for c in self.incidents
            if c.asset == asset
            and c.occurred_at >= cutoff
            and self._score(c, asset=asset, incident_type=incident_type, symptom=symptom) >= 0.55
        ]
        return {
            "is_recurrence": len(matches) >= 2,
            "occurrences_in_window": len(matches),
            "window_days": window_days,
            "prior_incident_ids": [m.incident_id for m in matches],
            "prior_resolutions": sorted({m.resolution for m in matches}),
            "framing": (
                f"{len(matches) + 1} occurrences in {window_days} days -- treat as a chronic "
                "integration reliability problem rather than a one-off failure."
                if len(matches) >= 2
                else "No established pattern; treat as a one-off."
            ),
        }


def _seed_history(now: datetime) -> list[PastIncident]:
    """
    A plausible prior history.

    The two AdBridge entries are what let the chronic-lateness scenario be recognised
    as the third occurrence rather than a fresh incident.
    """
    return [
        PastIncident(
            "INC-2026-0731",
            now - timedelta(days=21),
            "STG.AD_SPEND",
            "freshness_breach",
            "chronic_vendor_lateness",
            "AdBridge daily export missing at expected prefix; task failed with NO_FILE",
            "Waited for the vendor file and re-ran the extract manually 4 hours later.",
            "resolved",
            265,
        ),
        PastIncident(
            "INC-2026-0812",
            now - timedelta(days=12),
            "STG.AD_SPEND",
            "freshness_breach",
            "chronic_vendor_lateness",
            "AdBridge daily export not present at expected prefix, extract task failed",
            "Re-ran the extract once the vendor delivered; raised a vendor support case.",
            "resolved",
            310,
        ),
        PastIncident(
            "INC-2026-0705",
            now - timedelta(days=74),
            "RAW.STRIPE_CHARGES",
            "schema_drift",
            "upstream_schema_drift",
            "Stripe added a nullable column, contract validation rejected the file",
            "Bumped the schema contract to accept the new optional column and reprocessed.",
            "resolved",
            95,
        ),
        PastIncident(
            "INC-2026-0620",
            now - timedelta(days=89),
            "INT.CUSTOMER_360",
            "quality_degradation",
            "source_config_change",
            "CRM field cleared during a migration, null rate on a key column spiked",
            "Business deferred the fix; file quarantined and a ticket raised with the CRM team.",
            "rejected_quarantined",
            40,
        ),
        PastIncident(
            "INC-2026-0518",
            now - timedelta(days=122),
            "INT.ORDER_ENRICHED",
            "volume_anomaly",
            "bad_deploy_join_fanout",
            "Join key change duplicated order rows, downstream counts inflated",
            "Reverted the offending PR and restated the affected partitions.",
            "resolved",
            140,
        ),
    ]
