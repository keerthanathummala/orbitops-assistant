import { colors, fonts } from './theme'

export default function CoverPage({ onContinue }) {
  return (
    <div
      style={{
        minHeight: '100vh', width: '100%', display: 'flex', flexDirection: 'column',
        alignItems: 'center', justifyContent: 'center', fontFamily: fonts.body,
        background: colors.bluebg, color: colors.textMain, padding: '24px 16px', textAlign: 'center',
      }}
    >
      <div style={{ width: 56, height: 56, borderRadius: 14, background: colors.accent, marginBottom: 28 }} />

      <div style={{ fontFamily: fonts.heading, fontWeight: 600, fontSize: 28, color: colors.heading, maxWidth: 560, lineHeight: 1.3 }}>
        This is an AI chat demo built by a student for her portfolio.
      </div>

      <div style={{ fontSize: 15.5, color: colors.textSecondary, maxWidth: 480, lineHeight: 1.6, marginTop: 18 }}>
        It's a real, working app — not a mockup — with safety measures in place
        (input filtering, rate limiting, no login). Even so,{' '}
        <strong style={{ color: colors.textMain }}>
          please do not enter any personal, sensitive, or confidential information.
        </strong>{' '}
        Chats are stored without a login, so anyone with the right link could read them.
      </div>

      <button
        onClick={onContinue}
        style={{
          marginTop: 32, background: colors.accent, color: colors.accentText, border: 'none',
          fontFamily: 'inherit', fontWeight: 500, fontSize: 15, padding: '13px 28px',
          borderRadius: 10, cursor: 'pointer',
        }}
      >
        Continue to the chat
      </button>

      <div style={{ fontSize: 12, color: colors.textFaint, marginTop: 18 }}>
        Runs locally on qwen2.5:7b via Ollama — a student project, not a production or commercial service.
      </div>
    </div>
  )
}
