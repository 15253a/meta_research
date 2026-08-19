export type ReadinessCheck = {
  name: string;
  status: "ready" | "stale" | "unavailable";
  revision?: number;
  count?: number;
};

export type OwnerSnapshot = {
  status: "ready" | "unavailable";
  revision: number;
  facts: Record<string, string | number | boolean | null>;
};

export type UnavailableCapability = {
  capability: string;
  status: "capability_unavailable";
  reason: { code: string; message: string };
};

export type PublicSnapshot = {
  product: { name: string; version: string };
  revision: number;
  readiness: { status: "ready" | "unavailable"; checks: ReadinessCheck[] };
  research_space: {
    status: "empty" | "active";
    quest_count: number;
    question_count: number;
    foreground_cycle_count: number;
  };
  owners: Record<string, OwnerSnapshot>;
  unavailable: UnavailableCapability[];
};

export async function fetchSnapshot(signal?: AbortSignal): Promise<PublicSnapshot> {
  const response = await fetch("/api/v1/snapshot", {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`snapshot_unavailable:${response.status}`);
  }
  return (await response.json()) as PublicSnapshot;
}

export function followProjection(
  onRevision: (revision: number) => void,
  onSnapshotRequired: () => void,
  onConnection: (connected: boolean) => void,
): () => void {
  const stream = new EventSource("/api/v1/events");
  stream.onopen = () => onConnection(true);
  stream.onerror = () => onConnection(false);
  stream.addEventListener("system.ready", (event) => {
    onRevision(Number((event as MessageEvent).lastEventId));
  });
  stream.addEventListener("snapshot.required", () => onSnapshotRequired());
  return () => stream.close();
}
