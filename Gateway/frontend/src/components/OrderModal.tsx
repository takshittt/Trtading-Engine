import type { WatchlistItem } from '../types'

interface OrderModalProps {
  show: boolean
  orderItem: WatchlistItem | null
  orderSide: 'B' | 'S'
  orderQty: number
  orderLots: number
  orderPrice: number
  orderType: 'LMT' | 'MKT'
  orderProduct: 'I' | 'M'
  placingOrder: boolean
  holdForever: boolean
  tpEnabled: boolean
  tpValue: number
  description: string
  onClose: () => void
  onPlaceOrder: () => void
  onQtyChange: (qty: number) => void
  onLotsChange: (lots: number) => void
  onPriceChange: (price: number) => void
  onTypeChange: (type: 'LMT' | 'MKT') => void
  onProductChange: (product: 'I' | 'M') => void
  onHoldForeverChange: (v: boolean) => void
  onTpEnabledChange: (v: boolean) => void
  onTpValueChange: (v: number) => void
  onDescriptionChange: (v: string) => void
}

export default function OrderModal({
  show,
  orderItem,
  orderSide,
  orderQty,
  orderLots,
  orderPrice,
  orderType,
  orderProduct,
  placingOrder,
  onClose,
  onPlaceOrder,
  onQtyChange,
  onLotsChange,
  onPriceChange,
  onTypeChange,
  onProductChange,
  holdForever,
  tpEnabled,
  tpValue,
  description,
  onHoldForeverChange,
  onTpEnabledChange,
  onTpValueChange,
  onDescriptionChange,
}: OrderModalProps) {
  if (!show || !orderItem) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm" onClick={onClose} />

      <div className="relative w-full max-w-sm mx-4 rounded-xl overflow-hidden shadow-2xl border border-gray-700">
        <div className={`px-5 py-4 relative ${orderSide === 'B' ? 'bg-green-700' : 'bg-red-700'}`}>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="absolute top-2 right-2 w-7 h-7 flex items-center justify-center rounded-md text-white/80 hover:text-white hover:bg-white/15 transition"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M3 3l10 10M13 3L3 13" />
            </svg>
          </button>
          <div className="font-bold text-white text-base tracking-wide">
            {orderSide === 'B' ? 'BUY' : 'SELL'} {orderItem.tsym}
          </div>
          <div className="text-xs text-white/70 mt-0.5">{orderItem.exch} · {orderItem.instrumenttype}</div>
        </div>

        <div className="bg-gray-900 p-5 space-y-4">
          <div className="flex items-center gap-4">
            <label className="text-sm text-gray-400 w-16 shrink-0">Lots</label>
            <input
              type="number" min="1"
              value={orderLots}
              onChange={(e) => {
                const lots = parseInt(e.target.value) || 1
                onLotsChange(lots)
                onQtyChange(lots * (parseInt(orderItem.lotsize ?? '1') || 1))
              }}
              className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-right font-mono text-white focus:outline-none focus:border-blue-500"
            />
          </div>
          <div className="flex items-center gap-4">
            <label className="text-sm text-gray-400 w-16 shrink-0">Qty</label>
            <input
              type="number" min="1"
              value={orderQty}
              onChange={(e) => {
                const qty = parseInt(e.target.value) || 1
                onQtyChange(qty)
                const ls = parseInt(orderItem.lotsize ?? '1') || 1
                onLotsChange(Math.round(qty / ls * 100) / 100)
              }}
              className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-right font-mono text-white focus:outline-none focus:border-blue-500"
            />
          </div>

          {orderType === 'LMT' && (
            <div className="flex items-center gap-4">
              <label className="text-sm text-gray-400 w-16 shrink-0">Price</label>
              <input
                type="number" step="0.05" min="0"
                value={orderPrice}
                onChange={(e) => onPriceChange(parseFloat(e.target.value) || 0)}
                className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-right font-mono text-white focus:outline-none focus:border-blue-500"
              />
            </div>
          )}

          <div className="flex items-center gap-4">
            <label className="text-sm text-gray-400 w-16 shrink-0">Type</label>
            <div className="flex gap-1 flex-1">
              {(['LMT', 'MKT'] as const).map(t => (
                <button
                  key={t} type="button"
                  onClick={() => onTypeChange(t)}
                  className={`flex-1 py-1.5 text-xs font-semibold rounded border transition-colors cursor-pointer ${
                    orderType === t
                      ? 'border-blue-500 bg-blue-600/20 text-blue-300'
                      : 'border-gray-700 text-gray-500 hover:border-gray-600 hover:text-gray-300'
                  }`}
                >
                  {t === 'LMT' ? 'LIMIT' : 'MARKET'}
                </button>
              ))}
            </div>
          </div>

          <div className="flex items-center gap-4">
            <label className="text-sm text-gray-400 w-16 shrink-0">Product</label>
            <div className="flex gap-1 flex-1">
              {([{ code: 'I', label: 'MIS' }, { code: 'M', label: 'NRML' }] as const).map(({ code, label }) => (
                <button
                  key={code} type="button"
                  onClick={() => onProductChange(code)}
                  className={`flex-1 py-1.5 text-xs font-semibold rounded border transition-colors cursor-pointer ${
                    orderProduct === code
                      ? 'border-blue-500 bg-blue-600/20 text-blue-300'
                      : 'border-gray-700 text-gray-500 hover:border-gray-600 hover:text-gray-300'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          <div className="flex items-start gap-4">
            <label className="text-sm text-gray-400 w-16 shrink-0 pt-2">Note</label>
            <textarea
              value={description}
              onChange={(e) => onDescriptionChange(e.target.value)}
              rows={2}
              maxLength={500}
              placeholder="Optional description (e.g. thesis, trade setup)"
              className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-600 focus:outline-none focus:border-blue-500 resize-none"
            />
          </div>

          <div className="border-t border-gray-800 pt-3 space-y-3">
            <label className="flex items-center gap-3 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={tpEnabled}
                onChange={e => onTpEnabledChange(e.target.checked)}
                className="w-4 h-4 accent-amber-500 cursor-pointer"
              />
              <span className="text-sm text-gray-300">Take-profit target (this order only)</span>
            </label>
            {tpEnabled && (
              <div className="flex items-center gap-4 pl-7">
                <label className="text-xs text-gray-500 w-16 shrink-0">Order P&amp;L ₹</label>
                <input
                  type="number" step="1" min="1"
                  value={tpValue}
                  onChange={e => onTpValueChange(parseFloat(e.target.value) || 0)}
                  className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-right font-mono text-white focus:outline-none focus:border-amber-500"
                  placeholder="e.g. 1000"
                />
              </div>
            )}
            <label className="flex items-center gap-3 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={holdForever}
                onChange={e => onHoldForeverChange(e.target.checked)}
                className="w-4 h-4 accent-blue-500 cursor-pointer"
              />
              <span className="text-sm text-gray-300">Hold until filled or cancelled</span>
            </label>
            {holdForever && (
              <p className="text-xs text-gray-500 pl-7 leading-snug">
                A fresh DAY order is placed each trading session until this fills or you cancel it.
              </p>
            )}
          </div>

          <button
            type="button"
            onClick={onPlaceOrder}
            disabled={placingOrder || (tpEnabled && tpValue <= 0)}
            className={`w-full py-3 mt-1 rounded-lg font-bold text-white text-sm tracking-widest transition-colors cursor-pointer disabled:opacity-50 ${
              orderSide === 'B' ? 'bg-green-600 hover:bg-green-500' : 'bg-red-600 hover:bg-red-500'
            }`}
          >
            {placingOrder ? 'PLACING...' : `${orderSide === 'B' ? 'BUY' : 'SELL'} ${orderItem.tsym}`}
          </button>
        </div>
      </div>
    </div>
  )
}
