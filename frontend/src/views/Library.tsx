import { useEffect, useMemo, useRef, useState, type DragEvent } from 'react';
import { useJobs } from '../hooks';
import { fmt, jobState, parseDate } from '../lib';
import type { Job } from '../types';
import { Button, Empty, ErrorNote, Icon, PageHeader, Segmented, Spinner } from '../ui';
import { ACCEPT_ATTR, uploads, useUploads, type Upload } from '../uploads';
import { JobSheet } from './JobSheet';
import { JobTile, TileSkeletons } from './JobTile';

type Filter = 'all' | 'progress' | 'review' | 'approved' | 'rejected';
const FILTERS: { value: Filter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'progress', label: 'In progress' },
  { value: 'review', label: 'In review' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Rejected' },
];

function UploadTile({ upload }: { upload: Upload }) {
  const failed = upload.phase === 'error';
  return (
    <div className={`tile tile-upload ${failed ? 'tile-failed' : ''}`}>
      <div className="thumb">
        <Icon name={upload.kind === 'video' ? 'film' : 'photo'} />
        {upload.preview && <img src={upload.preview} alt="" className="shown" />}
        {!failed && (
          <div className="thumb-veil">
            {upload.phase === 'sent' ? <Spinner /> : (
              <div
                className="progress" role="progressbar" aria-label={`Uploading ${upload.name}`}
                aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(upload.progress * 100)}
              >
                <span style={{ transform: `scaleX(${upload.progress})` }} />
              </div>
            )}
          </div>
        )}
      </div>
      <span className="tile-body">
        <span className="tile-title">{upload.name}</span>
        {failed ? (
          <>
            <span className="tile-sub tile-error" role="alert">{upload.error}</span>
            {upload.retryable && (
              <button type="button" className="link" onClick={() => uploads.retry(upload.id)}>
                Retry
              </button>
            )}
            <button type="button" className="link" onClick={() => uploads.dismiss(upload.id)}>
              Dismiss
            </button>
          </>
        ) : (
          <span className="tile-sub">
            {upload.phase === 'sent'
              ? 'Checking file…'
              : `Uploading ${Math.round(upload.progress * 100)}% of ${fmt.bytes(upload.size)}`}
          </span>
        )}
      </span>
    </div>
  );
}

export function Library() {
  const { items: pending, settled } = useUploads();
  const inFlight = pending.some((u) => u.phase !== 'error');
  const { jobs, more, error, loadingMore, reload, loadMore, replace } = useJobs(
    '/jobs/mine',
    (list) => inFlight || list.some((j) => jobState(j).active),
  );
  const [filter, setFilter] = useState<Filter>('all');
  const [openId, setOpenId] = useState<string | null>(null);
  const [dropping, setDropping] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);

  useEffect(() => {
    if (settled) void reload();
  }, [settled, reload]);

  // Once the server has registered a file as a job, the real tile replaces the upload tile.
  const visibleUploads = useMemo(
    () =>
      pending.filter(
        (u) =>
          u.phase !== 'sent' ||
          !jobs?.some(
            (j) =>
              j.original_filename === u.name &&
              parseDate(j.created_at).getTime() >= u.startedAt - 5000,
          ),
      ),
    [pending, jobs],
  );

  const shown = useMemo(
    () => jobs?.filter((j) => filter === 'all' || jobState(j).key === filter) ?? [],
    [jobs, filter],
  );
  const open: Job | null = jobs?.find((j) => j.job_id === openId) ?? null;

  const hasFiles = (e: DragEvent) => e.dataTransfer.types.includes('Files');
  const drop = {
    onDragEnter: (e: DragEvent) => {
      if (!hasFiles(e)) return;
      dragDepth.current += 1;
      setDropping(true);
    },
    onDragLeave: (e: DragEvent) => {
      if (!hasFiles(e)) return;
      dragDepth.current = Math.max(0, dragDepth.current - 1);
      if (!dragDepth.current) setDropping(false);
    },
    onDragOver: (e: DragEvent) => hasFiles(e) && e.preventDefault(),
    onDrop: (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragDepth.current = 0;
      setDropping(false);
      uploads.start(e.dataTransfer.files);
    },
  };

  const empty = jobs !== null && jobs.length === 0 && visibleUploads.length === 0;

  return (
    <div className="page" {...drop}>
      <PageHeader title="Library" subtitle="Your uploads and the drafts generated from them">
        <Button variant="primary" onClick={() => picker.current?.click()}>
          <Icon name="plus" /> Upload
        </Button>
      </PageHeader>
      <input
        ref={picker} type="file" hidden multiple accept={ACCEPT_ATTR}
        onChange={(e) => {
          if (e.target.files) uploads.start(e.target.files);
          e.target.value = '';
        }}
      />

      <div className="page-body">
        {error && <ErrorNote message={error} onRetry={reload} />}

        {!empty && jobs !== null && (
          <div className="toolbar">
            <Segmented label="Filter" value={filter} onChange={setFilter} options={FILTERS} />
          </div>
        )}

        {jobs === null && !error && <TileSkeletons />}

        {empty && (
          <Empty
            icon="upload"
            title="Upload your first photo or video"
            action={
              <Button variant="primary" onClick={() => picker.current?.click()}>
                Choose files
              </Button>
            }
          >
            Drop files anywhere on this page. MediaFlow drafts captions for Instagram, TikTok, and
            Facebook from each one. JPG, PNG, WebP, MP4, MOV, or WebM.
          </Empty>
        )}

        {(shown.length > 0 || visibleUploads.length > 0) && (
          <div className="grid">
            {filter === 'all' || filter === 'progress'
              ? visibleUploads.map((u) => <UploadTile key={u.id} upload={u} />)
              : null}
            {shown.map((job) => (
              <JobTile key={job.job_id} job={job} onOpen={() => setOpenId(job.job_id)} />
            ))}
          </div>
        )}

        {jobs !== null && !empty && shown.length === 0 && visibleUploads.length === 0 && (
          <p className="quiet">Nothing here matches “{FILTERS.find((f) => f.value === filter)?.label}”.</p>
        )}

        {more && (
          <div className="more">
            <Button busy={loadingMore} onClick={loadMore}>Show more</Button>
          </div>
        )}
      </div>

      {dropping && (
        <div className="dropzone" aria-hidden="true">
          <div><Icon name="upload" /><span>Drop to upload</span></div>
        </div>
      )}

      <JobSheet job={open} owned onClose={() => setOpenId(null)} onChanged={replace} />
    </div>
  );
}
