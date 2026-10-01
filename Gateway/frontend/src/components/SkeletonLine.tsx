export default function SkeletonLine({ width }: { width: number }) {
  return <div className="h-3 bg-gray-800 rounded animate-pulse" style={{ width }} />
}
