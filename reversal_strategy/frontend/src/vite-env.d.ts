/// <reference types="vite/client" />

// The dashboard talks only to its own backend: same origin by default (via the
// Vite proxy in dev), or VITE_API_BASE when hosted apart from it. Login is proxied
// through Swing's /api/auth/login, so the browser never needs the Gateway's URL.
