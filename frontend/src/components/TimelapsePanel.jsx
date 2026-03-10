import { useState } from "react";
import * as api from "../api/client";
import { imageUrl } from "../api/client";

const RESOLUTIONS = [
  { label: "1080p (1920x1080)", value: "1920x1080" },
  { label: "720p (1280x720)", value: "1280x720" },
  { label: "4K (3840x2160)", value: "3840x2160" },
];

const FPS_OPTIONS = [24, 30, 60];

export default function TimelapsePanel({ gallery, images, onComplete }) {
  const [selected, setSelected] = useState(new Set());
  const [fps, setFps] = useState(30);
  const [resolution, setResolution] = useState("1920x1080");
  const [outputName, setOutputName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(null);
  const [error, setError] = useState(null);

  // Filter to image files only (exclude videos)
  const timelapseImages = images.filter(
    (img) => !img.filename.endsWith(".mp4") && !img.filename.endsWith(".webm")
  );

  const toggleImage = (filename) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(filename)) next.delete(filename);
      else next.add(filename);
      return next;
    });
  };

  const selectAll = () => setSelected(new Set(timelapseImages.map((i) => i.filename)));
  const clearAll = () => setSelected(new Set());

  const handleGenerate = async () => {
    if (selected.size < 2) return;
    setSubmitting(true);
    setError(null);
    setSubmitted(null);
    try {
      const { job_id } = await api.createTimelapse(
        gallery,
        Array.from(selected),
        fps,
        resolution,
        outputName.trim() || undefined
      );
      setSubmitted(job_id);
      onComplete?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  // Estimate video duration
  const durationSec = selected.size > 0 ? (selected.size / fps).toFixed(1) : 0;

  if (!gallery) {
    return (
      <div className="rounded-xl bg-slate-800 border border-slate-700 p-4">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300 mb-2">
          Timelapse
        </h2>
        <p className="text-slate-500 text-sm">Select a gallery first.</p>
      </div>
    );
  }

  return (
    <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-4">
      <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
        Timelapse Video
      </h2>

      {timelapseImages.length < 2 ? (
        <p className="text-slate-500 text-sm">
          At least 2 images are needed for a timelapse. Capture more frames first.
        </p>
      ) : (
        <>
          <div className="flex gap-2 items-center">
            <button
              onClick={selectAll}
              disabled={submitting}
              className="text-xs text-indigo-400 hover:text-indigo-300"
            >
              Select all
            </button>
            <span className="text-slate-600">&middot;</span>
            <button
              onClick={clearAll}
              disabled={submitting}
              className="text-xs text-slate-400 hover:text-slate-300"
            >
              Clear
            </button>
            <span className="text-slate-500 text-xs ml-auto">
              {selected.size} of {timelapseImages.length} selected
              {selected.size >= 2 && (
                <span className="text-indigo-400 ml-2">
                  ~{durationSec}s video
                </span>
              )}
            </span>
          </div>

          <ul className="grid grid-cols-3 sm:grid-cols-4 lg:grid-cols-5 gap-2 max-h-64 overflow-y-auto">
            {timelapseImages.map((img) => (
              <li key={img.filename}>
                <button
                  onClick={() => toggleImage(img.filename)}
                  disabled={submitting}
                  className={`relative w-full aspect-square rounded overflow-hidden border-2 transition-all ${
                    selected.has(img.filename)
                      ? "border-indigo-500 opacity-100"
                      : "border-slate-600 opacity-60 hover:opacity-80"
                  }`}
                >
                  <img
                    src={imageUrl(gallery, img.filename)}
                    alt={img.filename}
                    className="w-full h-full object-cover"
                  />
                  {selected.has(img.filename) && (
                    <div className="absolute top-1 right-1 bg-indigo-600 rounded-full w-4 h-4 flex items-center justify-center">
                      <svg className="w-3 h-3 text-white" fill="currentColor" viewBox="0 0 20 20">
                        <path
                          fillRule="evenodd"
                          d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
                          clipRule="evenodd"
                        />
                      </svg>
                    </div>
                  )}
                </button>
                <p className="text-xs text-slate-500 truncate mt-0.5">{img.filename}</p>
              </li>
            ))}
          </ul>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-400 uppercase tracking-wider">
                Frame Rate
              </label>
              <select
                className="rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                value={fps}
                onChange={(e) => setFps(parseInt(e.target.value))}
                disabled={submitting}
              >
                {FPS_OPTIONS.map((f) => (
                  <option key={f} value={f}>{f} fps</option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-400 uppercase tracking-wider">
                Resolution
              </label>
              <select
                className="rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                value={resolution}
                onChange={(e) => setResolution(e.target.value)}
                disabled={submitting}
              >
                {RESOLUTIONS.map((r) => (
                  <option key={r.value} value={r.value}>{r.label}</option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-400 uppercase tracking-wider">
                Output filename (optional)
              </label>
              <input
                className="rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                placeholder="timelapse.mp4"
                value={outputName}
                onChange={(e) => setOutputName(e.target.value)}
                disabled={submitting}
              />
            </div>
          </div>

          <button
            onClick={handleGenerate}
            disabled={submitting || selected.size < 2}
            className="w-full rounded-lg bg-purple-600 hover:bg-purple-500 disabled:opacity-50 px-4 py-2.5 text-sm font-semibold text-white transition-colors"
          >
            {submitting
              ? "Submitting..."
              : `Generate Timelapse (${selected.size} frame${selected.size !== 1 ? "s" : ""})`}
          </button>

          {submitted && (
            <p className="text-blue-400 text-xs">
              Job submitted (id: <span className="font-mono">{submitted}</span>). Check the Jobs tab for progress.
            </p>
          )}

          {error && <p className="text-red-400 text-xs">{error}</p>}
        </>
      )}
    </div>
  );
}
