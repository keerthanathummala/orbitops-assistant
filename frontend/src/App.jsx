import { useEffect, useRef, useState } from 'react'
import { colors, fonts } from './theme'
import { sendChat, fetchThreads, fetchThreadMessages, uploadDocument, fetchUploads, removeUpload, fetchStatus, fetchHealth, runHealthCheck, fetchMe, signOut, getToken } from './api'
import CoverPage from './CoverPage'
import AuthPage from './AuthPage'
import AdminPage from './AdminPage'
import Markdown from './Markdown'

const ACTIVE_THREAD_KEY = 'orbitops_active_thread'
const MAX_UPLOADS = 10
const THEME_KEY = 'orbitops_theme'
const THEME_LABELS = { system: 'Theme · auto', light: 'Theme · light', dark: 'Theme · dark' }
const NEXT_THEME = { system: 'light', light: 'dark', dark: 'system' }

function loadTheme() {
  try {
    const t = localStorage.getItem(THEME_KEY)
    return t === 'light' || t === 'dark' ? t : 'system'
  } catch {
    return 'system'
  }
}

// crypto.randomUUID() only exists on HTTPS/localhost, and the GKE load
// balancer serves plain HTTP, so build a v4 UUID from getRandomValues.
function newThreadId() {
  const b = crypto.getRandomValues(new Uint8Array(16))
  b[6] = (b[6] & 0x0f) | 0x40
  b[8] = (b[8] & 0x3f) | 0x80
  const h = [...b].map((x) => x.toString(16).padStart(2, '0')).join('')
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`
}

export default function App() {
  const [started, setStarted] = useState(false)
  // Signed-in account; authChecked is false until a saved session has been verified.
  const [user, setUser] = useState(null)
  const [authChecked, setAuthChecked] = useState(!getToken())
  const [view, setView] = useState('chat')
  const [threads, setThreads] = useState([])
  const [activeThread, setActiveThread] = useState(localStorage.getItem(ACTIVE_THREAD_KEY) || null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [status, setStatus] = useState({ docs_indexed: 0, google_search: null })
  // Files uploaded to the active chat: [{id, filename}], each removable.
  const [uploads, setUploads] = useState([])
  const uploadedCount = uploads.length
  // The file being uploaded and the view it was attached in, while it uploads.
  const [uploading, setUploading] = useState(null)
  const [error, setError] = useState('')
  // Which backend pod/version answered last -- makes load balancing and
  // rolling updates visible when demoing the Kubernetes deployment.
  const [served, setServed] = useState(null)
  // 'system' follows the laptop's light/dark setting; the toggle can pin either.
  const [theme, setTheme] = useState(loadTheme)
  const fileInputRef = useRef(null)
  const scrollRef = useRef(null)
  const endRef = useRef(null)
  // Bumped whenever the user opens another chat or starts a new one. A reply
  // that arrives after that belongs to the chat it was sent from, which
  // loads it from the server when reopened, so it isn't added to this view.
  const viewRef = useRef(0)
  const [viewId, setViewId] = useState(0)
  // The view a request is waiting on, so "Thinking…" shows only there.
  const [pendingView, setPendingView] = useState(null)
  // A chat that was just created by sending from "New chat": its messages are
  // already on screen, so opening it doesn't reload them.
  const justCreatedRef = useRef(null)

  // Resume a saved session, and drop back to sign-in whenever the API says
  // the session is gone (expired, signed out elsewhere, or account deleted).
  useEffect(() => {
    if (getToken()) {
      fetchMe().then(setUser).catch(() => {}).finally(() => setAuthChecked(true))
    }
    const onSignedOut = () => {
      setUser(null)
      setView('chat')
      setThreads([])
      setActiveThread(null)
      localStorage.removeItem(ACTIVE_THREAD_KEY)
    }
    window.addEventListener('orbitops-signed-out', onSignedOut)
    return () => window.removeEventListener('orbitops-signed-out', onSignedOut)
  }, [])

  // The sidebar lists this account's own chats from the server.
  function refreshThreads() {
    fetchThreads().then(setThreads).catch(() => {})
  }

  useEffect(() => {
    if (user) refreshThreads()
  }, [user])

  // Load messages + status whenever the active thread changes.
  useEffect(() => {
    if (!user) return
    if (!activeThread) {
      setMessages([])
      setStatus({ docs_indexed: 0, google_search: null })
      setUploads([])
      return
    }
    // Responses for a chat the user has already left are ignored, so a slow
    // load of chat A can't overwrite chat B after a quick switch.
    let current = true
    if (justCreatedRef.current === activeThread) {
      justCreatedRef.current = null
    } else {
      setMessages([])
      setUploads([])
      fetchThreadMessages(activeThread)
        .then((m) => { if (current) setMessages(m) })
        .catch((e) => { if (current) setError(`Couldn't load this chat: ${e.message}`) })
    }
    fetchUploads(activeThread).then((u) => { if (current) setUploads(u) }).catch(() => {})
    fetchStatus(activeThread).then((s) => { if (current && s) setStatus(s) }).catch(() => {})
    return () => { current = false }
  }, [activeThread, user])

  useEffect(() => {
    fetchHealth().then(setServed).catch(() => {})
  }, [])

  useEffect(() => {
    if (theme === 'system') delete document.documentElement.dataset.theme
    else document.documentElement.dataset.theme = theme
    try {
      if (theme === 'system') localStorage.removeItem(THEME_KEY)
      else localStorage.setItem(THEME_KEY, theme)
    } catch {
      // Storage blocked: the choice just won't survive a reload.
    }
  }, [theme])

  // Keep the newest message (and the "Thinking…" line) in view. On a narrow
  // screen the whole page scrolls instead, so go to its end, where the input is.
  useEffect(() => {
    const page = document.scrollingElement
    if (page && page.scrollHeight > window.innerHeight) {
      window.scrollTo({ top: page.scrollHeight, behavior: 'smooth' })
    } else {
      endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
    }
  }, [messages, sending, error])

  function persistActiveThread(id) {
    viewRef.current += 1
    setViewId(viewRef.current)
    setActiveThread(id)
    if (id) localStorage.setItem(ACTIVE_THREAD_KEY, id)
    else localStorage.removeItem(ACTIVE_THREAD_KEY)
  }

  function handleNewChat() {
    persistActiveThread(null)
    setMessages([])
    setInput('')
    setError('')
  }

  function handleSelectThread(id) {
    if (id === activeThread) return
    setError('')
    persistActiveThread(id)
  }

  async function handleSend() {
    const text = input.trim()
    if (!text || sending) return
    setError('')
    setSending(true)
    setInput('')
    const pending = { role: 'user', content: text, source: null }
    setMessages((prev) => [...prev, pending])
    const view = viewRef.current
    setPendingView(view)

    try {
      const res = await sendChat(activeThread, text)
      refreshThreads()
      fetchHealth().then(setServed).catch(() => {})
      if (viewRef.current !== view) return  // the user moved to another chat meanwhile
      const reply = res.segments.map((seg) => ({ role: 'assistant', content: seg.text, source: seg.source, citations: seg.citations }))
      if (!activeThread) {
        justCreatedRef.current = res.thread_id
        persistActiveThread(res.thread_id)
      }
      setMessages((prev) => [...prev, ...reply])
      fetchStatus(res.thread_id).then((s) => s && setStatus(s)).catch(() => {})
    } catch (e) {
      if (viewRef.current === view) {
        // Nothing was saved: take the question back out of the chat and put
        // it in the box again, so it can be sent once the problem clears.
        setMessages((prev) => prev.filter((m) => m !== pending))
        setInput((current) => current || text)
        setError(e.message || 'Something went wrong — is the backend running?')
      }
    } finally {
      setSending(false)
      setPendingView(null)
    }
  }

  // AIOps: scan the live Kubernetes deployment and post the result into the chat.
  async function handleHealthCheck() {
    if (sending) return
    setError('')
    setSending(true)
    const threadId = activeThread || newThreadId()
    const pending = { role: 'user', content: 'Run a cluster health check', source: null }
    setMessages((prev) => [...prev, pending])
    const view = viewRef.current
    setPendingView(view)
    try {
      const res = await runHealthCheck(threadId)
      refreshThreads()
      fetchHealth().then(setServed).catch(() => {})
      if (viewRef.current !== view) return
      if (!activeThread) {
        justCreatedRef.current = threadId
        persistActiveThread(threadId)
      }
      setMessages((prev) => [...prev, { role: 'assistant', content: res.text, source: 'ops', citations: res.citations }])
    } catch (e) {
      if (viewRef.current === view) {
        setMessages((prev) => prev.filter((m) => m !== pending))
        setError(e.message || 'Health check failed')
      }
    } finally {
      setSending(false)
      setPendingView(null)
    }
  }

  async function handleFileChange(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file || uploading) return
    if (uploadedCount >= MAX_UPLOADS) {
      setError(`Upload limit reached (${MAX_UPLOADS} documents max).`)
      return
    }
    let threadId = activeThread
    if (!threadId) {
      // Starting a brand-new chat with an upload: create the thread first via an empty-ish turn is
      // awkward, so just require the user to send a first message before attaching. Simpler UX.
      setError('Send your first message before attaching documents.')
      return
    }
    const view = viewRef.current
    setError('')
    setUploading({ name: file.name, view })
    try {
      await uploadDocument(threadId, file)
      // If the user has opened another chat, that chat's own list is shown;
      // this one picks the file up when reopened.
      if (viewRef.current !== view) return
      fetchUploads(threadId).then((u) => { if (viewRef.current === view) setUploads(u) }).catch(() => {})
      fetchStatus(threadId).then((s) => { if (viewRef.current === view && s) setStatus(s) }).catch(() => {})
    } catch (err) {
      if (viewRef.current === view) setError(err.message)
    } finally {
      setUploading(null)
    }
  }

  async function handleRemoveUpload(upload) {
    setError('')
    try {
      await removeUpload(activeThread, upload.id)
      setUploads((prev) => prev.filter((u) => u.id !== upload.id))
      fetchStatus(activeThread).then((s) => s && setStatus(s)).catch(() => {})
    } catch (err) {
      setError(err.message)
    }
  }

  // Model pill: the LLM that writes every answer (from /api/health).
  const llmInfo = served?.llm
  const llmPillText = !llmInfo
    ? 'Gemini · connecting…'
    : llmInfo.provider === 'ollama'
      ? `Ollama · ${llmInfo.model}`
      : llmInfo.configured
        ? `Gemini · ${llmInfo.model}`
        : 'Gemini · API key missing'

  async function handleSignOut() {
    await signOut().catch(() => {})
    window.dispatchEvent(new Event('orbitops-signed-out'))
  }

  if (!started) {
    return <CoverPage onContinue={() => setStarted(true)} />
  }
  if (!authChecked) return null
  if (!user) {
    return <AuthPage onSignedIn={(u) => { setUser(u); setActiveThread(null); localStorage.removeItem(ACTIVE_THREAD_KEY) }} />
  }
  if (view === 'admin' && user.is_admin) {
    return <AdminPage onBack={() => setView('chat')} />
  }

  return (
    // The page itself never scrolls on a wide screen: the chat list and the
    // messages scroll inside their columns, so the input box stays in view
    // (on a narrow screen the columns stack and the page scrolls; theme.js).
    <div className="app-shell" style={{ width: '100%', display: 'flex', flexWrap: 'wrap', fontFamily: fonts.body, background: colors.bluebg, color: colors.textMain }}>
      {/* Sidebar */}
      <div className="app-side" style={{ width: 272, flex: '0 0 272px', background: colors.sidebarBg, borderRight: `1px solid ${colors.border}`, display: 'flex', flexDirection: 'column', padding: '24px 18px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '0 6px 22px', borderBottom: `1px solid ${colors.borderSoft}`, marginBottom: 18 }}>
          <div style={{ width: 30, height: 30, borderRadius: 8, background: colors.accent, flexShrink: 0 }} />
          <div>
            <div style={{ fontFamily: fonts.heading, fontWeight: 600, fontSize: 19, color: colors.heading, letterSpacing: '-0.01em' }}>OrbitOps Assistant</div>
            <div style={{ fontSize: 12, color: colors.textMuted, marginTop: 2 }}>Docs · GitHub · HackerNews · Cluster</div>
          </div>
        </div>

        <button
          onClick={handleNewChat}
          style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8, background: colors.accent, color: colors.accentText, border: 'none', fontFamily: 'inherit', fontWeight: 500, fontSize: 14, padding: '11px 14px', borderRadius: 8, cursor: 'pointer', marginBottom: 22 }}
        >
          <span style={{ fontSize: 16, lineHeight: 1 }}>+</span> New chat
        </button>

        <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.06em', color: colors.textMuted, padding: '0 6px 10px' }}>
          Your chats
        </div>
        <div style={{ overflowY: 'auto', flex: 1, minHeight: 0 }}>
          {threads.length === 0 && (
            <div style={{ fontSize: 13, color: colors.textFaint, padding: '0 10px' }}>No chats yet</div>
          )}
          {threads.map((t) => (
            <a
              key={t.thread_id}
              href="#"
              onClick={(e) => { e.preventDefault(); handleSelectThread(t.thread_id) }}
              style={{
                display: 'block', padding: '10px 10px', borderRadius: 7, fontSize: 13.5,
                color: t.thread_id === activeThread ? colors.activeItemText : colors.textSecondary,
                background: t.thread_id === activeThread ? colors.activeItemBg : 'transparent',
                marginBottom: 4,
              }}
            >
              {t.title}
            </a>
          ))}
        </div>

        <div style={{ borderTop: `1px solid ${colors.borderSoft}`, paddingTop: 16, display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ width: 32, height: 32, borderRadius: '50%', background: colors.avatarBg, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 14, fontWeight: 600, color: colors.heading }}>
            {user.username.slice(0, 1).toUpperCase()}
          </div>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ fontSize: 13, fontWeight: 500, color: colors.textMain, overflow: 'hidden', textOverflow: 'ellipsis' }}>{user.username}</div>
            <div style={{ fontSize: 11.5, display: 'flex', gap: 10 }}>
              {user.is_admin && (
                <a href="#" onClick={(e) => { e.preventDefault(); setView('admin') }} style={{ color: colors.agentBadgeText }}>Admin</a>
              )}
              <a href="#" onClick={(e) => { e.preventDefault(); handleSignOut() }} style={{ color: colors.textMuted }}>Sign out</a>
            </div>
          </div>
        </div>
      </div>

      {/* Main column */}
      <div className="app-main" style={{ flex: '999 1 560px', minWidth: 0, display: 'flex', flexDirection: 'column' }}>
        {/* Top bar */}
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'flex-end', gap: 12, padding: '18px 32px', borderBottom: `1px solid ${colors.border}`, background: colors.sidebarBg }}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button
              onClick={() => setTheme(NEXT_THEME[theme])}
              title="Switch between automatic, light and dark"
              style={{ fontSize: 12, fontWeight: 500, color: colors.textSecondary, background: colors.white, border: `1px solid ${colors.border}`, borderRadius: 20, padding: '6px 12px', cursor: 'pointer', fontFamily: 'inherit' }}
            >
              {THEME_LABELS[theme]}
            </button>
            <button
              onClick={handleHealthCheck}
              disabled={sending}
              style={{ fontSize: 12, fontWeight: 600, color: colors.opsBadgeText, background: colors.opsBadgeBg, border: `1px solid ${colors.opsBadgeBorder}`, borderRadius: 20, padding: '6px 12px', cursor: sending ? 'default' : 'pointer', fontFamily: 'inherit' }}
            >
              Run health check
            </button>
            <div style={{ fontSize: 12, fontWeight: 500, color: colors.docsBadgeText, background: colors.docsBadgeBg, border: `1px solid ${colors.docsBadgeBorder}`, borderRadius: 20, padding: '6px 12px' }}>
              Docs indexed · {status.docs_indexed}
            </div>
            <div style={{ fontSize: 12, fontWeight: 500, color: colors.googleBadgeText, background: colors.googleBadgeBg, border: `1px solid ${colors.googleBadgeBorder}`, borderRadius: 20, padding: '6px 12px' }}>
              {llmPillText}
            </div>
          </div>
        </div>

        {/* Messages */}
        <div ref={scrollRef} style={{ flex: '1 1 auto', minHeight: 0, padding: '32px 10% 20px', display: 'flex', flexDirection: 'column', gap: 22, overflowY: 'auto' }}>
          {messages.length === 0 && (
            <div style={{ color: colors.textFaint, fontSize: 14, textAlign: 'center', marginTop: 40 }}>
              Ask about OrbitOps docs, GitHub &amp; HackerNews, or the live cluster (pods, restarts, logs).
            </div>
          )}
          {messages.map((m, i) =>
            m.role === 'user' ? (
              <div key={i} style={{ alignSelf: 'flex-end', maxWidth: '66%', display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6 }}>
                <div style={{ background: colors.accent, color: colors.accentText, padding: '13px 18px', borderRadius: '16px 16px 4px 16px', fontSize: 14.5, lineHeight: 1.55 }}>
                  {m.content}
                </div>
              </div>
            ) : (
              <div key={i} style={{ alignSelf: 'flex-start', maxWidth: '72%', display: 'flex', flexDirection: 'column', gap: 8 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span
                    style={{
                      fontSize: 11, fontWeight: 600, letterSpacing: '0.03em', borderRadius: 20, padding: '3px 10px',
                      color: m.source === 'agent' ? colors.agentBadgeText : m.source === 'ops' ? colors.opsBadgeText : colors.docsBadgeText,
                      background: m.source === 'agent' ? colors.agentBadgeBg : m.source === 'ops' ? colors.opsBadgeBg : colors.docsBadgeBg,
                    }}
                  >
                    {m.source === 'agent' ? 'AGENT' : m.source === 'ops' ? 'OPS' : 'DOCS'}
                  </span>
                  {m.citations && m.citations.length > 0 && (
                    <span style={{ fontSize: 11.5, color: colors.textMuted }}>
                      {m.citations.map((c) => c.doc_id).join(' · ')}
                    </span>
                  )}
                </div>
                <div style={{ background: colors.white, border: `1px solid ${colors.borderSoft}`, padding: '16px 20px', borderRadius: '4px 16px 16px 16px', fontSize: 14.5, lineHeight: 1.6, color: colors.textMain }}>
                  <Markdown text={m.content} />
                </div>
              </div>
            )
          )}
          {sending && pendingView === viewId && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, paddingLeft: 2 }}>
              <div style={{ width: 5, height: 5, borderRadius: '50%', background: colors.dot }} />
              <div style={{ width: 5, height: 5, borderRadius: '50%', background: colors.dot }} />
              <div style={{ width: 5, height: 5, borderRadius: '50%', background: colors.dot }} />
              <span style={{ fontSize: 11.5, color: colors.textMuted, marginLeft: 2 }}>Thinking…</span>
            </div>
          )}
          {error && <div style={{ color: colors.error, fontSize: 13 }}>{error}</div>}
          <div ref={endRef} />
        </div>

        {/* Input bar */}
        <div style={{ padding: '16px 10% 26px' }}>
          {uploading && uploading.view === viewId && (
            <div role="status" style={{ fontSize: 12.5, color: colors.textMuted, marginBottom: 8 }}>
              Uploading {uploading.name}… large PDFs can take a minute to index.
            </div>
          )}
          {uploads.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
              {uploads.map((u) => (
                <span key={u.id} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12.5, color: colors.textSecondary, background: colors.white, border: `1px solid ${colors.border}`, borderRadius: 16, padding: '4px 6px 4px 11px', maxWidth: 260 }}>
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={u.filename}>{u.filename}</span>
                  <button
                    onClick={() => handleRemoveUpload(u)}
                    title={`Remove ${u.filename}`}
                    aria-label={`Remove ${u.filename}`}
                    style={{ border: 'none', background: colors.bluebg, color: colors.textMuted, borderRadius: '50%', width: 18, height: 18, lineHeight: '16px', fontSize: 13, padding: 0, cursor: 'pointer', flexShrink: 0 }}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
          <div style={{ background: colors.white, border: `1px solid ${colors.border}`, borderRadius: 14, padding: '10px 10px 10px 18px', display: 'flex', alignItems: 'center', gap: 10, boxShadow: '0 1px 2px rgba(28,42,56,0.05)' }}>
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() } }}
              placeholder="Ask about OrbitOps docs, GitHub & HackerNews, or the live cluster…"
              style={{ flex: '1 1 auto', fontSize: 14.5, color: colors.textMain, border: 'none', outline: 'none', fontFamily: fonts.body, background: 'transparent' }}
            />
            <input type="file" ref={fileInputRef} style={{ display: 'none' }} onChange={handleFileChange} accept=".txt,.md,.docx,.pdf" />
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={uploadedCount >= MAX_UPLOADS || uploading !== null}
              style={{ fontSize: 12.5, fontWeight: 500, color: colors.textSecondary, background: colors.bluebg, border: `1px solid ${colors.border}`, borderRadius: 8, padding: '8px 12px', cursor: uploading ? 'default' : 'pointer', opacity: uploading ? 0.6 : 1, whiteSpace: 'nowrap' }}
            >
              {uploading ? 'Uploading…' : `Attach · ${uploadedCount}/${MAX_UPLOADS}`}
            </button>
            <button
              onClick={handleSend}
              disabled={sending || !input.trim()}
              style={{ width: 38, height: 38, flexShrink: 0, borderRadius: 10, background: colors.accent, border: 'none', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', opacity: sending || !input.trim() ? 0.6 : 1 }}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" style={{ stroke: colors.accentText }} strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                <line x1="12" y1="19" x2="12" y2="5"></line>
                <polyline points="5 12 12 5 19 12"></polyline>
              </svg>
            </button>
          </div>
          <div style={{ textAlign: 'center', fontSize: 11, color: colors.textFaint, marginTop: 10 }}>
            {served?.pod
              ? `Served by pod ${served.pod} · version ${served.version}`
              : 'Answers from the OrbitOps docs, GitHub, HackerNews & the live cluster, written by Gemini.'}
          </div>
        </div>
      </div>
    </div>
  )
}
