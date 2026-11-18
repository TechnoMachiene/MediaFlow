import { useCallback, useEffect, useRef, useState } from 'react';
import { useLive } from './live';
import { ApiError, fetchJobs } from './api';
import type { Job } from './types';

const PAGE = 24;

/** A paginated job list that can keep itself fresh while work is in flight. */
export function useJobs(path: string, shouldPoll: (jobs: Job[]) => boolean = () => false) {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [more, setMore] = useState(false);
  const [error, setError] = useState('');
  const [loadingMore, setLoadingMore] = useState(false);
  const count = useRef(PAGE);
  const request = useRef(0);

  const load = useCallback(async () => {
    const id = ++request.current;
    try {
      const page = await fetchJobs(path, count.current);
      if (id !== request.current) return; // a newer request superseded this one
      setJobs(page.items);
      setMore(page.more);
      setError('');
    } catch (err) {
      if (id !== request.current) return;
      setError(err instanceof ApiError ? err.message : 'Could not load.');
    }
  }, [path]);

  useEffect(() => {
    count.current = PAGE;
    setJobs(null);
    setError('');
    void load();
  }, [load]);
  useLive(['jobs'], load); // someone else uploaded, sent, decided or edited

  const polling = jobs !== null && shouldPoll(jobs);
  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [polling, load]);

  const loadMore = useCallback(async () => {
    count.current += PAGE;
    setLoadingMore(true);
    await load();
    setLoadingMore(false);
  }, [load]);

  const replace = useCallback((job: Job) => {
    setJobs((list) =>
      list?.map((j) =>
        // Detail responses may omit the submitter; keep what the list already knew.
        j.job_id === job.job_id
          ? { ...job, submitter_email: job.submitter_email ?? j.submitter_email }
          : j,
      ) ?? list,
    );
  }, []);
  const remove = useCallback((id: string) => {
    setJobs((list) => list?.filter((j) => j.job_id !== id) ?? list);
  }, []);

  return { jobs, more, error, loadingMore, reload: load, loadMore, replace, remove };
}
