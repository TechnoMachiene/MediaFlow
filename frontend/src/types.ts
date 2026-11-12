export type Role = 'user' | 'manager' | 'admin' | 'super_admin';
export type Approval = 'pending' | 'approved' | 'rejected';
export type MediaKind = 'image' | 'video';

export interface User {
  id: string;
  email: string;
  role: Role;
  created_at: string;
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
  gemini_calls_today: number;
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
