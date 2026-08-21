Produce an investigation plan for the question below.

Return JSON: {"steps": [{"goal": str, "tools": [str], "why": str}], "hypotheses": [str]}

Between 2 and 5 steps. Order them so the cheapest, highest-signal checks come
first: deploy timeline before log search, metrics before traces. If a past
incident might match, check that before anything else — it is one embedding
call and it can end the investigation immediately.

Available tools:
$tools

Question: $query
