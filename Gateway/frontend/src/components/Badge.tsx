export default function Badge({ label, variant = 'default' }: { label: string; variant?: 'default' | 'blue' | 'green' }) {
  const colors = {
    default: 'bg-gray-800 text-gray-300 border-gray-700',
    blue: 'bg-blue-950 text-blue-300 border-blue-800',
    green: 'bg-emerald-950 text-emerald-300 border-emerald-800',
  }
  return (
    <span className={`inline-block text-xs px-2 py-0.5 rounded border font-mono ${colors[variant]}`}>
      {label}
    </span>
  )
}
