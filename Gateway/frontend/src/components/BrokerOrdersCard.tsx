import { useState } from 'react'
import type { OrderItem } from '../types'
import SkeletonLine from './SkeletonLine'
import { cancelBrokerOrder } from '../api'
import { formatINR } from '../utils/format'
import { usePersistedState } from '../hooks/usePersistedState'

interface BrokerOrdersCardProps {
  orders: OrderItem[]
  loading: boolean
  onRefresh: () => void
}

const CANCELLABLE_STATUSES = new Set(['OPEN', 'PENDING', 'TRIGGER_PENDING'])
const HIDDEN_STATUSES = new Set(['CANCELLED', 'REJECTED'])

export default function BrokerOrdersCard({ orders, loading, onRefresh }: BrokerOrdersCardProps) {
  const [collapsed, setCollapsed] = usePersistedState('brokerOrders.collapsed', false)
  const [showHidden, setShowHidden] = usePersistedState('brokerOrders.showHidden', false)
  const [cancellingId, setCancellingId] = useState<string | null>(null)

  const hiddenCount = orders.filter(o => HIDDEN_STATUSES.has(o.status)).length
  const visibleOrders = showHidden ? orders : orders.filter(o => !HIDDEN_STATUSES.has(o.status))

  const handleCancel = async (orderId: string) => {
    if (!window.confirm('Cancel this order?')) return
    setCancellingId(orderId)
    try {
      await cancelBrokerOrder(orderId)
      onRefresh()
    } catch (e) {
      alert(`Failed to cancel: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setCancellingId(null)
    }
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 space-y-5">
      <div className="flex items-center justify-between">
        <button
          onClick={() => setCollapsed(c => !c)}
          className="flex items-center gap-2 text-xs font-semibold text-gray-500 uppercase tracking-widest hover:text-gray-300 transition-colors cursor-pointer"
        >
          <span className="text-gray-600 text-[10px]">{collapsed ? '▶' : '▼'}</span>
          Orders (from broker)
        </button>
        <div className="flex items-center gap-2">
          {hiddenCount > 0 && (
            <button
              onClick={() => setShowHidden(v => !v)}
              className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-500 hover:border-gray-500 hover:text-gray-300 transition-colors cursor-pointer"
            >
              {showHidden ? `Hide cancelled/rejected` : `Show cancelled/rejected (${hiddenCount})`}
            </button>
          )}
          <button
            onClick={onRefresh}
            disabled={loading}
            className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 disabled:opacity-50 transition-colors cursor-pointer"
          >
            ↻
          </button>
        </div>
      </div>

      {!collapsed && loading && !orders.length ? (
        <div className="space-y-3">
          {[1, 2].map(i => (
            <SkeletonLine key={i} width={300} />
          ))}
        </div>
      ) : !collapsed && visibleOrders.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-base">
            <thead>
              <tr className="text-left text-sm text-gray-600 border-b border-gray-800">
                <th className="pb-2 pr-4 font-medium">Symbol</th>
                <th className="pb-2 pr-4 font-medium">Exch</th>
                <th className="pb-2 pr-4 font-medium">Side</th>
                <th className="pb-2 pr-4 font-medium text-right">Qty</th>
                <th className="pb-2 pr-4 font-medium text-right">Price</th>
                <th className="pb-2 pr-4 font-medium">Type</th>
                <th className="pb-2 pr-4 font-medium">Status</th>
                <th className="pb-2 pr-4 font-medium">Time</th>
                <th className="pb-2 font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {visibleOrders.map((order) => {
                const cancellable = CANCELLABLE_STATUSES.has(order.status)
                const isCancelling = cancellingId === order.norenordno
                return (
                  <tr key={order.norenordno} className="border-b border-gray-800/40">
                    <td className="py-2 pr-4 font-mono text-sm text-gray-300">{order.tsym}</td>
                    <td className="py-2 pr-4 text-xs text-gray-500">{order.exch}</td>
                    <td className="py-2 pr-4">
                      <span className={`text-xs px-2 py-0.5 rounded ${order.trantype === 'B' ? 'bg-blue-950 text-blue-300' : 'bg-red-950 text-red-300'}`}>
                        {order.trantype === 'B' ? 'BUY' : 'SELL'}
                      </span>
                    </td>
                    <td className="py-2 pr-4 text-right font-mono text-gray-300">{order.qty}</td>
                    <td className="py-2 pr-4 text-right font-mono text-gray-400">
                      {parseFloat(order.price) > 0 ? `₹${formatINR(parseFloat(order.price))}` : '—'}
                    </td>
                    <td className="py-2 pr-4 text-xs text-gray-500">{order.pricetype}</td>
                    <td className="py-2 pr-4">
                      <span className={`text-xs px-2 py-0.5 rounded ${
                        order.status === 'COMPLETE' ? 'bg-green-950 text-green-300' :
                        order.status === 'OPEN' ? 'bg-blue-950 text-blue-300' :
                        order.status === 'PENDING' ? 'bg-yellow-950 text-yellow-300' :
                        order.status === 'CANCELLED' ? 'bg-gray-800 text-gray-400' :
                        order.status === 'REJECTED' ? 'bg-red-950 text-red-300' :
                        'bg-gray-800 text-gray-400'
                      }`}>
                        {order.status}
                      </span>
                    </td>
                    <td className="py-2 pr-4 font-mono text-xs text-gray-500">{order.pytime}</td>
                    <td className="py-2 text-right">
                      {cancellable && (
                        <button
                          onClick={() => handleCancel(order.norenordno)}
                          disabled={isCancelling}
                          className="text-xs px-2 py-1 rounded border border-red-900 text-red-400 hover:border-red-700 hover:text-red-300 disabled:opacity-50 transition-colors cursor-pointer"
                        >
                          {isCancelling ? '…' : 'Cancel'}
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : !collapsed ? (
        <p className="text-sm text-gray-500 text-center py-4">
          {orders.length > 0 ? 'No visible orders — all cancelled/rejected' : 'No broker orders'}
        </p>
      ) : null}
    </div>
  )
}
