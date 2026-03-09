import { useState } from "react";
import * as api from "../api/client";
import { imageUrl } from "../api/client";

export default function CapturePanel({ gallery, onCapture }) {
  const [capturing, setCapturing] = useState(false);
  const [lastCapture, setLastCapture] = useState(null);
  const [error, setError] = useState(null);
  const [burstCount, setBurstCount] = useState(1);
  const [burstInterval, setBurstInterval] = useState(5);
  const [progress, setProgress] = useState(null);

  const doCapture = async () => {
    if (!gallery) return;
    setCapturing(true);
    setError(null);
    setProgress(null);
    try {
      if (burstCount <= 1) {
        const result = await api.captureImage(gallery);
        setLastCapture(result);
        onCapture?.();
      } else {
        setProgress({ captured: 0, total: burstCount });
        const result = await api.captureBurst(gallery, burstCount, burstInterval);
        setProgress({ captured: result.captured, total: burstCount });
        if (result.files && result.files.length > 0) {
          const last = result.files[result.files.length - 1];
          setLastCapture({ gallery, filename: last.filename });
        }
        onCapture?.();
        if (result.captured < burstCount) {
          setError(`Only ${result.captured} of ${burstCount} frames captured`);
        }
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setCapturing(false);
      setProgress(null);
    }
  };

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
                max={99}
                value={burstCount}
                onChange={(e) => setBurstCount(Math.max(1, parseInt(e.target.value) || 1))}
                className="rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500 w-24"
              />
            </div>
            {burstCount > 1 && (
              <div className="flex flex-col gap-1">
                <label className="text-xs text-slate-400 uppercase tracking-wider">
                  Interval (s)
                </label>
                <input
                  type="number"
                  min={1}
                  max={600}
                  value={burstInterval}
                  onChange={(e) => setBurstInterval(Math.max(1, parseInt(e.target.value) || 1))}
                  className="rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500 w-24"
                />
              </div>
            )}
          </div>

          <button
            onClick={doCapture}
            disabled={capturing}
            className="w-full rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 px-4 py-3 text-sm font-semibold text-white transition-colors"
          >
            {capturing
              ? progress
                ? `Capturing burst (${progress.captured}/${progress.total})…`
                : "Capturing…"
              : burstCount > 1
              ? `Capture ${burstCount} Frames`
              : "Capture Image"}
          </button>

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
