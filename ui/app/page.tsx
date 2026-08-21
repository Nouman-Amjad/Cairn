"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  type CairnEvent,
  ask,
  formatCost,
  stateTone,
  subscribe,
} from "@/lib/api";

interface StepLine {
  seq: number;
  tool: string;
  summary: string;
  sensitivity: string;
  ok: boolean;
}

export default function AskPage() {
  const [query, setQuery] = useState("");
  const [trajectoryId, setTrajectoryId] = useState<string | null>(null);
  const [state, setState] = useState<string>("");
  const [plan, setPlan] = useState<{ goal: string; why?: string }[]>([]);
  const [steps, setSteps] = useState<StepLine[]>([]);
  const [notices, setNotices] = useState<string[]>([]);
  const [answer, setAnswer] = useState<string>("");
  const [confidence, setConfidence] = useState<number | null>(null);
  const [cost, setCost] = useState<string>("0");
  const [error, setError] = useState<string>("");
  const [running, setRunning] = useState(false);
  const unsubscribe = useRef<(() => void) | null>(null);

  useEffect(() => () => unsubscribe.current?.(), []);

  const onEvent = useCallback((event: CairnEvent) => {
    const data = event.data as Record<string, never>;
    switch (event.type) {
      case "state":
        setState(String(data.state ?? ""));
        break;
      case "plan":
        setPlan((data.steps as unknown as { goal: string; why?: string }[]) ?? []);
        break;
      case "step":
        setSteps((prior) => [
          ...prior,
          {
            seq: Number(data.seq ?? prior.length + 1),
            tool: String(data.tool ?? "tool"),
            summary: String(data.summary ?? ""),
            sensitivity: String(data.sensitivity ?? "public"),
            ok: Boolean(data.ok ?? true),
          },
        ]);
        break;
      case "notice":
        setNotices((prior) => [...prior, String(data.message ?? "")]);
        break;
      case "approval":
        setNotices((prior) => [
          ...prior,
          "Waiting for a human to approve an action — see Approvals.",
        ]);
        break;
      case "answer":
        setAnswer(String(data.answer ?? ""));
        setConfidence(data.confidence != null ? Number(data.confidence) : null);
        setCost(String(data.cost_usd ?? "0"));
        setState(String(data.state ?? ""));
        break;
      case "error":
        setError(String(data.message ?? "something went wrong"));
        break;
      default:
        break;
    }
  }, []);

  async function submit(formEvent: React.FormEvent) {
    formEvent.preventDefault();
    if (!query.trim() || running) return;

    unsubscribe.current?.();
    setSteps([]);
    setPlan([]);
    setNotices([]);
    setAnswer("");
    setError("");
    setConfidence(null);
    setRunning(true);

    try {
      const { trajectory_id } = await ask(query.trim());
      setTrajectoryId(trajectory_id);
      setState("PLANNING");
      unsubscribe.current = subscribe(trajectory_id, onEvent, () =>
        setRunning(false),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setRunning(false);
    }
  }

  return (
    <>
      <form className="ask" onSubmit={submit}>
        <input
          type="text"
          value={query}
          placeholder="why did checkout latency spike at 3am?"
          onChange={(e) => setQuery(e.target.value)}
          disabled={running}
          autoFocus
        />
        <button type="submit" className="primary" disabled={running || !query.trim()}>
          {running ? "Investigating…" : "Ask"}
        </button>
      </form>

      {error && (
        <div className="card error">
          <strong>Failed.</strong> <span className="mono">{error}</span>
        </div>
      )}

      {state && (
        <div className="card">
          <span className={`badge ${stateTone(state)}`}>{state}</span>{" "}
          {trajectoryId && (
            <Link href={`/trajectories/${trajectoryId}`} className="mono">
              trajectory {trajectoryId.slice(0, 8)}
            </Link>
          )}{" "}
          <span className="mono muted">{formatCost(cost)}</span>
        </div>
      )}

      {plan.length > 0 && (
        <div className="card">
          <strong>Plan</strong>
          <ol>
            {plan.map((step, index) => (
              <li key={index}>
                {step.goal}
                {step.why && <span className="muted"> — {step.why}</span>}
              </li>
            ))}
          </ol>
        </div>
      )}

      {steps.length > 0 && (
        <div className="card">
          <strong>Investigation</strong>
          <div>
            {steps.map((step) => (
              <div className="step" key={`${step.seq}-${step.tool}`}>
                <div className="seq">{step.seq}</div>
                <div className="body">
                  <span className="mono">{step.tool}</span>{" "}
                  {step.sensitivity === "restricted" && (
                    <span className="badge warn">restricted · local only</span>
                  )}
                  {!step.ok && <span className="badge bad">failed</span>}
                  {step.summary && <div className="meta">{step.summary}</div>}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {notices.map((notice, index) => (
        <div className="card" key={index}>
          <span className="muted">{notice}</span>
        </div>
      ))}

      {answer && (
        <div className="card">
          <strong>Answer</strong>
          {confidence != null && (
            <span className="mono muted"> · confidence {confidence.toFixed(2)}</span>
          )}
          <div className="answer">{answer}</div>
        </div>
      )}
    </>
  );
}
