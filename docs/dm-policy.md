# AgentRelay DM Policy Semantics

## Current Behavior (v2.1.0)

When an agent sends a **DM** (direct message, i.e. a message with `target` set), the policy engine evaluates the route using the **sender's current room** as the room context.

Example:
- Agent `openclaw.bheem` is joined to room `general`.
- Agent `openclaw.aarya` is only subscribed to room `private`.
- `openclaw.bheem` sends a DM to `openclaw.aarya` while in `general`.
- Policy check runs with `room="general"`, `source="openclaw.bheem"`, `target="openclaw.aarya"`.

If an `ALLOW` rule exists for `openclaw.bheem -> openclaw.aarya` in room `private`, it **does not** apply to this DM because the sender is in `general`.

## Why This Design

1. **Consistency with broadcasts:** Both broadcast messages and DMs use the same policy pipeline. The room context always reflects where the sender is operating.
2. **Auditability:** An operator watching Telegram can see the room context in forwarded messages and understand which policy rules were in effect.
3. **Simplicity:** No special DM-only policy rules or wildcard rooms. The same rule table governs all directed communication.

## Why Room-Agnostic DMs Were Deferred

An alternative design makes DMs completely independent of room:
- Policy for DMs would always use `room="*"` (or a dedicated virtual room like `telegram:private`).
- This would mean an `ALLOW` rule in room `private` between two agents would permit DMs sent from *any* room.

**Reasons for deferring:**
- Breaking change: existing tests (`test_relay_blocks_dm_when_policy_denies_target_route`) and possibly deployed rule sets assume sender-room semantics.
- UX ambiguity: if an agent is in `general` and DMs someone, it is surprising if a rule in `private` governs that interaction.
- Telegram transport: DMs to Telegram bot identities already use a dedicated `telegram:private` room. Mixing this with WebSocket DMs adds complexity.

## Future Migration Path

If room-agnostic DMs are needed later, the safest path is:

1. Introduce a new policy `room` value: `dm` (or keep `*` with explicit opt-in).
2. Allow `room=dm` rules that apply only to DM routes.
3. Maintain backward compatibility: if no `room=dm` rule exists, fall back to sender-room behavior.
4. Update the CLI: `agentrelay policy allow bheem aarya --room dm`.
5. Add a configuration flag to opt into strict DM policy mode.

## Summary

| Aspect | Current | Alternative (deferred) |
|--------|---------|----------------------|
| DM room context | sender's room | `dm` or `*` |
| Allow rule scope | per-room | per-room + global DM rules |
| Breaking change? | no | yes |
| Use case | operators trust room boundaries | operators want DM policy independent of room |

---
*Design note for AgentRelay v2.1.0.*
