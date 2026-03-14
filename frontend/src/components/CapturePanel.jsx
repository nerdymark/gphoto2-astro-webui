import { useState } from "react";
import * as api from "../api/client";
import { imageUrl } from "../api/client";

const RESOLUTIONS = [
  { label: "1080p (1920x1080)", value: "1920x1080" },
  { label: "720p (1280x720)", value: "1280x720" },
  { label: "4K (3840x2160)", value: "3840x2160" },
];

const FPS_OPTIONS = [24, 30, 60];

export default function CapturePanel({ gallery, onCapture, remoteStatus }) {
  const [capturing, setCapturing] = useState(false);
  const [lastCapture, setLastCapture] = useState(null);
  const [error, setError] = useState(null);
  const [burstCount, setBurstCount] = useState(1);
  const [burstInterval, setBurstInterval] = useState(5);
  const [submittedJob, setSubmittedJob] = useState(null);

  // Post-processing options
  const [enableStack, setEnableStack] = useState(false);
  const [stackMode, setStackMode] = useState("mean");
  const [enableTimelapse, setEnableTimelapse] = useState(false);
  const [tlFps, setTlFps] = useState(30);
  const [tlResolution, setTlResolution] = useState("1920x1080");
  const [useRemote, setUseRemote] = useState(false);

  const remoteAvailable = remoteStatus?.configured && remoteStatus?.reachable;

  const doCapture = async () => {
    if (!gallery) return;
    setCapturing(true);
    setError(null);
    setSubmittedJob(null);
    try {
      if (burstCount <= 1) {
        const result = await api.captureImage(gallery);
        setLastCapture(result);
        onCapture?.();
      } else {
        const postOpts = {};
        if (enableStack) {
          postOpts.stack = { mode: stackMode };
        }
        if (enableTimelapse) {
          postOpts.timelapse = { fps: tlFps, resolution: tlResolution };
        }
        if (useRemote && remoteAvailable) {
          postOpts.remote = true;
        }
        const { job_id } = await api.captureBurst(gallery, burstCount, burstInterval, null, postOpts);
        setSubmittedJob(job_id);
        onCapture?.();
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setCapturing(false);
    }
  };

  const isBurst = burstCount > 1;
  const hasPostProcessing = isBurst && (enableStack || enableTimelapse);

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

          {/* Post-processing options (visible only for burst) */}
          {isBurst && (
            <div className="rounded-lg bg-slate-750 border border-slate-600 p-3 space-y-3">
              <p className="text-xs text-slate-400 uppercase tracking-wider font-semibold">
                Post-processing
              </p>

              {/* Stack option */}
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input
                  type="checkbox"
                  checked={enableStack}
                  onChange={(e) => setEnableStack(e.target.checked)}
                  disabled={capturing}
                  className="rounded border-slate-600 bg-slate-700 text-purple-500 focus:ring-purple-500"
                />
                <span className="text-slate-300">Stack after capture</span>
              </label>

              {enableStack && (
                <div className="ml-6">
                  <label className="text-xs text-slate-400 uppercase tracking-wider">
                    Stacking Mode
                  </label>
                  <select
                    className="mt-1 w-full rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-purple-500"
                    value={stackMode}
                    onChange={(e) => setStackMode(e.target.value)}
                    disabled={capturing}
                  >
                    <option value="mean">Mean (noise reduction)</option>
                    <option value="max">Max (star trails)</option>
                    <option value="align+mean">Aligned Mean (drift correction)</option>
                  </select>
                </div>
              )}

              {/* Timelapse option */}
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input
                  type="checkbox"
                  checked={enableTimelapse}
                  onChange={(e) => setEnableTimelapse(e.target.checked)}
                  disabled={capturing}
                  className="rounded border-slate-600 bg-slate-700 text-purple-500 focus:ring-purple-500"
                />
                <span className="text-slate-300">Timelapse after capture</span>
              </label>

              {enableTimelapse && (
                <div className="ml-6 grid grid-cols-2 gap-3">
                  <div>
                    <label className="text-xs text-slate-400 uppercase tracking-wider">
                      Frame Rate
                    </label>
                    <select
                      className="mt-1 w-full rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-purple-500"
                      value={tlFps}
                      onChange={(e) => setTlFps(parseInt(e.target.value))}
                      disabled={capturing}
                    >
                      {FPS_OPTIONS.map((f) => (
                        <option key={f} value={f}>{f} fps</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="text-xs text-slate-400 uppercase tracking-wider">
                      Resolution
                    </label>
                    <select
                      className="mt-1 w-full rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-purple-500"
                      value={tlResolution}
                      onChange={(e) => setTlResolution(e.target.value)}
                      disabled={capturing}
                    >
                      {RESOLUTIONS.map((r) => (
                        <option key={r.value} value={r.value}>{r.label}</option>
                      ))}
                    </select>
                  </div>
                </div>
              )}

              {/* Remote option */}
              {remoteAvailable && (enableStack || enableTimelapse) && (
                <label className="flex items-center gap-2 text-sm cursor-pointer">
                  <input
                    type="checkbox"
                    checked={useRemote}
                    onChange={(e) => setUseRemote(e.target.checked)}
                    disabled={capturing}
                    className="rounded border-slate-600 bg-slate-700 text-indigo-500 focus:ring-indigo-500"
                  />
                  <span className="text-slate-300">
                    Process on remote server
                  </span>
                  {remoteStatus?.cuda && (
                    <span className="text-xs text-green-400 font-medium px-1.5 py-0.5 bg-green-900/30 rounded">
                      CUDA
                    </span>
                  )}
                  <span className="text-xs text-blue-400">(streams during capture)</span>
                </label>
              )}
            </div>
          )}

          <button
            onClick={doCapture}
            disabled={capturing}
            className="w-full rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 px-4 py-3 text-sm font-semibold text-white transition-colors"
          >
            {capturing
              ? isBurst ? "Submitting burst…" : "Capturing…"
              : isBurst
              ? `Capture ${burstCount} Frames${hasPostProcessing ? " + Process" : ""}`
              : "Capture Image"}
          </button>

          {submittedJob && (
            <p className="text-blue-400 text-xs">
              Burst job submitted (id: <span className="font-mono">{submittedJob}</span>). Check the Jobs tab for progress.
            </p>
          )}

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
