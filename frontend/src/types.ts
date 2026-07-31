export type QualKind = "required" | "preferred";
export type Verdict = "Meets" | "Partial" | "No";
export type Stage = "1" | "2" | "3" | "rejected";

export interface Qualification {
  id: string;
  label: string;
  text: string;
  kind: QualKind;
  position: number;
}

export interface VerdictEntry {
  verdict: Verdict;
  evidence: string;
  /** The model's explanation. Absent on screenings scored before it was captured. */
  reasoning?: string;
}

export interface CandidateRow {
  id: string;
  name: string;
  stage: Stage;
  rank: number;
  required_met: number;
  required_total: number;
  preferred_met: number;
  preferred_total: number;
  ai_pass: boolean;
  summary: string | null;
  error: string | null;
  source_files: string[];
  verdicts: Record<string, VerdictEntry>;
}

export interface ScreeningSummary {
  id: string;
  job_title: string;
  status: string;
  provider: string;
  model: string;
  quals_confirmed: number;
  created_at: string;
  candidates: number;
  qualifications: number;
}

export interface ScreeningDetail {
  screening: {
    id: string;
    jobTitle: string;
    jdFilename: string;
    hasJd: boolean;
    status: string;
    qualsConfirmed: boolean;
    provider: string;
    model: string;
    createdAt: string;
  };
  qualifications: Qualification[];
  stageCounts: Record<string, number>;
}

export interface StagePayload {
  stage: Stage;
  qualifications: Qualification[];
  candidates: CandidateRow[];
  stageCounts: Record<string, number>;
}

export interface ProviderCard {
  name: string;
  label: string;
  configured: boolean;
  baseUrl: string;
}

export interface ConnectionCard {
  provider: string;
  label: string;
  baseUrl: string;
  baseUrlSource: "config" | "environment" | "unset";
  hasKey: boolean;
  /** A masked hint only — the token itself is never sent to the browser. */
  keyHint: string;
  keySource: "config" | "environment" | "unset";
  projectId: string;
  requesterId: string;
  supportsAttribution: boolean;
  configured: boolean;
}

export interface AppConfig {
  providers: ProviderCard[];
  connections: ConnectionCard[];
  provider: string;
  model: string;
  chatMode: "harness" | "structured";
  chatModes: Record<string, string>;
  harnessCli: string;
  auditThresholds: Record<string, number>;
  adapters: { name: string; available: boolean }[];
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  mode: string;
  model: string;
  created_at: string;
}

export interface GridAction {
  id: number;
  type: string;
  clear?: boolean;
  sort?: { column: string; direction: "asc" | "desc" };
  filters?: { column: string; value: string }[];
}

export interface ChatTurn {
  sessionId: string;
  answer: string;
  mode: string;
  degradedFrom: string;
  actions: GridAction[];
  trace: { round: number; tool: string; args: Record<string, unknown> }[];
  model: string;
  provider: string;
}

export interface AuditClassCandidate {
  name: string;
  file: string;
  requiredMet: number;
  requiredTotal: number;
  preferredMet: number;
  preferredTotal: number;
  aiPass: boolean;
  rank: number | null;
  error: string;
}

export interface AuditClass {
  attribute: string;
  attributeLabel: string;
  value: string;
  candidates: AuditClassCandidate[];
  meanRequired: number;
  meanPreferred: number;
  passed: number;
  total: number;
}

export interface AuditLevel {
  level: string;
  note: string;
  expectedRequired: number | null;
  expectedPreferred: number | null;
  requiredTotal: number | null;
  preferredTotal: number | null;
  expectedStage1: string | null;
  referenceRequired: number | null;
  referencePreferred: number | null;
  classes: AuditClass[];
}

export interface AuditSummary {
  groups: number;
  coverageDepartures: {
    level: string;
    label: string;
    expected: number;
    actual: number;
    candidates: string[];
  }[];
  advancementFlips: {
    level: string;
    label: string;
    expected: string;
    candidates: string[];
  }[];
}

export interface AuditRun {
  id: string;
  provider: string;
  model: string;
  status: string;
  error: string;
  createdAt: string;
  corpus: string;
  levels: AuditLevel[];
  summary: AuditSummary;
}
