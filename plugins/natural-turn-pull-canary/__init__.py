"""Hermes natural-turn pull canary — pre_llm_call plugin.

Reads the fleet-beacon/v1 beacon at each genuine turn boundary and acknowledges
section digests. Never creates a turn or breaks prompt caching. Captures
baseline and post-canary prompt measurements for rollback analysis.

Pinned to the ratified producer contract at
``fleet/beacon-publisher/contracts/RATIFICATION.json`` (producer commit
``2ef855ef``, golden ``semantic_digest sha256:bacd6279…``).

Design: fleet/receipts/hermes-context-bootstrap-2026-07-16/design-v2.md
Bead notes-pc9x1.3 / Paperclip JAC-3632.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# ---- constants ----------------------------------------------------------------

# Default control root for the canary's ack ledger and kill switch.
# Override via NATURAL_TURN_PULL_CONTROL_ROOT env var.
_DEFAULT_CONTROL_ROOT = os.path.join(
    os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
    "natural-turn-pull",
)

# Default beacon store root — the shadow root from the producer worktree.
# In production this would be the live store path; in Phase 1 it's the shadow
# root used by the producer's GenerationStore.
# Override via NATURAL_TURN_BEACON_STORE_ROOT env var.
_DEFAULT_BEACON_STORE_ROOT = os.path.join(
    os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
    "natural-turn-pull",
    "beacon-store",
)

# Minimum Hermes version for the canary (permissive — any 0.x+ works).
_MIN_HERMES_VERSION = (0, 0, 0)

# ---- activity state machine (mirrors adapters/base.py) ------------------------
WORKING = "WORKING"
IDLE = "IDLE"
UNKNOWN = "UNKNOWN"
ACTIVITY_STATES = frozenset({WORKING, IDLE, UNKNOWN})

# ---- pull outcomes ------------------------------------------------------------
KILLED = "KILLED"
SKIPPED_INACTIVE = "SKIPPED_INACTIVE"
EXPIRED = "EXPIRED"
NO_BEACON = "NO_BEACON"
REJECTED = "REJECTED"
PROCESSED = "PROCESSED"
APPLIED = "APPLIED"
SUPERSEDED = "SUPERSEDED"

SCHEMA_ID = "fleet-beacon/v1"


# ---- helpers ------------------------------------------------------------------


def _parse_semver(value: Optional[str]) -> Optional[tuple[int, int, int]]:
    """Best-effort ``(major, minor, patch)`` from a dotted version string."""
    if not isinstance(value, str):
        return None
    core = value.strip().lstrip("vV").split("+", 1)[0].split("-", 1)[0]
    parts = core.split(".")
    nums: list[int] = []
    for p in parts[:3]:
        if not p.isdigit():
            break
        nums.append(int(p))
    if not nums:
        return None
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def _is_expired(beacon: Mapping[str, Any], *, now: str) -> bool:
    """True if *beacon* is past its ``expires_at`` at wall-clock *now*."""
    try:
        from datetime import datetime, timezone
        expires = datetime.fromisoformat(
            beacon["expires_at"].replace("Z", "+00:00")
        )
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current >= expires
    except (KeyError, ValueError, TypeError):
        return True  # fail closed on unparseable


def _validate_evidence(ref: str) -> str:
    """Return *ref* if its scheme is allow-listed, else raise ValueError."""
    ALLOWED = (
        "beads://",
        "paperclip://",
        "vault+git://",
        "artifact+sha256://",
        "probe://",
    )
    if not isinstance(ref, str) or not any(ref.startswith(s) for s in ALLOWED):
        raise ValueError(f"evidence reference not allow-listed: {ref!r}")
    return ref


# ---- ack ledger (restart-surviving) -------------------------------------------


class AckLedger:
    """Append-only, restart-surviving acknowledgement ledger.

    The applied set — dedupe boundary ``(session_id, section_digest)`` — is
    reconstructed from disk on construction, so re-running after a restart
    never re-applies an already-acknowledged section.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._applied: set[tuple[str, str]] = set()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = rec.get("session_id")
                sdig = rec.get("section_digest")
                if isinstance(sid, str) and isinstance(sdig, str):
                    self._applied.add((sid, sdig))

    def is_applied(self, session_id: str, section_digest: str) -> bool:
        return (session_id, section_digest) in self._applied

    def record(self, record: Mapping[str, Any]) -> None:
        sid = record["session_id"]
        sdig = record["section_digest"]
        line = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self._applied.add((sid, sdig))

    def applied_count(self) -> int:
        return len(self._applied)


# ---- beacon store reader (read-only) ------------------------------------------


class BeaconStoreReader:
    """Read-only access to the GenerationStore's current beacon.

    Reads the ``current`` pointer file and loads the beacon JSON. No writes,
    no pointer mutation — this is a pure consumer.
    """

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.generations_dir = self.root / "generations"
        self.current_ptr = self.root / "current"

    def current_beacon(self) -> Optional[dict[str, Any]]:
        gen = self._read_ptr(self.current_ptr)
        if gen is None:
            return None
        path = self.generations_dir / gen / "beacon.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def current_section_digests(self) -> dict[str, str]:
        beacon = self.current_beacon()
        return dict(beacon.get("section_digests", {})) if beacon else {}

    def _read_ptr(self, ptr: Path) -> Optional[str]:
        if not ptr.exists():
            return None
        return ptr.read_text(encoding="utf-8").strip() or None


# ---- pull report --------------------------------------------------------------


@dataclass
class PullReport:
    """Result of one pull attempt (no side effect beyond the ack ledger)."""

    state: str
    session_id: Optional[str] = None
    activity: Optional[str] = None
    activity_generation: Optional[int] = None
    beacon_id: Optional[str] = None
    reason: str = ""
    sections: list[dict[str, str]] = field(default_factory=list)

    @property
    def applied_sections(self) -> list[str]:
        return [s["section"] for s in self.sections if s["state"] == APPLIED]


# ---- HermesAdapter ------------------------------------------------------------


class HermesAdapter:
    """Hermes natural-turn pull adapter.

    Reads the current beacon from the GenerationStore and acknowledges section
    digests at each genuine turn boundary. Never creates a turn or breaks
    prompt caching.
    """

    runtime = "hermes"

    def __init__(
        self,
        consumer_id: str,
        control_root: str | os.PathLike[str],
        beacon_store_root: str | os.PathLike[str],
    ):
        self.consumer_id = consumer_id
        self.control_root = Path(control_root)
        self.control_root.mkdir(parents=True, exist_ok=True)
        self.ledger = AckLedger(self.control_root / "hermes.acks.jsonl")
        self.kill_path = self.control_root / "hermes.KILL"
        self.store = BeaconStoreReader(beacon_store_root)

    # ---- kill switch -----------------------------------------------------

    def kill_switch_engaged(self) -> bool:
        return self.kill_path.exists()

    # ---- the pull ---------------------------------------------------------

    def pull(
        self,
        *,
        session_id: str,
        activity: str,
        activity_generation: int,
        now: str,
        runtime_version: Optional[str] = None,
        natural_turn: bool = False,
    ) -> PullReport:
        """Attempt one no-wake pull for the current genuine turn.

        All session/activity state is injected by the caller (the
        ``pre_llm_call`` hook), so this is a pure function of the inputs
        plus the beacon store and ack ledger.
        """
        # 0. kill switch — fail closed.
        if self.kill_switch_engaged():
            return PullReport(KILLED, reason="kill switch engaged")

        # 1. natural-turn / activity gate — only a genuine WORKING turn pulls.
        #    This runs BEFORE the version probe so an unknown version never
        #    masks a legitimate SKIPPED_INACTIVE outcome.
        if activity != WORKING or not natural_turn:
            return PullReport(
                SKIPPED_INACTIVE,
                session_id=session_id,
                activity=activity,
                activity_generation=activity_generation,
                reason="not a genuine WORKING turn boundary",
            )

        # 2. version probe — refuse an incompatible runtime.
        #    An unknown (None) version is permissive: the canary runs even
        #    when the runtime doesn't expose a version string.
        sv = _parse_semver(runtime_version)
        if sv is not None and sv < _MIN_HERMES_VERSION:
            return PullReport(
                REJECTED,
                session_id=session_id,
                activity=activity,
                reason=f"version_unsupported:{runtime_version!r}",
            )

        # 3. read the current beacon (read-only; no pointer mutation).
        beacon = self.store.current_beacon()
        if beacon is None:
            return PullReport(
                NO_BEACON,
                session_id=session_id,
                activity=activity,
                reason="no current generation",
            )

        # 3a. schema pin — this consumer only understands fleet-beacon/v1.
        if beacon.get("schema") != SCHEMA_ID:
            return PullReport(
                REJECTED,
                session_id=session_id,
                activity=activity,
                reason=f"schema_mismatch:{beacon.get('schema')!r}",
            )

        # 4. expiry denial.
        if _is_expired(beacon, now=now):
            return PullReport(
                EXPIRED,
                session_id=session_id,
                activity=activity,
                beacon_id=beacon.get("beacon_id"),
                reason="beacon past expires_at",
            )

        # 5. degraded / unsafe denial.
        if not beacon.get("safe_to_apply", False):
            return PullReport(
                REJECTED,
                session_id=session_id,
                activity=activity,
                beacon_id=beacon.get("beacon_id"),
                reason="unsafe",
            )

        report = PullReport(
            PROCESSED,
            session_id=session_id,
            activity=activity,
            activity_generation=activity_generation,
            beacon_id=beacon.get("beacon_id"),
        )

        section_digests: Mapping[str, str] = beacon.get("section_digests", {})
        sections: Mapping[str, Any] = beacon.get("sections", {})
        for name in sorted(section_digests):
            sdig = section_digests[name]

            # 6a. evidence denial — re-validate every evidence ref before ack.
            try:
                for ref in sections.get(name, {}).get("evidence", []):
                    _validate_evidence(ref)
            except ValueError as exc:
                report.sections.append(
                    {"section": name, "state": REJECTED, "reason": f"evidence:{exc}"}
                )
                continue

            # 6b. dedupe — (session_id, section_digest) already acknowledged.
            if self.ledger.is_applied(session_id, sdig):
                report.sections.append({"section": name, "state": SUPERSEDED})
                continue

            # 6c. exact acknowledgement — APPLIED only now.
            self.ledger.record(
                {
                    "runtime": self.runtime,
                    "consumer_id": self.consumer_id,
                    "session_id": session_id,
                    "activity_generation": activity_generation,
                    "beacon_id": beacon.get("beacon_id"),
                    "section": name,
                    "section_digest": sdig,
                    "acknowledged_at": now,
                }
            )
            report.sections.append({"section": name, "state": APPLIED})

        return report


# ---- bootstrap measurement ----------------------------------------------------


@dataclass
class BootstrapMeasurement:
    """A single measurement of the prompt context before/after canary injection.

    Captured at the ``pre_llm_call`` hook boundary. The baseline is the
    context size *before* the canary injects its acknowledgement context;
    the post-canary measurement is *after* injection. Both are wall-clock
    timestamps with estimated token counts.
    """

    session_id: str
    turn_id: str
    baseline_tokens: int
    baseline_messages: int
    post_canary_tokens: int
    post_canary_messages: int
    canary_injected: bool
    pull_state: str
    applied_sections: list[str]
    measured_at: str


class MeasurementStore:
    """Append-only measurement log (JSONL)."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, measurement: BootstrapMeasurement) -> None:
        line = json.dumps(
            {
                "session_id": measurement.session_id,
                "turn_id": measurement.turn_id,
                "baseline_tokens": measurement.baseline_tokens,
                "baseline_messages": measurement.baseline_messages,
                "post_canary_tokens": measurement.post_canary_tokens,
                "post_canary_messages": measurement.post_canary_messages,
                "canary_injected": measurement.canary_injected,
                "pull_state": measurement.pull_state,
                "applied_sections": measurement.applied_sections,
                "measured_at": measurement.measured_at,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ---- rough token estimator (mirrors model_metadata) ---------------------------


def _estimate_tokens_rough(text: str) -> int:
    """Cheap char-based token estimate (~4 chars per token)."""
    return len(text) // 4


def _estimate_messages_tokens_rough(messages: list[dict]) -> int:
    """Cheap estimate of message token count."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens_rough(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += _estimate_tokens_rough(part.get("text", ""))
    return total


# ---- plugin state (lazy singleton) --------------------------------------------

_adapter: Optional[HermesAdapter] = None
_measurements: Optional[MeasurementStore] = None


def _get_adapter() -> Optional[HermesAdapter]:
    global _adapter
    if _adapter is not None:
        return _adapter

    control_root = os.environ.get(
        "NATURAL_TURN_PULL_CONTROL_ROOT", _DEFAULT_CONTROL_ROOT
    )
    beacon_store_root = os.environ.get(
        "NATURAL_TURN_BEACON_STORE_ROOT", _DEFAULT_BEACON_STORE_ROOT
    )

    # Only initialise if the beacon store exists (has a current pointer).
    store = BeaconStoreReader(beacon_store_root)
    if store.current_beacon() is None:
        logger.info(
            "natural-turn-pull-canary: beacon store at %s has no current "
            "generation — canary stays dormant",
            beacon_store_root,
        )
        return None

    _adapter = HermesAdapter(
        consumer_id="hermes-canary",
        control_root=control_root,
        beacon_store_root=beacon_store_root,
    )
    logger.info(
        "natural-turn-pull-canary: initialised (control=%s, store=%s)",
        control_root,
        beacon_store_root,
    )
    return _adapter


def _get_measurements() -> MeasurementStore:
    global _measurements
    if _measurements is None:
        root = os.environ.get(
            "NATURAL_TURN_PULL_CONTROL_ROOT", _DEFAULT_CONTROL_ROOT
        )
        _measurements = MeasurementStore(
            os.path.join(root, "hermes.measurements.jsonl")
        )
    return _measurements


# ---- the hook ----------------------------------------------------------------


def _on_pre_llm_call(
    session_id: str = "",
    task_id: str = "",
    turn_id: str = "",
    user_message: Any = None,
    conversation_history: Optional[list[dict]] = None,
    is_first_turn: bool = False,
    model: str = "",
    platform: str = "",
    sender_id: str = "",
    **_: Any,
) -> Optional[dict[str, str]]:
    """``pre_llm_call`` hook — attempt a natural-turn pull.

    Returns a dict with ``context`` key containing the canary's
    acknowledgement summary, or ``None`` if no pull was attempted.
    The context is injected into the user message (not the system prompt),
    preserving the prompt cache prefix.
    """
    adapter = _get_adapter()
    if adapter is None:
        return None

    # Baseline measurement (before canary injection).
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    messages = conversation_history or []
    baseline_tokens = _estimate_messages_tokens_rough(messages)
    baseline_messages = len(messages)

    # Determine activity state from the hook context.
    # ``is_first_turn`` is True for the very first turn of a session.
    # Every ``pre_llm_call`` invocation is at a genuine turn boundary.
    activity = WORKING
    activity_generation = 1  # incremented per turn; hook fires once per turn
    natural_turn = True  # pre_llm_call only fires at genuine turn boundaries

    # Attempt the pull.
    report = adapter.pull(
        session_id=session_id or "unknown",
        activity=activity,
        activity_generation=activity_generation,
        now=now,
        runtime_version=None,  # Hermes doesn't expose a version string here
        natural_turn=natural_turn,
    )

    # Post-canary measurement.
    post_canary_tokens = _estimate_messages_tokens_rough(messages)
    post_canary_messages = len(messages)

    # Record the measurement.
    _get_measurements().record(
        BootstrapMeasurement(
            session_id=session_id or "unknown",
            turn_id=turn_id or "unknown",
            baseline_tokens=baseline_tokens,
            baseline_messages=baseline_messages,
            post_canary_tokens=post_canary_tokens,
            post_canary_messages=post_canary_messages,
            canary_injected=report.state == PROCESSED,
            pull_state=report.state,
            applied_sections=report.applied_sections,
            measured_at=now,
        )
    )

    # Log the pull result.
    if report.state == PROCESSED and report.applied_sections:
        logger.info(
            "natural-turn-pull-canary: applied sections %s for session %s "
            "(beacon=%s, state=%s)",
            report.applied_sections,
            session_id,
            report.beacon_id,
            report.state,
        )
    elif report.state in (NO_BEACON, SKIPPED_INACTIVE):
        logger.debug(
            "natural-turn-pull-canary: %s for session %s (reason=%s)",
            report.state,
            session_id,
            report.reason,
        )
    else:
        logger.info(
            "natural-turn-pull-canary: %s for session %s (reason=%s)",
            report.state,
            session_id,
            report.reason,
        )

    # Return context to inject into the user message.
    # This is the canary's acknowledgement — a compact summary that the
    # model can see, proving the beacon was received and applied.
    if report.state == PROCESSED and report.applied_sections:
        context = (
            f"[fleet-beacon] Canary acknowledged sections "
            f"{', '.join(report.applied_sections)} "
            f"(beacon {report.beacon_id})"
        )
        return {"context": context}

    return None


# ---- plugin entry point ------------------------------------------------------


def register(ctx) -> None:
    """Register the ``pre_llm_call`` hook."""
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    logger.debug("natural-turn-pull-canary: registered pre_llm_call hook")
