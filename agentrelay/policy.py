"""Route policy evaluation for AgentRelay."""
from __future__ import annotations

from .models import Identity, IdentityKind, PolicyDecision, PolicyEffect, PolicyRule, TrustLevel
from .storage import Storage


class PolicyEngine:
    def __init__(self, storage: Storage):
        self.storage = storage

    async def evaluate(self, source: str, target: str, room: str) -> PolicyDecision:
        source_identity = await self.storage.get_identity(source)
        target_identity = await self.storage.get_identity(target)
        identity_block = self._identity_block(source, source_identity) or self._identity_block(
            target, target_identity
        )
        if identity_block:
            return identity_block

        rules = await self.storage.list_policies()
        matching_rules = [
            rule
            for rule in rules
            if self._matches(rule.source, source)
            and self._matches(rule.target, target)
            and self._matches(rule.room, room)
        ]

        deny = next((rule for rule in matching_rules if rule.effect == PolicyEffect.DENY), None)
        if deny:
            return PolicyDecision(allowed=False, reason="POLICY_DENY", rule_id=deny.id)

        allow = next((rule for rule in matching_rules if rule.effect == PolicyEffect.ALLOW), None)
        if allow:
            return PolicyDecision(allowed=True, reason="POLICY_ALLOW", rule_id=allow.id)

        if self._allow_required(source, target, room, source_identity, target_identity, rules):
            return PolicyDecision(allowed=False, reason="ALLOW_REQUIRED")

        return PolicyDecision(allowed=True, reason="DEFAULT_ALLOW")

    @staticmethod
    def _matches(pattern: str, value: str) -> bool:
        return pattern == "*" or pattern == value

    @staticmethod
    def _identity_block(identity_id: str, identity: Identity | None) -> PolicyDecision | None:
        if identity is None:
            if identity_id.startswith("external.") or identity_id.startswith("telegram."):
                return PolicyDecision(allowed=False, reason="UNKNOWN_EXTERNAL_IDENTITY")
            return None
        if not identity.enabled:
            return PolicyDecision(allowed=False, reason="IDENTITY_DISABLED")
        if identity.trust_level == TrustLevel.BLOCKED:
            return PolicyDecision(allowed=False, reason="IDENTITY_BLOCKED")
        return None

    def _allow_required(
        self,
        source: str,
        target: str,
        room: str,
        source_identity: Identity | None,
        target_identity: Identity | None,
        rules: list[PolicyRule],
    ) -> bool:
        if self._is_external(source, source_identity) or self._is_external(target, target_identity):
            return True
        return any(
            rule.effect == PolicyEffect.ALLOW
            and self._matches(rule.source, source)
            and self._matches(rule.target, target)
            and self._matches(rule.room, room)
            for rule in rules
        )

    @staticmethod
    def _is_external(identity_id: str, identity: Identity | None) -> bool:
        return identity_id.startswith(("external.", "telegram.")) or (
            identity is not None and identity.kind == IdentityKind.EXTERNAL
        )
