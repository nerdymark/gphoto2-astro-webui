export default function StatusBadge({ connected, shootingMode, focusMode, battery }) {
  return (
    <div className="flex items-center gap-2">
      {connected && shootingMode && (
        <span className="inline-flex items-center rounded-full bg-indigo-900 text-indigo-300 px-2.5 py-1 text-xs font-medium">
          {shootingMode}
        </span>
      )}
      {connected && focusMode && (
        <span className="inline-flex items-center rounded-full bg-amber-900 text-amber-300 px-2.5 py-1 text-xs font-medium">
          {focusMode}
        </span>
      )}
      {connected && battery && (
        <span className="inline-flex items-center rounded-full bg-slate-700 text-slate-300 px-2.5 py-1 text-xs font-medium">
          {battery}
        </span>
      )}
      <span
        className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${
          connected
            ? "bg-green-900 text-green-300"
            : "bg-red-900 text-red-300"
        }`}
      >
        <span
          className={`h-2 w-2 rounded-full ${connected ? "bg-green-400" : "bg-red-400"}`}
        />
        {connected ? "Camera connected" : "No camera"}
      </span>
    </div>
  );
}
