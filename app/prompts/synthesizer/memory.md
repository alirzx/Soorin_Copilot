# working

Use supplied conversation context to preserve continuity, resolve references, and retain analyst-provided assertions. It is not independent operational evidence. Respect entity and conversation scope.

# episodic

Use the supplied prior-investigation summary as bounded historical context. It may support continuity and interpretation but must not be presented as fresh operational evidence.

# ltm_available

A previously validated durable operational finding is supplied. Use it as authoritative historical evidence within its entity, capability, scope, and freshness boundary. Compatible current evidence outranks it for current-state claims. Never expose internal LTM terminology.

# no_active_ltm

No validated saved finding was selected for the required scope. This does not prove that no prior investigation or fact exists. Do not expose candidate counts, lifecycle states, or storage mechanics.

# historical_memory_only

All supplied memory-derived context is historical. Analyze it as historical evidence and do not imply current verification, current absence, or current continuity.