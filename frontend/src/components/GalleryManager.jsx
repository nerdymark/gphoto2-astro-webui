import { useState } from "react";
import * as api from "../api/client";

export default function GalleryManager({ onGallerySelect, selectedGallery, galleries, onRefresh }) {
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState(null);

  const handleCreate = async (e) => {
    e.preventDefault();
    if (!newName.trim()) return;
    setCreating(true);
    setError(null);
    try {
      const result = await api.createGallery(newName.trim());
      setNewName("");
      onRefresh();
      onGallerySelect(result.name);
    } catch (err) {
      setError(err.message);
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-4">
      <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
        Galleries
      </h2>

      <form onSubmit={handleCreate} className="flex gap-2">
        <input
          className="flex-1 rounded-lg bg-slate-700 border border-slate-600 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-indigo-500"
          placeholder="New gallery name…"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          disabled={creating}
        />
        <button
          type="submit"
          disabled={creating || !newName.trim()}
          className="rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 px-4 py-2 text-sm font-medium text-white transition-colors"
        >
          {creating ? "Creating…" : "Create"}
        </button>
      </form>

      {error && <p className="text-red-400 text-xs">{error}</p>}

      {galleries.length === 0 ? (
        <p className="text-slate-500 text-sm">No galleries yet. Create one above.</p>
      ) : (
        <ul className="space-y-1 max-h-64 overflow-y-auto">
          {galleries.map((g) => (
            <li key={g.name}>
              <button
                onClick={() => onGallerySelect(g.name)}
                className={`w-full text-left rounded-lg px-3 py-2 text-sm transition-colors flex items-center justify-between ${
                  selectedGallery === g.name
                    ? "bg-indigo-700 text-white"
                    : "hover:bg-slate-700 text-slate-300"
                }`}
              >
                <span className="font-medium truncate">{g.name}</span>
                <span className="text-xs text-slate-400 ml-2 shrink-0">
                  {g.image_count} img{g.image_count !== 1 ? "s" : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
