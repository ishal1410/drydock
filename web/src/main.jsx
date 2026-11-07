import { createRoot } from 'react-dom/client'
import './styles.css'
import Landing from './Landing.jsx'
import App from './App.jsx'

// Why here and not inline in index.html: the Content-Security-Policy allows no inline script.
if (/^#(dark|light)$/.test(location.hash)) document.documentElement.dataset.theme = location.hash.slice(1)
document.getElementById('font').media = 'all' // loaded as media=print so a slow font host never blocks the first paint

// ponytail: no router package. ?app or ?fixture opens the demo, anything else is the landing page.
const q = new URLSearchParams(location.search)
createRoot(document.getElementById('root')).render(q.has('app') || q.has('fixture') ? <App /> : <Landing />)
