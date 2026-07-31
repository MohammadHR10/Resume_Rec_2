import type {
  AppConfig,
  AuditRun,
  ChatMessage,
  ChatTurn,
  GridAction,
  Qualification,
  ScreeningDetail,
  ScreeningSummary,
  Stage,
  StagePayload,
} from "./types.ts";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* a non-JSON error body is still worth the status line */
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function json(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

// -- configuration ----------------------------------------------------------

export const getConfig = () => request<AppConfig>("/api/config");

export const saveConfig = (body: Record<string, unknown>) =>
  request<AppConfig>("/api/config", json("PUT", body));

export const listModels = (provider: string) =>
  request<{ provider: string; configured: boolean; models: string[] }>(
    `/api/config/models?provider=${encodeURIComponent(provider)}`,
  );

export const testProvider = (provider: string) =>
  request<{ provider: string; ok: boolean; detail: string }>(
    `/api/config/test?provider=${encodeURIComponent(provider)}`,
    { method: "POST" },
  );

// -- screenings -------------------------------------------------------------

export const listScreenings = () => request<ScreeningSummary[]>("/api/screenings");

export const createScreening = (jobTitle: string) =>
  request<{ id: string }>("/api/screenings", json("POST", { jobTitle }));

export const getScreening = (id: string) => request<ScreeningDetail>(`/api/screenings/${id}`);

export const deleteScreening = (id: string) =>
  request<{ deleted: boolean }>(`/api/screenings/${id}`, { method: "DELETE" });

export async function parseJobDescription(id: string, file: File) {
  const form = new FormData();
  form.append("file", file);
  return request<{
    jobTitle: string;
    qualifications: Qualification[];
    provider: string;
    model: string;
    warning: string;
  }>(`/api/screenings/${id}/parse-jd`, { method: "POST", body: form });
}

export const getQualifications = (id: string) =>
  request<{ qualifications: Qualification[]; confirmed: boolean }>(
    `/api/screenings/${id}/qualifications`,
  );

export const saveQualifications = (
  id: string,
  qualifications: { text: string; kind: string }[],
  confirmed: boolean,
) =>
  request<{ qualifications: Qualification[]; confirmed: boolean }>(
    `/api/screenings/${id}/qualifications`,
    json("PUT", { qualifications, confirmed }),
  );

export async function uploadResumes(id: string, files: File[], replace = true) {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  form.append("replace", String(replace));
  return request<{ added: number; skipped: string[]; total: number }>(
    `/api/screenings/${id}/candidates`,
    { method: "POST", body: form },
  );
}

export const startEvaluation = (id: string) =>
  request<{ jobId: string; candidates: number; provider: string; model: string }>(
    `/api/screenings/${id}/evaluate`,
    { method: "POST" },
  );

// -- stages -----------------------------------------------------------------

export const getStage = (id: string, stage: Stage) =>
  request<StagePayload>(`/api/screenings/${id}/stages/${stage}`);

export const stageAction = (
  id: string,
  stage: Stage,
  candidateIds: string[],
  action: "promote" | "reject" | "restore",
  note = "",
) =>
  request<{ moved: { candidateId: string; from: string; to: string; override: boolean }[]; stageCounts: Record<string, number> }>(
    `/api/screenings/${id}/stages/${stage}/actions`,
    json("POST", { candidateIds, action, note }),
  );

export const auditTrail = (id: string) =>
  request<
    {
      id: string;
      name: string;
      from_stage: string;
      to_stage: string;
      action: string;
      override: number;
      actor: string;
      note: string;
      created_at: string;
    }[]
  >(`/api/screenings/${id}/audit-trail`);

export const exportUrl = (id: string, stage: Stage, format: "xlsx" | "csv") =>
  `/api/screenings/${id}/stages/${stage}/export?format=${format}`;

// -- chat -------------------------------------------------------------------

export const getChat = (id: string, stage: Stage) =>
  request<{ sessionId: string; mode: string; provider: string; model: string; messages: ChatMessage[] }>(
    `/api/screenings/${id}/chat/${stage}`,
  );

export const askChat = (id: string, stage: Stage, question: string) =>
  request<ChatTurn>(`/api/screenings/${id}/chat/${stage}`, json("POST", { question }));

export const chatActions = (sessionId: string, since: number) =>
  request<{ actions: GridAction[] }>(`/api/chat/${sessionId}/actions?since=${since}`);

// -- bias audit -------------------------------------------------------------

export interface Corpus {
  name: string;
  isDefault: boolean;
  files: number;
  baselines: number;
  pairs: number;
  positionDescription: string;
  skillLevels: Record<string, number>;
  byAttribute: { attribute: string; label: string; pairs: number }[];
  unpaired: string[];
}

export const getCorpus = () =>
  request<{ root: string; corpora: Corpus[] }>("/api/audits/corpus");

export const startAudit = (
  screeningId: string,
  provider?: string,
  model?: string,
  corpus?: string,
) =>
  request<{ jobId: string; auditId: string; provider: string; model: string; corpus: string }>(
    "/api/audits",
    json("POST", { screeningId, provider, model, corpus }),
  );

export const listAudits = () =>
  request<
    {
      id: string;
      provider: string;
      model: string;
      status: string;
      passed: boolean | null;
      created_at: string;
      stageFlips: number | null;
      meanCoverageDelta: number | null;
      pairs: number | null;
    }[]
  >("/api/audits");

export const getAudit = (id: string) => request<AuditRun>(`/api/audits/${id}`);

// -- job progress -----------------------------------------------------------

const POLL_INTERVAL_MS = 1500;

export function subscribeToJob(
  jobId: string,
  onProgress: (message: string) => void,
  onDone: () => void,
  onError: (message: string) => void,
): () => void {
  let since = 0;
  let stopped = false;

  async function poll(): Promise<void> {
    if (stopped) return;
    try {
      const data = await request<{
        events: string[];
        status: string;
        error: string | null;
      }>(`/api/jobs/${jobId}/progress?since=${since}`);

      for (const message of data.events) onProgress(message);
      since += data.events.length;

      if (data.status === "done") {
        stopped = true;
        onDone();
        return;
      }
      if (data.status === "error") {
        stopped = true;
        onError(data.error ?? "Unknown error");
        return;
      }
    } catch {
      /* a network hiccup should not end the run — retry on the next tick */
    }
    if (!stopped) window.setTimeout(poll, POLL_INTERVAL_MS);
  }

  void poll();
  return () => {
    stopped = true;
  };
}
