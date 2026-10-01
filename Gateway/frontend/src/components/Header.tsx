import type { UserInfo, FundsInfo } from '../types'
import Badge from './Badge'
import InfoRow from './InfoRow'
import SkeletonLine from './SkeletonLine'

interface HeaderProps {
  connected: boolean | null
  connecting: boolean
  user: UserInfo | null
  funds: FundsInfo | null
  niftyData: { ltp: string; close: string } | null
  wsLive: boolean
  subscribedCount: number | null
  loadingData: boolean
  showUserDropdown: boolean
  onOpenSubscribed: () => void
  onConnect: () => void
  onDisconnect: () => void
  onToggleUserDropdown: () => void
  onCloseUserDropdown: () => void
  onOpenSettings: () => void
  accountEmail: string | null
  onSignOut: () => void
}

export default function Header({
  connected,
  connecting,
  user,
  funds,
  niftyData,
  wsLive,
  subscribedCount,
  loadingData,
  showUserDropdown,
  onOpenSubscribed,
  onConnect,
  onDisconnect,
  onToggleUserDropdown,
  onCloseUserDropdown,
  onOpenSettings,
  accountEmail,
  onSignOut,
}: HeaderProps) {
  return (
    <header className="border-b border-gray-800 px-6 py-3">
      <div className="flex items-center justify-between gap-6">
        <div className="flex items-center gap-4 shrink-0">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center font-bold text-sm select-none">
              G
            </div>
            <span className="font-semibold tracking-tight">Gateway</span>
          </div>

          {niftyData && (() => {
            const ltp = parseFloat(niftyData.ltp)
            const prevClose = parseFloat(niftyData.close)
            const chgPts = ltp && prevClose ? ltp - prevClose : null
            const chgPct = chgPts !== null && prevClose ? (chgPts / prevClose) * 100 : null
            const isPos = chgPts !== null ? chgPts >= 0 : null
            return (
              <div className="flex items-center gap-3 px-3 py-1.5 rounded border border-gray-700/50">
                <span className="text-xs font-semibold text-gray-500 uppercase tracking-wide shrink-0">NIFTY 50</span>
                <span className="font-mono text-sm font-semibold text-white">
                  ₹{ltp ? ltp.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—'}
                </span>
                {chgPts !== null && chgPct !== null && (
                  <>
                    <span className={`font-mono text-sm font-semibold ${isPos ? 'text-green-400' : 'text-red-400'}`}>
                      {isPos ? '+' : ''}{chgPts.toFixed(2)}
                    </span>
                    <span className={`font-mono text-sm ${isPos ? 'text-green-400' : 'text-red-400'}`}>
                      ({isPos ? '+' : ''}{chgPct.toFixed(2)}%)
                    </span>
                  </>
                )}
              </div>
            )
          })()}

          {connected && subscribedCount !== null && (
            <button
              onClick={onOpenSubscribed}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-gray-700/50 hover:border-gray-500 transition-colors cursor-pointer"
              title="View live data for every subscribed token"
            >
              <span className="text-xs text-gray-500">Subscribed</span>
              <span className="text-sm font-semibold text-gray-100 font-mono">{subscribedCount}</span>
            </button>
          )}

          {connected && !wsLive && (
            <span className="flex items-center gap-1.5 px-2 py-1 rounded border border-amber-700/50 text-[10px] font-semibold uppercase tracking-wide text-amber-400">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
              Feed stale — reconnecting
            </span>
          )}
        </div>

        {connected && funds && (
          <div className="flex items-center gap-6 flex-1 justify-center">
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-gray-500">Balance</span>
              <span className="text-sm font-semibold text-gray-100 font-mono">₹{funds.cash.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
            </div>
            <div className="w-px h-4 bg-gray-700" />
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-gray-500">Margin Used</span>
              <span className="text-sm font-semibold text-amber-400 font-mono">₹{funds.margin_used.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
            </div>
            <div className="w-px h-4 bg-gray-700" />
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-gray-500">Available</span>
              <span className="text-sm font-semibold text-green-400 font-mono">₹{(funds.cash - funds.margin_used).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
            </div>
            {funds.payin > 0 && (
              <>
                <div className="w-px h-4 bg-gray-700" />
                <div className="flex items-center gap-1.5">
                  <span className="text-xs text-gray-500">Pay-in</span>
                  <span className="text-sm font-semibold text-blue-400 font-mono">₹{funds.payin.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                </div>
              </>
            )}
          </div>
        )}

        <div className="flex items-center gap-4 shrink-0">
          <button
            onClick={onOpenSettings}
            className="p-1.5 rounded-lg text-gray-500 hover:text-gray-200 hover:bg-gray-800 transition-colors cursor-pointer"
            title="Market hours settings"
            aria-label="Settings"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={1.7} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.324.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
            </svg>
          </button>
          {connected === null ? (
            <span className="text-sm text-gray-500">Checking connection…</span>
          ) : connected ? (
            <>
              <span className="flex items-center gap-2 text-sm text-emerald-400">
                <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
                Connected to Shoonya
              </span>
<button
                onClick={onDisconnect}
                className="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 transition-colors cursor-pointer"
              >
                Disconnect
              </button>
              <div className="relative">
                <button
                  onClick={onToggleUserDropdown}
                  className="flex items-center gap-2 px-2 py-1 rounded-lg hover:bg-gray-800 transition-colors cursor-pointer"
                  title="User Profile"
                >
                  {user && (
                    <span className="text-sm font-medium text-gray-300">{user.uid}</span>
                  )}
                  <div className="w-8 h-8 rounded-full bg-blue-700 flex items-center justify-center text-sm font-bold text-white hover:bg-blue-600 transition-colors select-none shrink-0">
                    {user ? user.uid.charAt(0).toUpperCase() : (
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 6a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0ZM4.501 20.118a7.5 7.5 0 0 1 14.998 0A17.933 17.933 0 0 1 12 21.75c-2.676 0-5.216-.584-7.499-1.632Z" />
                      </svg>
                    )}
                  </div>
                </button>

                {showUserDropdown && (
                  <>
                    <div className="fixed inset-0 z-40" onClick={onCloseUserDropdown} />
                    <div className="absolute right-0 top-10 z-50 w-80 bg-gray-900 border border-gray-700 rounded-xl shadow-2xl overflow-hidden">
                      <div className="px-4 py-3 border-b border-gray-800 flex items-center justify-between">
                        <span className="text-xs font-semibold text-gray-500 uppercase tracking-widest">User Profile</span>
                        <button onClick={onCloseUserDropdown} className="text-gray-600 hover:text-gray-300 text-lg leading-none cursor-pointer">×</button>
                      </div>
                      <div className="p-4 space-y-4 max-h-[80vh] overflow-y-auto">
                        {loadingData && !user ? (
                          <div className="space-y-4">
                            <div className="flex items-center gap-3">
                              <div className="w-10 h-10 rounded-full bg-gray-800 animate-pulse shrink-0" />
                              <div className="space-y-2">
                                <SkeletonLine width={100} />
                                <SkeletonLine width={70} />
                              </div>
                            </div>
                            {[100, 120, 80, 100].map((w, i) => (
                              <div key={i} className="flex justify-between">
                                <SkeletonLine width={50} />
                                <SkeletonLine width={w} />
                              </div>
                            ))}
                          </div>
                        ) : user ? (
                          <>
                            <div className="flex items-center gap-3">
                              <div className="w-10 h-10 rounded-full bg-blue-700 flex items-center justify-center text-lg font-bold select-none shrink-0">
                                {user.uid.charAt(0).toUpperCase()}
                              </div>
                              <div>
                                <div className="font-semibold text-white">{user.uid}</div>
                                <div className="text-xs text-gray-500">Account: {user.actid}</div>
                              </div>
                              <span className="ml-auto text-xs px-2 py-1 rounded-full bg-emerald-950 border border-emerald-800 text-emerald-300">
                                {user.uprev}
                              </span>
                            </div>

                            <div className="grid grid-cols-1 gap-2 pt-1 border-t border-gray-800">
                              <InfoRow label="Email" value={user.email} />
                              <InfoRow label="Mobile" value={user.m_num} />
                              <InfoRow label="Broker" value={user.brkname} />
                              <InfoRow label="Branch" value={user.brnchid} />
                            </div>

                            {user.exarr.length > 0 && (
                              <div className="space-y-1.5 pt-1 border-t border-gray-800">
                                <span className="text-xs text-gray-500">Enabled Exchanges</span>
                                <div className="flex flex-wrap gap-1.5">
                                  {user.exarr.map(ex => <Badge key={ex} label={ex} variant="green" />)}
                                </div>
                              </div>
                            )}

                            {user.orarr.length > 0 && (
                              <div className="space-y-1.5 border-t border-gray-800 pt-3">
                                <span className="text-xs text-gray-500">Allowed Order Types</span>
                                <div className="flex flex-wrap gap-1.5">
                                  {user.orarr.map(o => <Badge key={o} label={o} variant="blue" />)}
                                </div>
                              </div>
                            )}

                            {user.prarr.length > 0 && (
                              <div className="space-y-2 border-t border-gray-800 pt-3">
                                <span className="text-xs text-gray-500">Enabled Products</span>
                                <table className="w-full text-sm">
                                  <thead>
                                    <tr className="text-left text-xs text-gray-600 border-b border-gray-800">
                                      <th className="pb-2 pr-4 font-medium">Code</th>
                                      <th className="pb-2 pr-4 font-medium">Name</th>
                                      <th className="pb-2 font-medium">Exchanges</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {user.prarr.map(p => (
                                      <tr key={p.prd} className="border-b border-gray-800/40">
                                        <td className="py-1.5 pr-4 font-mono text-gray-300 text-xs">{p.prd}</td>
                                        <td className="py-1.5 pr-4 text-gray-400 text-xs">{p.s_prdt_ali}</td>
                                        <td className="py-1.5">
                                          <div className="flex flex-wrap gap-1">
                                            {p.exch.map(ex => <Badge key={ex} label={ex} variant="default" />)}
                                          </div>
                                        </td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            )}

                            {user.request_time && (
                              <p className="text-xs text-gray-700 text-right border-t border-gray-800 pt-2">
                                Last updated: {user.request_time}
                              </p>
                            )}
                          </>
                        ) : null}
                      </div>
                    </div>
                  </>
                )}
              </div>
            </>
          ) : (
            <>
              <span className="flex items-center gap-2 text-sm text-red-400">
                <span className="w-2 h-2 rounded-full bg-red-400" />
                Disconnected
              </span>
              {connecting && (
                <span className="text-xs text-gray-500">may take up to 30s</span>
              )}
              <button
                onClick={onConnect}
                disabled={connecting}
                className="text-xs px-4 py-1.5 rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium transition-colors cursor-pointer"
              >
                {connecting ? 'Connecting…' : 'Connect'}
              </button>
            </>
          )}

          <div className="w-px h-5 bg-gray-800" />
          <div className="flex items-center gap-3">
            {accountEmail && (
              <span className="text-xs text-gray-500 max-w-[180px] truncate" title={accountEmail}>
                {accountEmail}
              </span>
            )}
            <button
              onClick={onSignOut}
              className="text-xs px-3 py-1.5 rounded border border-gray-700 text-gray-400 hover:border-gray-500 hover:text-gray-200 transition-colors cursor-pointer"
              title="Sign out of the app"
            >
              Sign out
            </button>
          </div>
        </div>
      </div>
    </header>
  )
}
