import { useState } from 'react'
import { colors, fonts } from './theme'
import { signIn, signUp } from './api'

const inputStyle = {
  width: '100%', boxSizing: 'border-box', fontSize: 14.5, padding: '11px 12px', borderRadius: 8,
  border: `1px solid ${colors.border}`, background: colors.white, color: colors.textMain,
  fontFamily: fonts.body, outline: 'none',
}

export default function AuthPage({ onSignedIn }) {
  const [mode, setMode] = useState('signin')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      const user = mode === 'signin' ? await signIn(username.trim(), password) : await signUp(username.trim(), password)
      onSignedIn(user)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const isSignIn = mode === 'signin'

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: colors.bluebg, fontFamily: fonts.body, color: colors.textMain, padding: '24px 16px' }}>
      <form onSubmit={handleSubmit} style={{ width: '100%', maxWidth: 360, background: colors.sidebarBg, border: `1px solid ${colors.border}`, borderRadius: 14, padding: '28px 26px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 22 }}>
          <div style={{ width: 30, height: 30, borderRadius: 8, background: colors.accent, flexShrink: 0 }} />
          <div style={{ fontFamily: fonts.heading, fontWeight: 600, fontSize: 20, color: colors.heading }}>OrbitOps Assistant</div>
        </div>

        <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 16 }}>{isSignIn ? 'Sign in' : 'Create an account'}</div>

        <label style={{ display: 'block', fontSize: 12.5, color: colors.textSecondary, marginBottom: 6 }}>Username</label>
        <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus style={{ ...inputStyle, marginBottom: 14 }} />

        <label style={{ display: 'block', fontSize: 12.5, color: colors.textSecondary, marginBottom: 6 }}>Password</label>
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete={isSignIn ? 'current-password' : 'new-password'} style={inputStyle} />
        {!isSignIn && (
          <div style={{ fontSize: 11.5, color: colors.textMuted, marginTop: 6 }}>At least 8 characters.</div>
        )}

        {error && <div style={{ color: colors.error, fontSize: 13, marginTop: 14 }}>{error}</div>}

        <button
          type="submit"
          disabled={busy || !username.trim() || !password}
          style={{ width: '100%', marginTop: 20, background: colors.accent, color: colors.accentText, border: 'none', fontFamily: 'inherit', fontWeight: 500, fontSize: 14.5, padding: '12px 14px', borderRadius: 8, cursor: 'pointer', opacity: busy || !username.trim() || !password ? 0.6 : 1 }}
        >
          {busy ? 'Please wait…' : isSignIn ? 'Sign in' : 'Create account'}
        </button>

        <div style={{ fontSize: 13, color: colors.textSecondary, marginTop: 18, textAlign: 'center' }}>
          {isSignIn ? 'New here? ' : 'Already have an account? '}
          <a href="#" onClick={(e) => { e.preventDefault(); setMode(isSignIn ? 'signup' : 'signin'); setError('') }} style={{ color: colors.agentBadgeText }}>
            {isSignIn ? 'Create an account' : 'Sign in'}
          </a>
        </div>
      </form>
    </div>
  )
}
