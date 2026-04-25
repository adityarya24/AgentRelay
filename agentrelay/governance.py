"""Governance engine for caps, cooldowns, rate limits, and thread state."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

from .models import GovernanceConfig

logger = logging.getLogger("agentrelay.governance")


class GovernanceEngine:
    def __init__(self, config: GovernanceConfig | None = None):
        self.config = config or GovernanceConfig()
        self._agent_thread_counts: dict[str, dict[str, int]] = defaultdict(dict)
        self._thread_counts: dict[str, int] = defaultdict(int)
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._rates: dict[str, list[float]] = defaultdict(list)
        self._thread_last_activity: dict[str, float] = {}
        self._metrics: dict[str, int] = {
            "cooldown_hits": 0,
            "cap_hits": 0,
            "rate_limit_hits": 0,
            "thread_autoclose": 0,
        }

    @staticmethod
    def _thread_key(room: str, thread: str | None) -> str | None:
        return f"{room}:{thread}" if thread else None

    def update_config(self, config: GovernanceConfig) -> None:
        self.config = config

    def check(self, agent: str, room: str, thread: str | None) -> tuple[bool, str | None, int | None]:
        now = time.time()
        recent = [t for t in self._rates[agent] if t > now - 60]
        self._rates[agent] = recent
        if len(recent) >= self.config.rate_limit:
            self._metrics["rate_limit_hits"] += 1
            return False, "RATE_LIMIT_EXCEEDED", 60

        last = self._cooldowns.get((room, agent), 0)
        remaining = self.config.cooldown_seconds - (now - last)
        if remaining > 0:
            self._metrics["cooldown_hits"] += 1
            return False, "COOLDOWN_ACTIVE", int(remaining) + 1

        key = self._thread_key(room, thread)
        if key:
            agent_count = self._agent_thread_counts.get(key, {}).get(agent, 0)
            if agent_count >= self.config.per_agent_thread_cap:
                self._metrics["cap_hits"] += 1
                return False, "AGENT_THREAD_CAP_REACHED", None
            if self._thread_counts.get(key, 0) >= self.config.per_thread_cap:
                self._metrics["cap_hits"] += 1
                return False, "THREAD_CAP_REACHED", None

        return True, None, None

    def record(self, agent: str, room: str, thread: str | None) -> None:
        now = time.time()
        self._rates[agent].append(now)
        self._cooldowns[(room, agent)] = now
        key = self._thread_key(room, thread)
        if key:
            self._agent_thread_counts.setdefault(key, {})
            self._agent_thread_counts[key][agent] = self._agent_thread_counts[key].get(agent, 0) + 1
            self._thread_counts[key] = self._thread_counts.get(key, 0) + 1
            self._thread_last_activity[key] = now

    def should_auto_close(self, room: str, thread: str) -> bool:
        key = self._thread_key(room, thread)
        return bool(key and self._thread_counts.get(key, 0) >= self.config.per_thread_cap)

    def should_timeout(self, room: str, thread: str) -> bool:
        key = self._thread_key(room, thread)
        if not key or key not in self._thread_last_activity:
            return False
        if time.time() - self._thread_last_activity[key] > self.config.inactivity_timeout:
            self._metrics["thread_autoclose"] += 1
            return True
        return False

    def thread_transition(self, current: str, event: str) -> str:
        transitions = {
            ("open", "first_response"): "active",
            ("active", "warn"): "closing",
            ("active", "timeout"): "closing",
            ("active", "moderator_close"): "closing",
            ("closing", "final"): "closed",
            ("closing", "timeout"): "closed",
            ("closing", "moderator_close"): "closed",
            ("active", "cap_reached"): "closing",
            ("open", "cap_reached"): "closing",
        }
        return transitions.get((current, event), current)

    def cleanup_stale(self, active_agents: set[str]) -> int:
        stale = [agent for agent in self._rates if agent not in active_agents]
        for agent in stale:
            self._rates.pop(agent, None)
        stale_cooldowns = [key for key in self._cooldowns if key[1] not in active_agents]
        for key in stale_cooldowns:
            self._cooldowns.pop(key, None)
        return len(stale)

    def cleanup_thread(self, room: str, thread: str) -> None:
        """Remove governance counters for a completed thread."""
        key = self._thread_key(room, thread)
        if not key:
            return
        removed = 0
        for mapping in (self._agent_thread_counts, self._thread_counts, self._thread_last_activity):
            if key in mapping:
                mapping.pop(key, None)
                removed += 1
        if removed:
            logger.debug("cleaned governance thread state", extra={"room": room, "thread": thread})

    def cleanup_threads(self, threads: list[tuple[str, str]]) -> int:
        """Remove governance counters for multiple completed threads."""
        for room, thread in threads:
            self.cleanup_thread(room, thread)
        return len(threads)

    def cleanup_room(self, room: str) -> None:
        for mapping in (self._agent_thread_counts, self._thread_counts, self._thread_last_activity):
            for key in [k for k in mapping if k.startswith(f"{room}:")]:
                mapping.pop(key, None)
        for key in [k for k in self._cooldowns if k[0] == room]:
            self._cooldowns.pop(key, None)

    def export_state(self) -> dict[str, Any]:
        return {
            "agent_thread_counts": {k: dict(v) for k, v in self._agent_thread_counts.items()},
            "thread_counts": dict(self._thread_counts),
            "cooldowns": {f"{room}:{agent}": ts for (room, agent), ts in self._cooldowns.items()},
            "global_rates": {agent: list(values) for agent, values in self._rates.items()},
            "thread_last_activity": dict(self._thread_last_activity),
            "metrics": dict(self._metrics),
        }

    def import_state(self, state: dict[str, Any]) -> None:
        self._agent_thread_counts = defaultdict(
            dict, {k: dict(v) for k, v in state.get("agent_thread_counts", {}).items()}
        )
        self._thread_counts = defaultdict(
            int, {k: int(v) for k, v in state.get("thread_counts", {}).items()}
        )
        self._cooldowns = {}
        for key, value in state.get("cooldowns", {}).items():
            room, sep, agent = key.partition(":")
            if sep:
                self._cooldowns[(room, agent)] = float(value)
        self._rates = defaultdict(list)
        for key, values in state.get("global_rates", {}).items():
            self._rates[key] = [float(v) for v in values]
        self._thread_last_activity = {
            str(k): float(v) for k, v in state.get("thread_last_activity", {}).items()
        }
        for key, value in state.get("metrics", {}).items():
            if key in self._metrics:
                self._metrics[key] = int(value)

    def metrics(self) -> dict[str, int]:
        return dict(self._metrics)
