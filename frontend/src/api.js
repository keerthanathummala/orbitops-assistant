// In the container build VITE_API_URL is set to "" so requests go to the same
// origin (/api/...), which nginx proxies to the backend service. Hence ?? not ||.
const API_BASE = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

export async function sendChat(threadId, message) {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thread_id: threadId, message }),
  })
  if (!res.ok) throw new Error((await res.json()).detail || 'Chat request failed')
  return res.json()
}

export async function fetchThreads(threadIds) {
  if (!threadIds.length) return []
  const res = await fetch(`${API_BASE}/api/threads?ids=${encodeURIComponent(threadIds.join(','))}`)
  if (!res.ok) return []
  return res.json()
}

export async function fetchThreadMessages(threadId) {
  const res = await fetch(`${API_BASE}/api/threads/${threadId}/messages`)
  if (!res.ok) return []
  return res.json()
}

export async function uploadDocument(threadId, file) {
  const form = new FormData()
  form.append('thread_id', threadId)
  form.append('file', file)
  const res = await fetch(`${API_BASE}/api/upload`, { method: 'POST', body: form })
  if (!res.ok) throw new Error((await res.json()).detail || 'Upload failed')
  return res.json()
}

export async function fetchStatus(threadId) {
  const res = await fetch(`${API_BASE}/api/status?thread_id=${threadId}`)
  if (!res.ok) return null
  return res.json()
}

export async function fetchHealth() {
  const res = await fetch(`${API_BASE}/api/health`)
  if (!res.ok) return null
  return res.json()
}

export async function runHealthCheck(threadId) {
  const form = new FormData()
  form.append('thread_id', threadId)
  const res = await fetch(`${API_BASE}/api/ops/health-check`, { method: 'POST', body: form })
  if (!res.ok) throw new Error((await res.json()).detail || 'Health check failed')
  return res.json()
}
