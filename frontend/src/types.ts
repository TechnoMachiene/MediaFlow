export type Role = 'user' | 'manager' | 'admin' | 'super_admin';
/** with_uploader: drafted, but its uploader hasn't accepted it or sent it for review yet. */
export type Approval = 'with_uploader' | 'pending' | 'approved' | 'rejected';
export type MediaKind = 'image' | 'video';

export interface User {
  id: string;
  email: string;
  role: Role;
  created_at: string;
}

export interface WorkspaceRef {
  id: string;
  name: string;
  role: Role;
}

/** The signed-in person, as seen from the workspace the request resolved to. */
export interface Me extends User {
  workspace: WorkspaceRef;
  workspaces: WorkspaceRef[];
  platform_admin: boolean;
}

export type AssignableRole = Exclude<Role, 'super_admin'>;

export interface Invitation {
  id: string;
  email: string;
  role: AssignableRole;
  status: 'pending' | 'accepted' | 'revoked' | 'expired';
  invited_by_email: string | null;
  created_at: string;
  expires_at: string;
}

/** Returned once, to the inviter: the token is never shown again. */
export interface CreatedInvitation extends Invitation {
  token: string;
  accept_path: string;
  /** Also emailed to the invitee (the server has email set up). */
  emailed?: boolean;
}

export interface InvitationPreview {
  workspace_name: string;
  email: string;
  role: AssignableRole;
  expires_at: string;
  account_exists: boolean;
}

export interface InvitationAccepted {
  access_token: string;
  workspace: WorkspaceRef;
}

export interface AuditEvent {
  id: number;
  created_at: string;
  actor_id: string | null;
  actor_email: string;
  action: string;
  target_type: string;
  target_id: string;
  target_label: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  request_id: string | null;
}

export interface AuditPage {
  items: AuditEvent[];
  next_before: number | null;
}

export interface DraftVersion {
  number: number;
  text: string;
  source: 'ai' | 'edit' | 'restore';
  restored_from: number | null;
  note: string | null;
  author_email: string | null;
  created_at: string;
  current: boolean;
}

export interface DraftSaved {
  version: DraftVersion;
  approval_status: Approval;
  returned_to_review: boolean;
}

export interface DraftComment {
  id: string;
  parent_id: string | null;
  version: number;
  field: string | null;
  kind: 'comment' | 'approved' | 'rejected';
  body: string;
  author_email: string;
  created_at: string;
  resolved_at: string | null;
  resolved_by_email: string | null;
}

export interface PublishingProvider {
  name: string;
  display_name: string;
  sandbox: boolean;
  idempotent: boolean;
  channels: string[];
  options: Record<string, string[]>;
  /** The account belongs to the installation (credentials in .env); operators only. */
  operator_only?: boolean;
}

export interface Connection {
  id: string;
  provider: string;
  provider_name: string;
  sandbox: boolean;
  channel: string;
  account_name: string;
  status: 'active' | 'needs_reauth' | 'disconnected';
  options: Record<string, string>;
  last_error: string | null;
  connected_by_email: string;
  token_expires_at: string | null;
  created_at: string;
}

export type PublicationStatus =
  | 'queued' | 'publishing' | 'processing' | 'retrying' | 'published' | 'failed' | 'unknown'
  | 'cancelled';

export interface Publication {
  id: string;
  scheduled_post_id: string;
  connection_id: string;
  account_name: string;
  provider: string;
  channel: string;
  sandbox: boolean;
  status: PublicationStatus;
  attempts: number;
  next_attempt_at: string | null;
  error_code: string | null;
  last_error: string | null;
  external_id: string | null;
  external_url: string | null;
  published_at: string | null;
  requested_by_email: string;
  created_at: string;
}

export interface SandboxFeed {
  posts: { external_id: string; account_name: string; channel: string; body: string;
    hashtags: string[]; state: string; created_at: string }[];
  requests: { account_name: string; kind: string; outcome: string; created_at: string }[];
}

export interface RetentionPolicy {
  originals_days: number | null;
  delete_after_publish: boolean;
  rejected_days: number | null;
}

export interface RetentionPreview {
  originals_expired: number;
  originals_published: number;
  rejected_deleted: number;
}

export interface RetentionView extends RetentionPolicy {
  effective_originals_days: number;
  default_days: number;
  max_days: number;
  preview: RetentionPreview;
}

export interface Timing {
  count: number;
  p50: number | null;
  p90: number | null;
  max: number | null;
}

export interface AnalyticsTotals {
  uploads: number; drafted: number; failed: number; approved: number; rejected: number;
  edits: number; comments: number; scheduled: number; published: number; publish_failed: number;
}

export interface AnalyticsRates {
  approval: number | null;
  first_pass_approval: number | null;
  failure: number | null;
  publish_success: number | null;
}

export interface AnalyticsReport {
  start: string;
  end: string;
  timezone: string;
  totals: AnalyticsTotals;
  rates: AnalyticsRates;
  timings: { queue_wait: Timing; analysis: Timing; review_wait: Timing; publish_lag: Timing };
  previous_totals: AnalyticsTotals;
  previous_rates: AnalyticsRates;
  by_kind: { kind: MediaKind; uploads: number; drafted: number; approved: number; rejected: number;
    analysis: Timing; review_wait: Timing }[];
  by_channel: { channel: string; provider: string; requested: number; published: number;
    failed: number; success_rate: number | null; top_error: string | null; publish_lag: Timing }[];
  series: { date: string; uploads: number; drafted: number; approved: number; rejected: number;
    published: number }[];
  insights: { kind: string; severity: 'info' | 'warn'; text: string }[];
}

export interface Meta {
  demo_mode: boolean;
  allow_signup: boolean;
  demo_password: string | null;
  demo_accounts: { email: string; role: Role }[];
}

export interface AnalysisResult {
  text: string;
  kind: MediaKind;
  model: string;
  tokens: number | null;
}

export interface Job {
  job_id: string;
  status: string;
  approval_status: Approval;
  /** Which version of the draft `result` holds; 1 is Gemini's. */
  draft_version?: number;
  /** Set once this job's original was deleted. */
  media_deleted_reason?: 'policy' | 'published' | 'manual' | null;
  submitter_email: string | null;
  original_filename: string;
  attempts: number;
  cached: boolean;
  error: string | null;
  result: AnalysisResult | null;
  media_kind: MediaKind;
  created_at: string;
  /** What to verify before approving; `block` ones must be fixed first. */
  checks?: DraftCheck[];
}

export interface DraftCheck {
  level: 'block' | 'warn';
  field: string | null;
  message: string;
}

export interface AppNotification {
  id: string;
  kind: string;
  title: string;
  body: string;
  job_id: string | null;
  route: string | null;
  created_at: string;
  read: boolean;
}

export interface NotificationList {
  items: AppNotification[];
  unread: number;
}

export interface Page<T> {
  items: T[];
  limit: number;
  offset: number;
}

export interface Stats {
  ready: number;
  processing: number;
  deferred: number;
  dead: number;
  /** Installation-wide; null unless you are a platform admin. */
  gemini_calls_today: number | null;
}

export interface Post {
  id: string;
  job_id: string;
  original_filename: string;
  media_kind: MediaKind;
  status: string;
  scheduled_at: string;
  timezone: string;
  local_date: string;
  content: string;
  created_at: string;
}

export interface CalendarData {
  timezone: string;
  days: { date: string; posts: Post[] }[];
}
