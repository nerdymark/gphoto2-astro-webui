import { useState, useEffect, useRef } from "react";
import * as api from "../api/client";
import { imageUrl } from "../api/client";

function useElapsed(active) {
  const [elapsed, setElapsed] = useState(0);
  const startRef = useRef(null);
  useEffect(() => {
    if (!active) {
      startRef.current = null;
      return;
    }
    startRef.current = Date.now();
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startRef.current) / 1000));
    }, 1000);
    return () => {
      clearInterval(id);
      setElapsed(0);
    };
  }, [active]);
  return elapsed;
}

function formatElapsed(s) {
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}

export default function StackingPanel({ gallery, images, onStackComplete }) {
  const [selected, setSelected] = useState(new Set());
  const [mode, setMode] = useState("mean");
  const [outputName, setOutputName] = useState("");
  const [stacking, setStacking] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const elapsed = useElapsed(stacking);

  // Filter out already-stacked images from selection candidates
  const stackableImages = images.filter((img) => !img.filename.startsWith("stacked-"));

  const toggleImage = (filename) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(filename)) next.delete(filename);
      else next.add(filename);
      return next;
    });
  };

  const selectAll = () => setSelected(new Set(stackableImages.map((i) => i.filename)));
  const clearAll = () => setSelected(new Set());

  const handleStack = async () => {
    if (selected.size < 2) return;
    setStacking(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.stackImages(
        gallery,
        Array.from(selected),
        mode,
        outputName.trim() || undefined
      );
      setResult(res);
      onStackComplete?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setStacking(false);
    }
  };

  if (!gallery) {
    return (
      <div className="rounded-xl bg-slate-800 border border-slate-700 p-4">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300 mb-2">
          Image Stacking
        </h2>
        <p className="text-slate-500 text-sm">Select a gallery first.</p>
      </div>
    );
  }

  return (
    <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-4">
      <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
        Image Stacking
      </h2>

      {stackableImages.length < 2 ? (
        <p className="text-slate-500 text-sm">
          At least 2 images are needed for stacking. Capture more frames first.
        </p>
      ) : (
        <>
          <div className="flex gap-2 items-center">
            <button
              onClick={selectAll}
              className="text-xs text-indigo-400 hover:text-indigo-300"
            >
              Select all
            </button>
            <span className="text-slate-600">·</span>
            <button
              onClick={clearAll}
              className="text-xs text-slate-400 hover:text-slate-300"
            >
              Clear
            </button>
            <span className="text-slate-500 text-xs ml-auto">
              {selected.size} of {stackableImages.length} selected
            </span>
          </div>

          <ul className="grid grid-cols-3 sm:grid-cols-4 lg:grid-cols-5 gap-2 max-h-64 overflow-y-auto">
            {stackableImages.map((img) => (
              <li key={img.filename}>
                <button
                  onClick={() => toggleImage(img.filename)}
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

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-400 uppercase tracking-wider">
                Stacking Mode
              </label>
              <select
                className="rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                value={mode}
                onChange={(e) => setMode(e.target.value)}
                disabled={stacking}
              >
                <option value="mean">Mean (average)</option>
                <option value="median">Median (noise rejection)</option>
                <option value="sum">Sum (bright stars)</option>
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-400 uppercase tracking-wider">
                Output filename (optional)
              </label>
              <input
                className="rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                placeholder="stacked-result.jpg"
                value={outputName}
                onChange={(e) => setOutputName(e.target.value)}
                disabled={stacking}
              />
            </div>
          </div>

          <button
            onClick={handleStack}
            disabled={stacking || selected.size < 2}
            className="w-full rounded-lg bg-purple-600 hover:bg-purple-500 disabled:opacity-50 px-4 py-2.5 text-sm font-semibold text-white transition-colors"
          >
            {stacking
              ? `Stacking… ${formatElapsed(elapsed)}`
              : `Stack ${selected.size} Image${selected.size !== 1 ? "s" : ""}`}
          </button>

          {error && <p className="text-red-400 text-xs">{error}</p>}

          {result && (
            <div>
              <p className="text-green-400 text-xs mb-1">
                ✓ Stacked image saved as <span className="font-mono">{result.filename}</span>
              </p>
              <a
                href={imageUrl(result.gallery, result.filename)}
                target="_blank"
                rel="noopener noreferrer"
              >
                <img
                  src={imageUrl(result.gallery, result.filename)}
                  alt="Stacked result"
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
