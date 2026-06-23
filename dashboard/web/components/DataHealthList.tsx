interface DataSource {
  ok: boolean;
  last_ok?: string;
  error?: string;
}

interface DataHealthListProps {
  sources: Record<string, DataSource>;
}

export default function DataHealthList({ sources }: DataHealthListProps) {
  const entries = Object.entries(sources);

  if (entries.length === 0) {
    return (
      <p className="text-sm text-gray-500 py-4 text-center">No sources configured</p>
    );
  }

  return (
    <div className="divide-y divide-surface-border">
      {entries.map(([name, src]) => (
        <div key={name} className="flex items-center justify-between py-3 gap-3">
          <div className="flex items-center gap-3">
            <span
              className={`w-2 h-2 rounded-full ${
                src.ok ? 'bg-success' : 'bg-danger'
              }`}
            />
            <span className="text-sm text-white font-medium">{name}</span>
          </div>
          <div className="text-right">
            {src.ok ? (
              <span className="text-xs text-gray-500">
                {src.last_ok ? new Date(src.last_ok).toLocaleTimeString() : 'OK'}
              </span>
            ) : (
              <span className="text-xs text-danger truncate max-w-[160px]">
                {src.error ?? 'Error'}
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
