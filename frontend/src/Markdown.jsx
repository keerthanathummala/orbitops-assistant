import { colors, fonts } from './theme'

// Gemini answers in Markdown (**bold**, bullet lists, `code`). Showing that
// raw left asterisks all over the chat, so render the common subset here.
// Builds React elements rather than HTML strings, so model output can never
// inject markup into the page.

const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|_[^_\s][^_]*_|\[[^\]]+\]\([^)\s]+\))/g

function renderInline(text, keyPrefix) {
  return text.split(INLINE).filter(Boolean).map((part, i) => {
    const key = `${keyPrefix}-${i}`
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code key={key} style={{ fontFamily: fonts.mono, fontSize: '0.9em', background: colors.codeBg, padding: '1px 5px', borderRadius: 4 }}>
          {part.slice(1, -1)}
        </code>
      )
    }
    if ((part.startsWith('**') && part.endsWith('**')) || (part.startsWith('__') && part.endsWith('__'))) {
      return <strong key={key}>{renderInline(part.slice(2, -2), key)}</strong>
    }
    if ((part.startsWith('*') && part.endsWith('*')) || (part.startsWith('_') && part.endsWith('_'))) {
      if (part.length > 2) return <em key={key}>{part.slice(1, -1)}</em>
    }
    const link = part.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/)
    if (link && /^https?:\/\//.test(link[2])) {
      return (
        <a key={key} href={link[2]} target="_blank" rel="noopener noreferrer" style={{ color: colors.agentBadgeText }}>
          {link[1]}
        </a>
      )
    }
    return part
  })
}

function parseBlocks(text) {
  const lines = text.replace(/\r\n/g, '\n').split('\n')
  const blocks = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (/^\s*```/.test(line)) {
      const code = []
      i++
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++])
      i++
      blocks.push({ type: 'code', text: code.join('\n') })
    } else if (/^\s*#{1,6}\s+/.test(line)) {
      blocks.push({ type: 'heading', text: line.replace(/^\s*#{1,6}\s+/, '') })
      i++
    } else if (/^\s*[-*+]\s+/.test(line)) {
      const items = []
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push({ text: lines[i].replace(/^\s*[-*+]\s+/, ''), nested: /^\s{2,}/.test(lines[i]) })
        i++
      }
      blocks.push({ type: 'ul', items })
    } else if (/^\s*\d+[.)]\s+/.test(line)) {
      const items = []
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push({ text: lines[i].replace(/^\s*\d+[.)]\s+/, '') })
        i++
      }
      blocks.push({ type: 'ol', items })
    } else if (line.trim() === '') {
      i++
    } else {
      const para = []
      while (i < lines.length && lines[i].trim() !== '' && !/^\s*(```|#{1,6}\s|[-*+]\s|\d+[.)]\s)/.test(lines[i])) {
        para.push(lines[i++])
      }
      blocks.push({ type: 'p', text: para.join('\n') })
    }
  }
  return blocks
}

const gap = { margin: '0 0 10px' }

export default function Markdown({ text }) {
  return (
    <div className="md">
      {parseBlocks(text || '').map((b, bi) => {
        const key = `b${bi}`
        if (b.type === 'code') {
          return (
            <pre key={key} style={{ ...gap, fontFamily: fonts.mono, fontSize: 13, background: colors.codeBg, padding: '10px 12px', borderRadius: 8, overflowX: 'auto', whiteSpace: 'pre' }}>
              {b.text}
            </pre>
          )
        }
        if (b.type === 'heading') {
          return <div key={key} style={{ ...gap, fontWeight: 600, color: colors.heading }}>{renderInline(b.text, key)}</div>
        }
        if (b.type === 'ul' || b.type === 'ol') {
          const List = b.type
          return (
            <List key={key} style={{ ...gap, paddingLeft: 22 }}>
              {b.items.map((it, ii) => (
                <li key={ii} style={{ marginBottom: 6, marginLeft: it.nested ? 18 : 0 }}>{renderInline(it.text, `${key}-${ii}`)}</li>
              ))}
            </List>
          )
        }
        return <p key={key} style={{ ...gap, whiteSpace: 'pre-wrap' }}>{renderInline(b.text, key)}</p>
      })}
    </div>
  )
}
