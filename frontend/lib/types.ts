export type Source = {
  source: string; value: string | null; observation_id: string | null;
  availability: string; comparison?: string; age_seconds: string | null;
  state: string; weight_bps: number; score_bps: number | null; reasons: string[];
  score_reasons?: string[]; unknown_dependencies: string[];
  peer_median?: string | null; deviation_threshold?: string | null;
};
export type Gate = { name: string; observed: number | null; required: number; comparison: string; passed: boolean; reason: string };
export type Round = {
  report_id: string; cutoff: string; candidate_value: string | null;
  quality_score_bps: number; total_weight_bps: number; eligible_source_count: number;
  status: string; reasons: string[];
};
export type Assessment = Round & {
  gates: Gate[]; registry_id: string; aggregation_report_id: string; scoring_report_id: string;
  components: Record<string, number>; independent_domain_count: null;
};
export type Registry = {
  registry_id: string; version: string; scope: string; reviewed_at: string;
  nodes: { id: string; kind: string; description: string; evidence_ids: string[] }[];
  sources: { source: string; node_id: string }[];
  edges: { downstream: string; upstream: string; relationship: string; explanation: string; evidence_ids: string[] }[];
};
export type Snapshot = {
  asset: string; dataset: string; mode: string; cursor: number; as_of: string;
  assessment: Assessment | null; sources: Source[]; registry: Registry | null;
  history: Round[]; publication_ready: false; value: null; confidence: null;
  scoring_report_id: string | null;
};
