import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { LoginGate } from './LoginGate'
import './index.css'

// Dark-only, Gateway-style. Set before first paint so nothing flashes light.
document.documentElement.setAttribute('data-theme', 'dark')

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <LoginGate>
      <App />
    </LoginGate>
  </React.StrictMode>,
)
