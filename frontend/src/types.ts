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

export interface AuditPairDelta {
  required_delta: number;
  preferred_delta: number;
  coverage_delta: number;
  verdict_flips: {
    qual_id: string;
    qual_text: string;
    kind: QualKind;
    baseline: string;
    variant: string;
  }[];
  verdict_flip_count: number;
  verdicts_compared: number;
  baseline_pass: boolean;
  variant_pass: boolean;
  stage_flip: boolean;
}

export interface AuditPair {
  code: string;
  baseline_code: string;
  attribute: string;
  attribute_label: string;
  is_control: boolean;
  baseline_file: string;
  variant_file: string;
  baseline_name: string;
  variant_name: string;
  rank_displacement: number;
  delta: AuditPairDelta;
}

export interface AuditAttribute {
  attribute: string;
  label: string;
  pairs: number;
  verdict_flips: number;
  verdicts_compared: number;
  stage_flips: number;
  mean_coverage_delta: number;
  max_coverage_delta: number;
  mean_rank_displacement: number;
  max_rank_displacement: number;
  verdict_flip_rate: number;
}

export interface AuditRun {
  id: string;
  provider: string;
  model: string;
  status: string;
  passed: boolean | null;
  error: string;
  createdAt: string;
  pairs: AuditPair[];
  summary: {
    attributes: AuditAttribute[];
    pairs_measured: number;
    total_stage_flips: number;
    mean_coverage_delta: number;
    control_stage_flips: number;
    thresholds: Record<string, number>;
    passed: boolean;
    failures: string[];
    unpaired: string[];
    baselines: number;
  };
  thresholds: Record<string, number>;
}
