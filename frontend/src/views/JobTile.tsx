import { fmt, headline } from '../lib';
import type { Job } from '../types';
import { JobBadge, Thumb } from '../ui';

export function JobTile({
  job, onOpen, showSubmitter,
}: { job: Job; onOpen: () => void; showSubmitter?: boolean }) {
  const title = headline(job.result?.text, job.original_filename);
  return (
    <button type="button" className="tile" onClick={onOpen} title={job.original_filename}>
      <Thumb jobId={job.job_id} kind={job.media_kind} />
      <span className="tile-body">
        <span className="tile-title" dir="auto">{title}</span>
        <span className="tile-sub">
          {showSubmitter && job.submitter_email ? job.submitter_email : job.original_filename}
        </span>
        <span className="tile-foot">
          <JobBadge job={job} />
          <time dateTime={job.created_at}>{fmt.ago(job.created_at)}</time>
        </span>
      </span>
    </button>
  );
}

export function TileSkeletons({ count = 8 }: { count?: number }) {
  return (
    <div className="grid" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="tile tile-skeleton">
          <div className="thumb" />
          <span className="tile-body"><span className="bone" /><span className="bone short" /></span>
        </div>
      ))}
    </div>
  );
}
