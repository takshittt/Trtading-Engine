import type { WatchlistItem, OptionChainData } from '../types'
import { formatINR } from '../utils/format'

interface OptionChainModalProps {
  show: boolean
  chainItem: WatchlistItem | null
  chainData: OptionChainData | null
  chainLoading: boolean
  chainError: string | null
  chainExpiry: string
  onClose: () => void
  onFetchChain: (item: WatchlistItem, expiry: string) => void
  onExpiryChange: (expiry: string) => void
}

export default function OptionChainModal({
  show,
  chainItem,
  chainData,
  chainLoading,
  chainError,
  chainExpiry,
  onClose,
  onFetchChain,
  onExpiryChange,
}: OptionChainModalProps) {
  if (!show || !chainItem) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onClose} />
      <div className="relative w-full max-w-3xl mx-4 rounded-xl overflow-hidden shadow-2xl border border-gray-700 bg-gray-900 max-h-[90vh] flex flex-col">
        <div className="px-5 py-3 border-b border-gray-800 flex items-center justify-between shrink-0">
          <div>
            <span className="font-bold text-white">{chainItem.sym || chainItem.tsym}</span>
            <span className="ml-2 text-xs text-gray-500">Option Chain</span>
            {chainData && (
              <span className="ml-2 text-xs text-gray-500">· {chainData.exchange}</span>
            )}
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300 text-xl leading-none cursor-pointer">×</button>
        </div>

        {chainData && chainData.expiries.length > 0 && (
          <div className="px-5 py-2 flex gap-2 overflow-x-auto shrink-0 border-b border-gray-800">
            {chainData.expiries.map(exp => (
              <button
                key={exp}
                onClick={() => {
                  onExpiryChange(exp)
                  onFetchChain(chainItem, exp)
                }}
                className={`whitespace-nowrap px-3 py-1 text-xs rounded border transition-colors cursor-pointer ${
                  chainExpiry === exp
                    ? 'border-blue-500 bg-blue-600/20 text-blue-300'
                    : 'border-gray-700 text-gray-500 hover:border-gray-500 hover:text-gray-300'
                }`}
              >
                {exp}
              </button>
            ))}
          </div>
        )}

        <div className="overflow-y-auto flex-1 p-4">
          {chainLoading ? (
            <div className="flex items-center justify-center py-16 text-gray-500 text-sm">Loading option chain…</div>
          ) : chainError ? (
            <div className="text-red-400 text-sm text-center py-8">{chainError}</div>
          ) : chainData && chainData.chain.length > 0 ? (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-600 border-b border-gray-800">
                  <th className="pb-2 text-right font-medium text-blue-500/70 pr-4">OI</th>
                  <th className="pb-2 text-right font-medium text-blue-500/70 pr-4">Vol</th>
                  <th className="pb-2 text-right font-medium text-blue-400 pr-6">CE LTP</th>
                  <th className="pb-2 text-center font-medium text-gray-400 px-4">Strike</th>
                  <th className="pb-2 text-left font-medium text-red-400 pl-6">PE LTP</th>
                  <th className="pb-2 text-left font-medium text-red-500/70 pl-4">Vol</th>
                  <th className="pb-2 text-left font-medium text-red-500/70 pl-4">OI</th>
                </tr>
              </thead>
              <tbody>
                {chainData.chain.map(row => {
                  const ltp = parseFloat(chainItem.ltp) || 0
                  const isAtm = ltp > 0 && Math.abs(row.strike - ltp) === Math.min(
                    ...chainData.chain.map(r => Math.abs(r.strike - ltp))
                  )
                  return (
                    <tr key={row.strike} className={`border-b border-gray-800/40 ${isAtm ? 'bg-blue-950/30' : ''}`}>
                      <td className="py-1.5 text-right text-gray-500 pr-4 font-mono">{row.CE?.oi || '—'}</td>
                      <td className="py-1.5 text-right text-gray-500 pr-4 font-mono">{row.CE?.v || '—'}</td>
                      <td className="py-1.5 text-right text-blue-300 font-mono font-semibold pr-6">
                        {row.CE?.lp ? `₹${formatINR(parseFloat(row.CE.lp))}` : '—'}
                      </td>
                      <td className={`py-1.5 text-center font-mono font-bold px-4 ${isAtm ? 'text-yellow-400' : 'text-gray-300'}`}>
                        {row.strike.toLocaleString('en-IN')}
                      </td>
                      <td className="py-1.5 text-left text-red-300 font-mono font-semibold pl-6">
                        {row.PE?.lp ? `₹${formatINR(parseFloat(row.PE.lp))}` : '—'}
                      </td>
                      <td className="py-1.5 text-left text-gray-500 pl-4 font-mono">{row.PE?.v || '—'}</td>
                      <td className="py-1.5 text-left text-gray-500 pl-4 font-mono">{row.PE?.oi || '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          ) : chainData ? (
            <div className="text-gray-500 text-sm text-center py-8">
              No options found for {chainItem.sym || chainItem.tsym}
              {chainData.exchange !== chainItem.exch && (
                <span className="block mt-1 text-xs text-gray-600">
                  Searched on {chainData.exchange} (mapped from {chainItem.exch})
                </span>
              )}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
