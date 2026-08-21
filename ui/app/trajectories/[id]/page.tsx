"use client";

import { use, useEffect, useState } from "react";
import {
  type Trajectory,
  formatCost,
  getTrajectory,
  stateTone,
} from "@/lib/api";

/**
 * The trajectory viewer.
 *
 * This is the page an engineering manager reads after the fact, so it shows
 * everything: which model ran each step, why the router chose it, what it
 * cost, and what each tool returned. An agent you cannot audit is an agent
 * nobody will let near production twice.
 */
export default function TrajectoryPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [trajectory, setTrajectory] = useState<Trajectory | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;

    const load = () =>
      getTrajectory(id)
        .then((next) => !cancelled && setTrajectory(next))
        .catch((caught: unknown) =>
          setError(caught instanceof Error ? caught.message : String(caught)),
        );

    load();
    // Poll only while the investigation is live; a finished trajectory is
    // immutable and does not need refreshing.
    const timer = setInterval(() => {
      if (trajectory && trajectory.ended_at) return;
      load();
    }, 3000);

    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [id, trajectory]);

  if (error) {
    return (
      <div className="card error">
        <strong>Could not load.</strong> <span className="mono">{error}</span>
      </div>
    );
  }
  if (!trajectory) return <div className="card muted">Loading…</div>;

  const local = trajectory.tokens.local;
  const cloud = trajectory.tokens.cloud;
  const localShare = local + cloud > 0 ? local / (local + cloud) : 0;

  return (
    <>
      <div className="card">
        <span className={`badge ${stateTone(trajectory.state)}`}>
          {trajectory.state}
        </span>
        <h2 style={{ marginBottom: 6 }}>{trajectory.query}</h2>
        <table className="kv">
          <tbody>
            <tr>
              <td>cost</td>
              <td>{formatCost(trajectory.cost_usd)}</td>
            </tr>
            <tr>
              <td>tokens</td>
              <td>
                {local.toLocaleString()} local / {cloud.toLocaleString()} cloud (
                {(localShare * 100).toFixed(0)}% local)
              </td>
            </tr>
            <tr>
              <td>sensitivity</td>
              <td>{trajectory.sensitivity}</td>
            </tr>
            <tr>
              <td>prompts</td>
              <td>{trajectory.prompt_version}</td>
            </tr>
            <tr>
              <td>started</td>
              <td>{new Date(trajectory.started_at).toLocaleString()}</td>
            </tr>
            {trajectory.confidence != null && (
              <tr>
                <td>confidence</td>
                <td>{trajectory.confidence.toFixed(2)}</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {trajectory.answer && (
        <div className="card">
          <strong>Answer</strong>
          <div className="answer">{trajectory.answer}</div>
        </div>
      )}

      <div className="card">
        <strong>Steps</strong>
        {trajectory.steps.map((step) => (
          <div className="step" key={step.seq}>
            <div className="seq">{step.seq}</div>
            <div className="body">
              <span className="mono">
                {step.kind}
                {step.tool ? ` · ${step.tool}` : ""}
              </span>
              {step.error && <span className="badge bad">error</span>}
              {step.sensitivity === "restricted" && (
                <span className="badge warn">restricted</span>
              )}
              <div className="meta">
                {step.model && <>{step.model} · </>}
                {step.route_reason && <>{step.route_reason} · </>}
                {step.cost_usd && <>{formatCost(step.cost_usd)} · </>}
                {step.latency_ms != null && <>{step.latency_ms}ms</>}
                {step.artifact_id && <> · artifact {step.artifact_id.slice(0, 12)}</>}
              </div>
              {(step.input || step.output) && (
                <button
                  style={{ padding: "2px 8px", marginTop: 6, fontSize: 12 }}
                  onClick={() =>
                    setExpanded(expanded === step.seq ? null : step.seq)
                  }
                >
                  {expanded === step.seq ? "hide" : "detail"}
                </button>
              )}
              {expanded === step.seq && (
                <pre>
                  {JSON.stringify(
                    { input: step.input, output: step.output, error: step.error },
                    null,
                    2,
                  ).slice(0, 20_000)}
                </pre>
              )}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
