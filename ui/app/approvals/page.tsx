"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { type Approval, decide, listApprovals } from "@/lib/api";

/**
 * The approval queue.
 *
 * Requests you raised yourself never appear here — the gateway filters them
 * out and the approval service rejects them anyway. The UI is not where that
 * rule is enforced; this is just the version of it a human sees.
 */
export default function ApprovalsPage() {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    listApprovals()
      .then((body) => setApprovals(body.approvals))
      .catch((caught: unknown) =>
        setError(caught instanceof Error ? caught.message : String(caught)),
      );
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 10_000);
    return () => clearInterval(timer);
  }, [load]);

  async function act(id: string, approve: boolean) {
    setBusy(id);
    setError("");
    try {
      await decide(id, approve, approve ? undefined : "denied from the console");
      load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <h2>Pending approvals</h2>
      {error && (
        <div className="card error">
          <span className="mono">{error}</span>
        </div>
      )}
      {approvals.length === 0 && (
        <div className="card muted">Nothing waiting on you.</div>
      )}

      {approvals.map((approval) => {
        const remaining = Math.max(
          0,
          Math.round((Date.parse(approval.expires_at) - Date.now()) / 60_000),
        );
        const have = approval.approvals?.length ?? 0;
        return (
          <div className="card" key={approval.id}>
            <div>
              <span className="badge warn">{approval.action}</span>{" "}
              <span className="muted">
                requested by {approval.requested_by} · expires in {remaining} min
                {approval.required_approvals > 1 &&
                  ` · ${have}/${approval.required_approvals} approvals`}
              </span>
            </div>
            <pre>{JSON.stringify(approval.args, null, 2)}</pre>
            {approval.trajectory_id && (
              <p>
                <Link href={`/trajectories/${approval.trajectory_id}`}>
                  Read the investigation that asked for this
                </Link>
              </p>
            )}
            <div style={{ display: "flex", gap: 10, marginTop: 10 }}>
              <button
                className="primary"
                disabled={busy === approval.id}
                onClick={() => act(approval.id, true)}
              >
                Approve
              </button>
              <button
                className="danger"
                disabled={busy === approval.id}
                onClick={() => act(approval.id, false)}
              >
                Deny
              </button>
            </div>
          </div>
        );
      })}
    </>
  );
}
