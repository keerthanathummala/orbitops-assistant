// Palette matches the original design mockup (blue backgrounds, beige accent).
// Components read colors as CSS variables so the whole UI can switch between
// light and dark without re-rendering: `colors.accent` is 'var(--c-accent)'.
const light = {
  bluebg: '#EAF2F8',
  sidebarBg: '#F3F8FC',
  border: '#D7E5F0',
  borderSoft: '#DCE8F2',
  activeItemBg: '#DCEAF6',
  activeItemText: '#28384A',
  accent: '#A8926B',
  accentText: '#FAF8EF',
  heading: '#5B4A33',
  textMain: '#1C2A38',
  textSecondary: '#3E5668',
  textMuted: '#6E8599',
  textFaint: '#8FA6B8',
  placeholder: '#9FB4C4',
  avatarBg: '#E8DCC3',
  docsBadgeText: '#8A6A2E',
  docsBadgeBg: '#F3E9CF',
  docsBadgeBorder: '#E3D3A3',
  agentBadgeText: '#1E4D73',
  agentBadgeBg: '#D3E6F4',
  opsBadgeText: '#2F5D3A',
  opsBadgeBg: '#DCEFD9',
  opsBadgeBorder: '#BFDDBA',
  googleBadgeText: '#2E5A8A',
  googleBadgeBg: '#DCE6F1',
  googleBadgeBorder: '#BFD6EA',
  white: '#FFFFFF',
  dot: '#AEC2D1',
  codeBg: '#EEF3F7',
  error: '#B23A3A',
}

// Same hues, dark surfaces: navy backgrounds, lighter text, muted badges.
const dark = {
  bluebg: '#111A23',
  sidebarBg: '#16212C',
  border: '#2A3947',
  borderSoft: '#243240',
  activeItemBg: '#22384C',
  activeItemText: '#E4EDF5',
  accent: '#A8926B',
  accentText: '#FAF8EF',
  heading: '#E2CFA8',
  textMain: '#E4EDF5',
  textSecondary: '#B4C6D6',
  textMuted: '#8DA3B6',
  textFaint: '#6F8597',
  placeholder: '#5F7486',
  avatarBg: '#5B4A33',
  docsBadgeText: '#E6CB8C',
  docsBadgeBg: '#3A3020',
  docsBadgeBorder: '#5A4A2A',
  agentBadgeText: '#A9D0F0',
  agentBadgeBg: '#1E3A52',
  opsBadgeText: '#A8DDB2',
  opsBadgeBg: '#1F3A26',
  opsBadgeBorder: '#2F5A3A',
  googleBadgeText: '#A9C8EC',
  googleBadgeBg: '#1E3550',
  googleBadgeBorder: '#2E4F73',
  white: '#1B2733',
  dot: '#4D6274',
  codeBg: '#0E161E',
  error: '#F08A8A',
}

export const colors = Object.fromEntries(Object.keys(light).map((k) => [k, `var(--c-${k})`]))

const vars = (palette) => Object.entries(palette).map(([k, v]) => `--c-${k}:${v};`).join('')

// Dark follows the system setting unless the user picked a theme with the
// toggle, which sets <html data-theme="light|dark">.
export const themeCss = `
:root{${vars(light)}color-scheme:light;}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){${vars(dark)}color-scheme:dark;}}
:root[data-theme="dark"]{${vars(dark)}color-scheme:dark;}
body{background:var(--c-bluebg);}
input::placeholder{color:var(--c-placeholder);}
.md>:last-child{margin-bottom:0 !important;}
.app-shell{height:100vh;height:100dvh;overflow:hidden;}
.app-shell>.app-side,.app-shell>.app-main{height:100%;box-sizing:border-box;}
@media (max-width:860px){.app-shell{height:auto;min-height:100vh;overflow:visible;}.app-shell>.app-side,.app-shell>.app-main{height:auto;}.app-shell>.app-main{min-height:100vh;}}
`

export const fonts = {
  heading: "'Newsreader', serif",
  body: "'IBM Plex Sans', sans-serif",
  mono: "'IBM Plex Mono', monospace",
}
