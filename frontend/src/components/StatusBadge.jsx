export default function StatusBadge({ connected }) {
  return (
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
  );
}
