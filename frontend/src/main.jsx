import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import { themeCss } from './theme'

const style = document.createElement('style')
style.textContent = themeCss
document.head.appendChild(style)

// Restore a theme picked with the toggle before the first render, so the page doesn't flash.
try {
  const saved = localStorage.getItem('orbitops_theme')
  if (saved === 'light' || saved === 'dark') document.documentElement.dataset.theme = saved
} catch {
  // Storage blocked: just follow the system setting.
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
