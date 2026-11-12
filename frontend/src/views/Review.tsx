import { useState } from 'react';
import { useJobs } from '../hooks';
import { useSession } from '../session';
import { Button, Empty, ErrorNote, PageHeader, Segmented } from '../ui';
import { JobSheet } from './JobSheet';
import { JobTile, TileSkeletons } from './JobTile';

type Queue = 'review' | 'approved' | 'rejected';
const QUEUES: { value: Queue; label: string }[] = [
  { value: 'review', label: 'Waiting' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Rejected' },
];
const EMPTY: Record<Queue, [string, string]> = {
  review: ['Nothing is waiting for review', 'Finished drafts appear here as soon as analysis completes.'],
  approved: ['No approved drafts yet', 'Drafts you approve are listed here.'],
  rejected: ['No rejected drafts', 'Drafts you reject are listed here.'],
};

export function Review() {
  const { user } = useSession();
  const [queue, setQueue] = useState<Queue>('review');
  const { jobs, more, error, loadingMore, reload, loadMore, replace, remove } = useJobs(
    `/jobs/${queue}`,
  );
  const [openId, setOpenId] = useState<string | null>(null);
  const open = jobs?.find((j) => j.job_id === openId) ?? null;

  return (
    <div className="page">
      <PageHeader title="Review" subtitle="Approve or reject finished drafts" />
      <div className="page-body">
        <div className="toolbar">
          <Segmented
            label="Queue" value={queue} options={QUEUES}
            onChange={(q) => { setOpenId(null); setQueue(q); }}
          />
        </div>
        {error && <ErrorNote message={error} onRetry={reload} />}
        {jobs === null && !error && <TileSkeletons count={4} />}
        {jobs?.length === 0 && (
          <Empty icon="review" title={EMPTY[queue][0]}>{EMPTY[queue][1]}</Empty>
        )}
        {jobs && jobs.length > 0 && (
          <div className="grid">
            {jobs.map((job) => (
              <JobTile key={job.job_id} job={job} showSubmitter onOpen={() => setOpenId(job.job_id)} />
            ))}
          </div>
        )}
        {more && (
          <div className="more"><Button busy={loadingMore} onClick={loadMore}>Show more</Button></div>
        )}
      </div>
      <JobSheet
        job={open}
        owned={open?.submitter_email === user.email}
        onClose={() => setOpenId(null)}
        onChanged={replace}
        onReviewed={(job) => remove(job.job_id)}
      />
    </div>
  );
}
