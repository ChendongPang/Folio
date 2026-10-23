export type FileRole = 'source' | 'template' | 'style_reference';
export interface UploadedFile {
  id: string;
  server_id?: string;
  file: { name: string; size: number; lastModified: number };
  role: FileRole;
  queued?: boolean;
  uploading?: boolean;
  failed?: boolean;
  removing?: boolean;
  changingRole?: boolean;
  error?: string;
}
export interface HistoryItem { id: string; title: string; title_is_default?: boolean; pinned?: boolean; updated_at?: string }
export interface DirectoryPreviewSource {
  id: string;
  name: string;
  download_url: string;
}
export interface DirectorySection { number: string; title: string; level: number }
export interface DirectoryConfirmationIssue { code: string; message: string }
export interface DirectoryResearchReading { label: string; characters?: number }
export interface DirectoryResearchFile {
  path: string;
  name?: string;
  inspected: boolean;
  read: boolean;
  characters?: number;
  source_version?: string;
  readings?: DirectoryResearchReading[];
}
export interface DirectoryWebSource {
  evidence_id: string;
  url: string;
  title: string;
  retrieved_at?: string;
}
export interface DirectoryWebSearch {
  query: string;
  purpose: string;
  status: 'completed' | 'failed' | 'unavailable' | 'interrupted';
  cached?: boolean;
  error_code?: string;
  evidence_ids: string[];
  source_count?: number;
}
export interface DirectoryWebResearch {
  available: boolean;
  searches: DirectoryWebSearch[];
  evidence: DirectoryWebSource[];
  findings?: { text: string; evidence_ids: string[] }[];
}
export interface DirectoryResearch {
  status: 'ready' | 'partial' | 'no_sources';
  summary: string;
  unresolved: string[];
  files: DirectoryResearchFile[];
  calls: number;
  rounds: number;
  termination_reason?: string | null;
  download_url?: string;
  web_research?: DirectoryWebResearch;
}
export interface DisplayField { label: string; value: string }
export interface FailureDetails {
  reason?: string;
  stage_label?: string;
  action_label?: string;
  task_title?: string;
  section_labels?: string[];
  attempts?: number;
  saved_sections?: number;
  total_sections?: number;
  progress_note?: string;
  diagnostic_id?: string;
  code?: string;
  action?: string;
  details?: Record<string, unknown>;
}
export interface ReviewDetails {
  phase?: string;
  status?: string;
  step?: string;
  index?: number;
  total?: number;
  completed?: number;
  round_id?: string | number;
  unit_id?: string | number;
  sections?: { title?: string; section_id?: string; revision?: number }[];
  reasons?: string[];
  findings?: { description?: string }[];
  outcome?: string;
  reason?: string;
  elapsed_ms?: number;
  calls?: { model_calls?: number; cache_hits?: number; failed_calls?: number; tasks?: Record<string, { model_calls?: number; cache_hits?: number; failed_calls?: number }> };
}
export interface TaskUsage {
  task_id: string;
  title?: string;
  started_at?: string;
  finished_at?: string;
  elapsed_ms?: number;
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  model_calls?: number;
  cache_hits?: number;
  failed_calls?: number;
}
export interface DirectoryReview {
  revision: string;
  phase: 'directory_review' | 'generating';
  title?: string;
  reason?: string;
  directory: { sections: DirectorySection[]; preview?: DirectoryPreviewSource; research?: DirectoryResearch; confirmable?: boolean; confirmation_issues?: DirectoryConfirmationIssue[]; notes?: string[]; style_url?: string; task_usage?: TaskUsage[] };
  research?: DirectoryResearch;
  confirmable?: boolean;
  notes?: string[];
  failed_advice?: string;
  operation_error?: { text: string; diagnostics?: Record<string, unknown> };
}
export interface PreviewBlock { kind: string; text?: string; level?: number; rows?: string[][] }
export interface DocumentPreview {
  title: string;
  titleIsLabel?: boolean;
  downloadUrl: string;
  kind: 'directory';
  status: string;
  pages: string[];
  blocks: PreviewBlock[];
  showStructure: boolean;
}
export interface Artifact { elapsed_ms?: number | null; filename: string; download_url: string; pending_count?: number; proposal_count?: number; status?: string; issues?: string[]; confirmation_url?: string; archived?: boolean; archive_round?: number; task_usage?: TaskUsage[] }
export interface ConfirmationHistory { round: number; request_id: string; download_url: string; filename: string }
export interface ConfirmationIssue { issue_id: string; description: string; section_ids: string[]; key: string; kind: string }
export interface ConfirmationData {
  job_id: string;
  status: string;
  revision: string;
  issues: ConfirmationIssue[];
  used_calls: number;
  default_additional_calls: number;
  rounds: number;
  history?: ConfirmationHistory[];
}
export type DirectoryModelStatus = 'waiting' | 'connected' | 'thinking' | 'responding';
export interface DirectoryProgressActivity {
  id: string;
  text: string;
  detail?: string;
  status: 'running' | 'completed' | 'failed';
  phase?: string;
}
export interface Activity { id: string; text: string; detail?: string; status?: DirectoryProgressActivity['status']; phase?: string; review?: ReviewDetails; failure?: FailureDetails; jobId?: string }
export interface DirectoryProgress {
  elapsedSeconds?: number;
  stepElapsedSeconds?: number;
  modelStatus?: DirectoryModelStatus;
}
export interface StyleAnalysisReport {
  revision: string;
  status: string;
  document_names: string[];
  style_url: string;
  summary?: string;
  cache_hit?: boolean;
}
export interface Message {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  contentOrigin?: 'model' | 'system';
  files?: string[];
  working: boolean;
  workingLabel?: string;
  notes: string[];
  activities: Activity[];
  currentActivityId?: string;
  activityLabel?: string;
  historyDeferred?: boolean;
  historyLoading?: boolean;
  activityComplete?: boolean;
  directoryProgress?: DirectoryProgress;
  progressKind?: 'directory' | 'style' | 'message';
  styleAnalysis?: StyleAnalysisReport;
  failure?: FailureDetails;
  jobId?: string;
  diagnostics?: Record<string, unknown>;
  artifact?: Artifact;
  taskUsage?: TaskUsage[];
  directoryReview?: DirectoryReview;
}
export interface WorkspaceState {
  files: UploadedFile[];
  messages: Message[];
  conversationId: string | null;
  title: string;
  titleIsLabel: boolean;
  text: string;
  saveStatus: string;
  historyItems: HistoryItem[];
  deletingConversation: string | null;
  updatingConversation: string | null;
  historyMore: boolean;
  historyError: boolean;
  restoring: boolean;
  restoreFailed: boolean;
  running: boolean;
  generating: boolean;
  activeJob: string | null;
  activeJobStatus: string | null;
  retryJob: string | null;
  taskControlPending: boolean;
  directoryReview: DirectoryReview | null;
  confirmationJob: string | null;
  confirmation: ConfirmationData | null;
  confirmationAnswers: Record<string, string>;
  confirmationClosedComments: Record<string, boolean>;
  confirmationHistory: ConfirmationHistory[];
  confirmationCalls: number;
  confirmationLoading: boolean;
  confirmationSubmitting: boolean;
  confirmationError: string;
  confirmationCollapsed: boolean;
  confirmationSkipped: boolean;
  preview: DocumentPreview;
  previewFootnote: string;
  previewOpen: boolean;
  materialsOpen: boolean;
  sidebarOpen: boolean;
  toast: string;
  toastTone: 'neutral' | 'success' | 'error' | 'warning';
  announcement: string;
}
