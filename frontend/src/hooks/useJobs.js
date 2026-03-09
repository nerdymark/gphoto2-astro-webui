import { useState, useEffect, useCallback, useRef } from "react";
import * as api from "../api/client";

/**
 * Poll the jobs list and individual active jobs.
 *
 * Polls GET /api/jobs every 2s.  When an active (non-terminal) job
 * exists, also fetches its full log via GET /api/jobs/{id} every 1s.
 */
export function useJobs() {
  const [jobsList, setJobsList] = useState([]);
  const [activeJobDetail, setActiveJobDetail] = useState(null);
  const pendingRef = useRef(false);

  const refresh = useCallback(async () => {
    if (pendingRef.current) return;
    pendingRef.current = true;
    try {
      const data = await api.listJobs();
      setJobsList(data.jobs ?? []);

      // Find the first running/queued job and fetch its full detail (with log).
      const active = (data.jobs ?? []).find(
        (j) => j.status === "running" || j.status === "queued"
      );
      if (active) {
        const detail = await api.getJob(active.id);
        setActiveJobDetail(detail);
      } else {
        setActiveJobDetail(null);
      }
    } catch {
      // ignore
    } finally {
      pendingRef.current = false;
    }
  }, []);

  const hasActive = !!activeJobDetail;

  useEffect(() => {
    refresh();
    // Poll faster when there's an active job.
    const id = setInterval(refresh, hasActive ? 1000 : 3000);
    return () => clearInterval(id);
  }, [refresh, hasActive]);

  const cancelJob = useCallback(async (jobId) => {
    try {
      await api.cancelJob(jobId);
      await refresh();
    } catch {
      // ignore
    }
  }, [refresh]);

  const hasActiveJobs = jobsList.some(
    (j) => j.status === "running" || j.status === "queued"
  );

  return { jobsList, activeJobDetail, hasActiveJobs, cancelJob, refresh };
}
