import { useEffect, useState } from 'react'
import { colors, fonts } from './theme'
import { deleteUser, fetchAdminActivity, fetchAdminUsers } from './api'

function when(iso) {
  return iso ? new Date(iso).toLocaleString() : '—'
}

// Short browser/OS label from the user-agent string.
function device(ua) {
  if (!ua) return '—'
  const browser = /Edg\//.test(ua) ? 'Edge' : /Chrome\//.test(ua) ? 'Chrome' : /Firefox\//.test(ua) ? 'Firefox' : /Safari\//.test(ua) ? 'Safari' : ua.split(' ')[0]
  const os = /Windows/.test(ua) ? 'Windows' : /Mac OS X/.test(ua) ? 'macOS' : /Android/.test(ua) ? 'Android' : /iPhone|iPad/.test(ua) ? 'iOS' : /Linux/.test(ua) ? 'Linux' : ''
  return os ? `${browser} on ${os}` : browser
}

const th = { textAlign: 'left', fontSize: 11.5, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em', color: colors.textMuted, padding: '10px 12px', borderBottom: `1px solid ${colors.border}` }
const td = { fontSize: 13.5, padding: '10px 12px', borderBottom: `1px solid ${colors.borderSoft}`, color: colors.textMain, whiteSpace: 'nowrap' }
const card = { background: colors.white, border: `1px solid ${colors.borderSoft}`, borderRadius: 12, overflowX: 'auto', marginBottom: 28 }

export default function AdminPage({ onBack }) {
  const [users, setUsers] = useState([])
  const [activity, setActivity] = useState([])
  const [error, setError] = useState('')

  async function load() {
    try {
      const [u, a] = await Promise.all([fetchAdminUsers(), fetchAdminActivity()])
      setUsers(u)
      setActivity(a)
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    let cancelled = false
    Promise.all([fetchAdminUsers(), fetchAdminActivity()])
      .then(([u, a]) => { if (!cancelled) { setUsers(u); setActivity(a) } })
      .catch((err) => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [])

  async function handleDelete(user) {
    if (!window.confirm(`Delete ${user.username}? Their account and chats are removed permanently. Their sign-in history stays in the log.`)) return
    setError('')
    try {
      await deleteUser(user.id)
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div style={{ minHeight: '100vh', background: colors.bluebg, color: colors.textMain, fontFamily: fonts.body, padding: '28px 5%' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginBottom: 8 }}>
        <div style={{ fontFamily: fonts.heading, fontWeight: 600, fontSize: 24, color: colors.heading }}>Admin</div>
        <button onClick={onBack} style={{ fontSize: 13, fontWeight: 500, color: colors.textSecondary, background: colors.white, border: `1px solid ${colors.border}`, borderRadius: 8, padding: '8px 14px', cursor: 'pointer', fontFamily: 'inherit' }}>
          Back to chat
        </button>
      </div>
      <div style={{ fontSize: 13, color: colors.textMuted, marginBottom: 24 }}>
        Who has an account, when they signed in and from which IP address. Chats are private: this page can't open them.
      </div>
      {error && <div role="alert" style={{ color: colors.error, fontSize: 13, marginBottom: 16 }}>{error}</div>}

      <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 10 }}>Users · {users.length}</div>
      <div style={card}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={th}>Username</th><th style={th}>Joined</th><th style={th}>Last active</th>
              <th style={th}>Last IP</th><th style={th}>Sign-ins</th><th style={th}></th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td style={td}>{u.username}{u.is_admin ? ' (admin)' : ''}</td>
                <td style={td}>{when(u.created_at)}</td>
                <td style={td}>{when(u.last_seen_at)}</td>
                <td style={td}>{u.last_ip || '—'}</td>
                <td style={td}>{u.logins}</td>
                <td style={{ ...td, textAlign: 'right' }}>
                  {!u.is_admin && (
                    <button onClick={() => handleDelete(u)} aria-label={`Delete ${u.username}`} style={{ fontSize: 12.5, color: colors.error, background: 'transparent', border: `1px solid ${colors.border}`, borderRadius: 7, padding: '5px 10px', cursor: 'pointer', fontFamily: 'inherit' }}>
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 10 }}>Sign-in history</div>
      <div style={card}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={th}>Time</th><th style={th}>Username</th><th style={th}>Event</th>
              <th style={th}>IP address</th><th style={th}>Device</th>
            </tr>
          </thead>
          <tbody>
            {activity.map((a, i) => (
              <tr key={i}>
                <td style={td}>{when(a.created_at)}</td>
                <td style={{ ...td, color: a.active ? colors.textMain : colors.textFaint }}>{a.username}{a.active ? '' : ' (deleted)'}</td>
                <td style={td}>{a.event === 'signup' ? 'Created account' : a.event === 'login' ? 'Signed in' : 'Signed out'}</td>
                <td style={td}>{a.ip || '—'}</td>
                <td style={td}>{device(a.user_agent)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
