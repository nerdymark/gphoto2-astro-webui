import { useEffect, useRef } from "react";

function statusColor(status) {
  switch (status) {
    case "running": return "text-blue-400";
    case "completed": return "text-green-400";
    case "failed": return "text-red-400";
    case "cancelled": return "text-amber-400";
    default: return "text-slate-400";
  }
}

function statusBg(status) {
  switch (status) {
    case "running": return "bg-blue-500/20 border-blue-500/30";
    case "completed": return "bg-green-500/10 border-green-500/20";
    case "failed": return "bg-red-500/10 border-red-500/20";
    case "cancelled": return "bg-amber-500/10 border-amber-500/20";
    default: return "bg-slate-500/10 border-slate-500/20";
  }
}

function formatTime(epoch) {
  if (!epoch) return "—";
  return new Date(epoch * 1000).toLocaleTimeString();
}

function formatDuration(start, end) {
  if (!start) return "";
  const elapsed = (end || Date.now() / 1000) - start;
  const m = Math.floor(elapsed / 60);
  const s = Math.floor(elapsed % 60);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function LogScroll({ lines }) {
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [lines?.length]);

  if (!lines || lines.length === 0) return null;

  return (
    <div className="bg-slate-950 rounded-lg border border-slate-700 p-2 max-h-48 overflow-y-auto font-mono text-xs text-slate-400 space-y-0.5">
      {lines.map((line, i) => (
        <div key={i} className={line.includes("FAILED") || line.includes("ERROR") ? "text-red-400" : ""}>
          {line}
        </div>
      ))}
      <div ref={endRef} />
    </div>
  );
}

export default function JobsPanel({ jobsList, activeJobDetail, onCancel }) {
  if (!jobsList || jobsList.length === 0) {
    return (
      <div className="rounded-xl bg-slate-800 border border-slate-700 p-4">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300 mb-2">
          Jobs
        </h2>
        <p className="text-slate-500 text-sm">No jobs yet. Start a burst capture or stacking operation.</p>
      </div>
    );
  }

  return (
    <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-3">
      <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
        Jobs
      </h2>

      <div className="space-y-2">
        {jobsList.map((job) => {
          const isActive = job.status === "running" || job.status === "queued";
          const detail = activeJobDetail?.id === job.id ? activeJobDetail : null;

          return (
            <div
              key={job.id}
              className={`rounded-lg border p-3 space-y-2 ${statusBg(job.status)}`}
            >
              {/* Header row */}
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <span className={`text-xs font-bold uppercase ${statusColor(job.status)}`}>
                    {job.status}
                  </span>
                  <span className="text-xs text-slate-400 uppercase">
                    {job.type}
                  </span>
                  <span className="text-xs text-slate-500 font-mono truncate">
                    {job.id}
                  </span>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-xs text-slate-500">
                    {formatDuration(job.started_at, job.finished_at)}
                  </span>
                  {isActive && (
                    <button
                      onClick={() => onCancel(job.id)}
                      className="text-xs px-2 py-0.5 rounded bg-red-700 hover:bg-red-600 text-white transition-colors"
                    >
                      Cancel
                    </button>
                  )}
                </div>
              </div>

              {/* Message */}
              <p className="text-xs text-slate-300">{job.message}</p>

              {/* Progress bar */}
              {isActive && job.total > 0 && (
                <div className="space-y-1">
                  <div className="flex justify-between text-xs text-slate-500">
                    <span>{job.progress}/{job.total}</span>
                    <span>{Math.round((job.progress / job.total) * 100)}%</span>
                  </div>
                  <div className="h-1.5 bg-slate-700 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-blue-500 rounded-full transition-all duration-500"
                      style={{ width: `${(job.progress / job.total) * 100}%` }}
                    />
                  </div>
                </div>
              )}

              {/* Log scroll for active job */}
              {detail && detail.log && detail.log.length > 0 && (
                <LogScroll lines={detail.log} />
              )}

              {/* Error display */}
              {job.error && (
                <p className="text-xs text-red-400 bg-red-500/10 rounded px-2 py-1">
                  {job.error}
                </p>
              )}

              {/* Timestamps */}
              <div className="flex gap-3 text-xs text-slate-600">
                <span>Created {formatTime(job.created_at)}</span>
                {job.started_at && <span>Started {formatTime(job.started_at)}</span>}
                {job.finished_at && <span>Finished {formatTime(job.finished_at)}</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
