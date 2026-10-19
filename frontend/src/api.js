// In the container build VITE_API_URL is set to "" so requests go to the same
// origin (/api/...), which nginx proxies to the backend service. Hence ?? not ||.
const API_BASE = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

const TOKEN_KEY = 'orbitops_token'

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    // Storage blocked: the session just won't survive a reload.
  }
}

// Every call carries the session token. A 401 means it expired or the
// account was deleted, so App listens for this event and shows sign-in.
async function request(path, { method = 'GET', json, form } = {}) {
  const headers = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  let body
  if (json !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(json)
  } else if (form) {
    body = form
  }
  const res = await fetch(`${API_BASE}${path}`, { method, headers, body })
  if (res.status === 401 && token) {
    setToken(null)
    window.dispatchEvent(new Event('orbitops-signed-out'))
  }
  if (!res.ok) {
    let detail = ''
    try {
      detail = (await res.json()).detail
    } catch {
      // Not JSON (e.g. nginx error page).
    }
    const err = new Error(detail || `Request failed (${res.status})`)
    err.status = res.status
    throw err
  }
  return res.json()
}

export async function signUp(username, password) {
  const res = await request('/api/auth/signup', { method: 'POST', json: { username, password } })
  setToken(res.token)
  return res.user
}

export async function signIn(username, password) {
  const res = await request('/api/auth/login', { method: 'POST', json: { username, password } })
  setToken(res.token)
  return res.user
}

export async function signOut() {
  try {
    await request('/api/auth/logout', { method: 'POST' })
  } finally {
    setToken(null)
  }
}

export function fetchMe() {
  return request('/api/auth/me')
}

export function sendChat(threadId, message) {
  return request('/api/chat', { method: 'POST', json: { thread_id: threadId, message } })
}

export function fetchThreads() {
  return request('/api/threads')
}

export function fetchThreadMessages(threadId) {
  return request(`/api/threads/${threadId}/messages`)
}

export function uploadDocument(threadId, file) {
  const form = new FormData()
  form.append('thread_id', threadId)
  form.append('file', file)
  return request('/api/upload', { method: 'POST', form })
}

export function fetchStatus(threadId) {
  return request(`/api/status?thread_id=${threadId}`)
}

export async function fetchHealth() {
  const res = await fetch(`${API_BASE}/api/health`)
  if (!res.ok) return null
  return res.json()
}

export function runHealthCheck(threadId) {
  const form = new FormData()
  form.append('thread_id', threadId)
  return request('/api/ops/health-check', { method: 'POST', form })
}

export function fetchAdminUsers() {
  return request('/api/admin/users')
}

export function fetchAdminActivity() {
  return request('/api/admin/activity')
}

export function deleteUser(userId) {
  return request(`/api/admin/users/${userId}`, { method: 'DELETE' })
}
