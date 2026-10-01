// Shared broker credential field definitions.
// Every field here that is NOT explicitly optional MUST match backend
// REQUIRED_FIELDS (app/routers/credentials.py). Fields marked "(optional)" in
// their label (e.g. shoonya's `proxy`) are accepted and stored but not required.

export type BrokerName = 'shoonya'

export interface FieldDef {
  key: string
  label: string
  type?: string
}

export const BROKER_FIELDS: Record<BrokerName, FieldDef[]> = {
  shoonya: [
    { key: 'user_id', label: 'User ID' },
    { key: 'password', label: 'Password', type: 'password' },
    { key: 'totp_secret', label: 'TOTP secret', type: 'password' },
    { key: 'vendor_code', label: 'Vendor code' },
    { key: 'api_secret', label: 'API secret', type: 'password' },
    { key: 'imei', label: 'IMEI' },
    { key: 'proxy', label: 'Proxy (optional, http://user:pass@host:port)', type: 'password' },
    { key: 'proxy_mode', label: 'Always route orders via proxy (skip the direct attempt)', type: 'checkbox' },
  ],
}

export function isBrokerName(v: string): v is BrokerName {
  return v === 'shoonya'
}
