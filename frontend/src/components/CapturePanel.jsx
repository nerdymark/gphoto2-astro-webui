import { useState } from "react";
import * as api from "../api/client";
import { imageUrl } from "../api/client";
import { useJob } from "../hooks/useJob";

export default function CapturePanel({ gallery, onCapture }) {
  const [capturing, setCapturing] = useState(false);
  const [lastCapture, setLastCapture] = useState(null);
  const [error, setError] = useState(null);
  const [burstCount, setBurstCount] = useState(1);
  const [burstInterval, setBurstInterval] = useState(5);

  const { job: burstJob, startJob, cancelJob } = useJob({
    onComplete: (data) => {
      setCapturing(false);
      const files = data.result?.files;
      if (files && files.length > 0) {
        const last = files[files.length - 1];
        setLastCapture({ gallery, filename: last.filename });
      }
      const captured = data.result?.captured ?? 0;
      const requested = data.result?.requested ?? burstCount;
      if (captured < requested) {
        setError(`${captured} of ${requested} frames captured`);
      }
      onCapture?.();
    },
    onFail: (data) => {
      setCapturing(false);
      setError(data.error || data.status);
      onCapture?.();
    },
  });

  const doCapture = async () => {
    if (!gallery) return;
    setCapturing(true);
    setError(null);
    try {
      if (burstCount <= 1) {
        const result = await api.captureImage(gallery);
        setLastCapture(result);
        setCapturing(false);
        onCapture?.();
      } else {
        // Burst returns a job ID – poll via useJob.
        const { job_id } = await api.captureBurst(gallery, burstCount, burstInterval);
        startJob(job_id);
      }
    } catch (err) {
      setError(err.message);
      setCapturing(false);
    }
  };

  const handleCancel = async () => {
    await cancelJob();
  };

  const isBurst = burstCount > 1;
  const burstActive = capturing && isBurst && burstJob;

  return (
    <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-4">
      <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
        Capture
      </h2>

      {!gallery ? (
        <p className="text-slate-500 text-sm">Select or create a gallery first.</p>
      ) : (
        <>
          <p className="text-slate-400 text-xs">
            Saving to gallery: <span className="text-indigo-400 font-medium">{gallery}</span>
          </p>

          <div className="grid grid-cols-2 gap-4">
            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-400 uppercase tracking-wider">
                Burst frames
              </label>
              <input
                type="number"
                min={1}
                max={999}
                value={burstCount}
                onChange={(e) => setBurstCount(Math.max(1, parseInt(e.target.value) || 1))}
                disabled={capturing}
                className="rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500 w-24"
              />
            </div>
            {isBurst && (
              <div className="flex flex-col gap-1">
                <label className="text-xs text-slate-400 uppercase tracking-wider">
                  Interval (s)
                </label>
                <input
                  type="number"
                  min={0}
                  max={600}
                  value={burstInterval}
                  onChange={(e) => setBurstInterval(Math.max(0, parseInt(e.target.value) || 0))}
                  disabled={capturing}
                  className="rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500 w-24"
                />
              </div>
            )}
          </div>

          {/* Progress bar for burst */}
          {burstActive && burstJob.total > 0 && (
            <div className="space-y-1">
              <div className="flex justify-between text-xs text-slate-400">
                <span>{burstJob.message}</span>
                <span>{burstJob.progress}/{burstJob.total}</span>
              </div>
              <div className="h-2 bg-slate-700 rounded-full overflow-hidden">
                <div
                  className="h-full bg-emerald-500 rounded-full transition-all duration-300"
                  style={{ width: `${(burstJob.progress / burstJob.total) * 100}%` }}
                />
              </div>
            </div>
          )}

          <div className="flex gap-2">
            <button
              onClick={doCapture}
              disabled={capturing}
              className="flex-1 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 px-4 py-3 text-sm font-semibold text-white transition-colors"
            >
              {capturing
                ? burstActive
                  ? `Capturing ${burstJob.progress}/${burstJob.total}…`
                  : "Capturing…"
                : isBurst
                ? `Capture ${burstCount} Frames`
                : "Capture Image"}
            </button>
            {burstActive && (
              <button
                onClick={handleCancel}
                className="rounded-lg bg-red-700 hover:bg-red-600 px-4 py-3 text-sm font-semibold text-white transition-colors"
              >
                Cancel
              </button>
            )}
          </div>

          {error && <p className="text-red-400 text-xs">{error}</p>}

          {lastCapture && (
            <div className="mt-2">
              <p className="text-xs text-slate-400 mb-1">Last capture:</p>
              <a
                href={imageUrl(lastCapture.gallery, lastCapture.filename)}
                target="_blank"
                rel="noopener noreferrer"
              >
                <img
                  src={imageUrl(lastCapture.gallery, lastCapture.filename)}
                  alt="Last capture"
                  className="rounded-lg max-h-48 object-contain border border-slate-600"
                />
              </a>
            </div>
          )}
        </>
      )}
    </div>
  );
}
