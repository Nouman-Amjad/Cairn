/**
 * Gateway client.
 *
 * Everything goes through the gateway; the UI never talks to the
 * orchestrator, the router or a tool server directly. That keeps exactly one
 * place where a token is attached to a request.
 */

export const GATEWAY =
  process.env.NEXT_PUBLIC_GATEWAY_URL ?? "http://localhost:8000";

export type TrajectoryState =
  | "PLANNING"
  | "EXECUTING"
  | "WAITING_APPROVAL"
  | "SYNTHESIZING"
  | "CRITIQUING"
  | "COMPLETE"
  | "PARTIAL"
  | "FAILED"
  | "ABANDONED";

export type EventType =
  | "state"
  | "plan"
  | "step"
  | "token"
  | "notice"
  | "approval"
  | "answer"
  | "error"
  | "done";

export interface CairnEvent {
  type: EventType;
  trajectory_id: string;
  seq: number;
  at: string;
  data: Record<string, unknown>;
}

export interface Step {
  seq: number;
  kind: string;
  tool: string | null;
  model: string | null;
  route: string | null;
  route_reason: string | null;
  cost_usd: string | null;
  latency_ms: number | null;
  artifact_id: string | null;
  sensitivity: string;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  at: string;
}

export interface Trajectory {
  id: string;
  query: string;
  state: TrajectoryState;
  answer: string | null;
  confidence: number | null;
  sensitivity: string;
  cost_usd: string;
  tokens: { local: number; cloud: number };
  prompt_version: string;
  plan: { steps?: { goal: string; why?: string }[] } | null;
  started_at: string;
  ended_at: string | null;
  steps: Step[];
}

export interface Approval {
  id: string;
  action: string;
  args: Record<string, unknown>;
  state: string;
  requested_by: string;
  required_approvals: number;
  approvals: { actor: string; at: string }[];
  trajectory_id: string | null;
  expires_at: string;
  result: Record<string, unknown> | null;
  denial_reason: string | null;
}

/**
 * In dev mode the gateway trusts an identity header. In production the
 * browser carries a session cookie from the IdP and this adds nothing.
 */
function authHeaders(): HeadersInit {
  const devUser = process.env.NEXT_PUBLIC_DEV_USER;
  return devUser
    ? { "x-cairn-dev-user": devUser, "x-cairn-dev-groups": "sre" }
    : {};
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${GATEWAY}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "content-type": "application/json",
      ...authHeaders(),
      ...(init?.headers ?? {}),
    },
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`${resp.status}: ${detail.slice(0, 300)}`);
  }
  return (await resp.json()) as T;
}

export function ask(
  query: string,
): Promise<{ trajectory_id: string; stream: string }> {
  return request("/v1/queries", {
    method: "POST",
    body: JSON.stringify({ query }),
  });
}

export function getTrajectory(id: string): Promise<Trajectory> {
  return request(`/v1/trajectories/${id}`);
}

export function listApprovals(): Promise<{ approvals: Approval[] }> {
  return request("/v1/approvals");
}

export function decide(
  id: string,
  approve: boolean,
  reason?: string,
): Promise<Approval> {
  return request(`/v1/approvals/${id}/decision`, {
    method: "POST",
    body: JSON.stringify({ approve, reason }),
  });
}

export function whoami(): Promise<{
  sub: string;
  email: string;
  groups: string[];
  spent_today_usd: string;
  daily_budget_usd: number;
}> {
  return request("/v1/me");
}

/**
 * Subscribe to an investigation's event stream.
 *
 * EventSource reconnects on its own and replays via Last-Event-ID, which the
 * gateway honours — a closed laptop lid resumes rather than losing the answer.
 */
export function subscribe(
  trajectoryId: string,
  onEvent: (event: CairnEvent) => void,
  onDone: () => void,
): () => void {
  const source = new EventSource(
    `${GATEWAY}/v1/queries/${trajectoryId}/events`,
    { withCredentials: true },
  );

  const handle = (raw: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(raw.data) as CairnEvent);
    } catch {
      // A malformed frame is not worth tearing the stream down for.
    }
  };

  const types: EventType[] = [
    "state",
    "plan",
    "step",
    "notice",
    "approval",
    "answer",
    "error",
  ];
  types.forEach((type) => source.addEventListener(type, handle as EventListener));
  source.addEventListener("done", ((raw: MessageEvent<string>) => {
    handle(raw);
    source.close();
    onDone();
  }) as EventListener);

  source.onerror = () => {
    // EventSource retries automatically; only a closed stream is terminal.
    if (source.readyState === EventSource.CLOSED) onDone();
  };

  return () => source.close();
}

export function formatCost(usd: string | number | null | undefined): string {
  const value = Number(usd ?? 0);
  if (value === 0) return "$0";
  return value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

export function stateTone(state: string): "good" | "warn" | "bad" | "busy" {
  if (state === "COMPLETE") return "good";
  if (state === "PARTIAL" || state === "WAITING_APPROVAL") return "warn";
  if (state === "FAILED" || state === "ABANDONED") return "bad";
  return "busy";
}
