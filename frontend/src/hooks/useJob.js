import { useState, useEffect, useCallback, useRef } from "react";
import * as api from "../api/client";

/**
 * Poll a background job until it reaches a terminal state.
 *
 * Returns { job, startJob, cancelJob } where:
 *   - job: current job state (null until started)
 *   - startJob(jobId): begin polling the given job ID
 *   - cancelJob(): request cancellation of the active job
 *
 * The caller's onComplete / onFail callbacks fire once when the job
 * reaches a terminal state.
 */
export function useJob({ onComplete, onFail } = {}) {
  const [job, setJob] = useState(null);
  const [pollTrigger, setPollTrigger] = useState(0);
  const jobIdRef = useRef(null);
  const callbacksRef = useRef({ onComplete, onFail });
  useEffect(() => {
    callbacksRef.current = { onComplete, onFail };
  });

  // Poll loop – starts when pollTrigger changes (i.e. startJob is called).
  useEffect(() => {
    if (!jobIdRef.current) return;

    let active = true;
    const poll = async () => {
      while (active && jobIdRef.current) {
        try {
          const data = await api.getJob(jobIdRef.current);
          if (!active) break;
          setJob(data);

          if (data.status === "completed") {
            jobIdRef.current = null;
            callbacksRef.current.onComplete?.(data);
            break;
          }
          if (data.status === "failed" || data.status === "cancelled") {
            jobIdRef.current = null;
            callbacksRef.current.onFail?.(data);
            break;
          }
        } catch {
          // Network hiccup – keep polling.
        }
        await new Promise((r) => setTimeout(r, 1000));
      }
    };
    poll();
    return () => {
      active = false;
    };
  }, [pollTrigger]);

  const startJob = useCallback((jobId) => {
    jobIdRef.current = jobId;
    setJob({ id: jobId, status: "queued", progress: 0, total: 0, message: "" });
    setPollTrigger((n) => n + 1);
  }, []);

  const cancelJob = useCallback(async () => {
    if (jobIdRef.current) {
      try {
        await api.cancelJob(jobIdRef.current);
      } catch {
        // ignore
      }
    }
  }, []);

  return { job, startJob, cancelJob };
}
