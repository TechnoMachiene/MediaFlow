export type Role = 'user' | 'manager' | 'admin' | 'super_admin';
export type Approval = 'pending' | 'approved' | 'rejected';
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
  submitter_email: string | null;
  original_filename: string;
  attempts: number;
  cached: boolean;
  error: string | null;
  result: AnalysisResult | null;
  media_kind: MediaKind;
  created_at: string;
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
