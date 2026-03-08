import { useState } from "react";
import { useExposure } from "../hooks/useCamera";

function SelectOrText({ label, value, choices, onChange, disabled }) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-xs text-slate-400 uppercase tracking-wider">{label}</label>
      {choices && choices.length > 0 ? (
        <select
          className="rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500"
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value || null)}
          disabled={disabled}
        >
          <option value="">— unchanged —</option>
          {choices.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      ) : (
        <input
          className="rounded bg-slate-700 border border-slate-600 px-2 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-indigo-500"
          value={value ?? ""}
          placeholder="e.g. 1/100"
          onChange={(e) => onChange(e.target.value || null)}
          disabled={disabled}
        />
      )}
    </div>
  );
}

export default function ExposureControls() {
  const { exposure, loading, saving, error, save } = useExposure();

  const [aperture, setAperture] = useState(null);
  const [shutter, setShutter] = useState(null);
  const [iso, setIso] = useState(null);

  if (loading) {
    return <p className="text-slate-400 text-sm">Loading exposure settings…</p>;
  }

  const handleSave = async () => {
    await save({
      aperture: aperture ?? exposure?.aperture,
      shutter: shutter ?? exposure?.shutter,
      iso: iso ?? exposure?.iso,
    });
    setAperture(null);
    setShutter(null);
    setIso(null);
  };

  return (
    <div className="rounded-xl bg-slate-800 border border-slate-700 p-4 space-y-4">
      <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
        Exposure Controls
      </h2>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <SelectOrText
          label="Aperture (f/)"
          value={aperture ?? exposure?.aperture}
          choices={exposure?.aperture_choices}
          onChange={setAperture}
          disabled={saving}
        />
        <SelectOrText
          label="Shutter Speed"
          value={shutter ?? exposure?.shutter}
          choices={exposure?.shutter_choices}
          onChange={setShutter}
          disabled={saving}
        />
        <SelectOrText
          label="ISO"
          value={iso ?? exposure?.iso}
          choices={exposure?.iso_choices}
          onChange={setIso}
          disabled={saving}
        />
      </div>

      {error && (
        <p className="text-red-400 text-xs">{error}</p>
      )}

      <button
        onClick={handleSave}
        disabled={saving}
        className="rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 px-4 py-2 text-sm font-medium text-white transition-colors"
      >
        {saving ? "Applying…" : "Apply Settings"}
      </button>
    </div>
  );
}
