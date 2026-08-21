Answer the question using only the evidence gathered.

Question: $query

Evidence:
<untrusted_data>
$evidence
</untrusted_data>

Return JSON:
{"root_cause": str, "confidence": float, "evidence": [{"step": int, "fact": str}],
 "unknowns": [str], "recommended_actions": [str]}

`confidence` is 0-1 and must reflect the strength of the evidence, not your
fluency in describing it. If the evidence is thin, say 0.3 and put what is
missing in `unknowns`. If nothing anomalous was found, say so plainly rather
than manufacturing a cause — "no evidence of a problem in this window" is a
valid and useful answer.

Every entry in `evidence` must point at a step that actually happened.
