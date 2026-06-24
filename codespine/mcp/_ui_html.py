"""Enterprise-grade CodeSpine dashboard HTML.

This module contains the full single-page application HTML/CSS/JS served
by ``codespine ui``.  Kept separate from ``cli.py`` to keep the CLI monolith
manageable.
"""

__all__ = ["UI_HTML"]

UI_HTML: str = r"""<!doctype html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CodeSpine — Code Intelligence Dashboard</title>
<style>
/* ═══════════════════════════════════════════════════════════════════════════
   Design System Tokens
   ═══════════════════════════════════════════════════════════════════════════ */
:root {
  --font: "Inter","SF Pro",system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --mono: "SF Mono","JetBrains Mono","Fira Code",monospace;
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;
  --shadow-sm: 0 1px 2px rgba(0,0,0,.04);
  --shadow-md: 0 4px 12px rgba(0,0,0,.06);
  --shadow-lg: 0 8px 30px rgba(0,0,0,.08);
  --transition: .18s cubic-bezier(.4,0,.2,1);
  --sidebar-w: 240px;
}
/* Light */
[data-theme="light"] {
  --bg-page: #f3f5f8;
  --bg-surface: #ffffff;
  --bg-sidebar: #151b26;
  --bg-sidebar-hover: #1f2a3a;
  --bg-sidebar-active: #2a3a52;
  --bg-input: #ffffff;
  --bg-code: #f0f2f5;
  --bg-tag: #eef2f7;
  --bg-card: #ffffff;
  --text: #1f2328;
  --text-secondary: #656d78;
  --text-muted: #8b949e;
  --text-sidebar: #9aa4b3;
  --text-sidebar-active: #ffffff;
  --border: #e0e4ea;
  --border-light: #eef0f3;
  --accent: #2463eb;
  --accent-hover: #1a55d0;
  --accent-soft: #e9f0ff;
  --green: #1a7f4a;
  --green-bg: #e6f4ea;
  --yellow: #b05a00;
  --yellow-bg: #fef7e0;
  --red: #b3261e;
  --red-bg: #fce8e6;
  --blue: #1750a0;
  --blue-bg: #e0edff;
  --purple: #7c3aed;
  --purple-bg: #f0e8ff;
  --scrollbar: #d0d5dd;
}
/* Dark */
[data-theme="dark"] {
  --bg-page: #0d1117;
  --bg-surface: #161b22;
  --bg-sidebar: #0d1117;
  --bg-sidebar-hover: #161b22;
  --bg-sidebar-active: #1c2333;
  --bg-input: #0d1117;
  --bg-code: #161b22;
  --bg-tag: #1c2333;
  --bg-card: #161b22;
  --text: #e6edf3;
  --text-secondary: #8b949e;
  --text-muted: #6e7681;
  --text-sidebar: #8b949e;
  --text-sidebar-active: #ffffff;
  --border: #30363d;
  --border-light: #21262d;
  --accent: #4896ff;
  --accent-hover: #6aa6ff;
  --accent-soft: #0d2463;
  --green: #3fb950;
  --green-bg: #0f2d1a;
  --yellow: #d29922;
  --yellow-bg: #2d1f00;
  --red: #f85149;
  --red-bg: #2d0f0e;
  --blue: #58a6ff;
  --blue-bg: #0a1f3d;
  --purple: #bc8cff;
  --purple-bg: #1c1335;
  --scrollbar: #30363d;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-family:var(--font);color:var(--text);background:var(--bg-page);-webkit-font-smoothing:antialiased}
body{display:flex;min-height:100vh;overflow:hidden}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
code{font-family:var(--mono);font-size:.875em;background:var(--bg-code);padding:1px 5px;border-radius:4px}
button{cursor:pointer;font-family:var(--font);font-size:14px;transition:all var(--transition)}
input,select,textarea{font-family:var(--font);font-size:14px;color:var(--text);background:var(--bg-input);border:1px solid var(--border);border-radius:var(--radius-sm);padding:8px 12px;outline:none;transition:border-color var(--transition)}
input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
::placeholder{color:var(--text-muted)}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--scrollbar);border-radius:3px}
/* ═══════════════════════════════════════════════════════════════════════════
   Sidebar
   ═══════════════════════════════════════════════════════════════════════════ */
.sidebar{width:var(--sidebar-w);background:var(--bg-sidebar);display:flex;flex-direction:column;flex-shrink:0;overflow-y:auto;border-right:1px solid var(--border)}
.sidebar-brand{padding:20px 18px 14px;border-bottom:1px solid rgba(255,255,255,.06)}
.sidebar-brand h1{font-size:18px;font-weight:700;color:#fff;letter-spacing:-.3px;display:flex;align-items:center;gap:8px}
.sidebar-brand .sub{font-size:11px;color:var(--text-sidebar);margin-top:2px;letter-spacing:.3px;text-transform:uppercase}
.sidebar-nav{padding:8px 0;flex:1}
.nav-item{display:flex;align-items:center;gap:10px;padding:9px 18px;color:var(--text-sidebar);font-size:14px;cursor:pointer;transition:all var(--transition);border:none;background:none;width:100%;text-align:left;font-weight:450}
.nav-item:hover{background:var(--bg-sidebar-hover);color:var(--text-sidebar-active)}
.nav-item.active{background:var(--bg-sidebar-active);color:var(--text-sidebar-active);font-weight:550}
.nav-item .icon{font-size:17px;width:22px;text-align:center;flex-shrink:0}
.nav-item .badge{margin-left:auto;font-size:11px;padding:1px 7px;border-radius:10px;background:var(--accent);color:#fff}
.sidebar-footer{padding:12px 18px;border-top:1px solid rgba(255,255,255,.06);font-size:12px;color:var(--text-sidebar);display:flex;flex-direction:column;gap:6px}
.sidebar-footer .version{color:var(--text-sidebar)}/* ═══════════════════════════════════════════════════════════════════════════
   Main Content Area
   ═══════════════════════════════════════════════════════════════════════════ */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
.topbar{display:flex;align-items:center;justify-content:space-between;padding:12px 24px;background:var(--bg-surface);border-bottom:1px solid var(--border);min-height:52px;flex-shrink:0;gap:12px}
.topbar .page-title{font-size:16px;font-weight:600;letter-spacing:-.2px}
.topbar-actions{display:flex;align-items:center;gap:8px}
.btn-icon{background:none;border:none;color:var(--text-secondary);padding:6px;border-radius:var(--radius-sm);display:inline-flex;align-items:center;justify-content:center;font-size:18px;transition:all var(--transition)}
.btn-icon:hover{background:var(--bg-tag);color:var(--text)}
.content{padding:20px 24px;overflow-y:auto;flex:1}
/* ═══════════════════════════════════════════════════════════════════════════
   Buttons
   ═══════════════════════════════════════════════════════════════════════════ */
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:var(--radius-sm);font-weight:500;font-size:13px;border:1px solid var(--border);background:var(--bg-surface);color:var(--text);transition:all var(--transition)}
.btn:hover{background:var(--bg-tag)}
.btn-primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn-primary:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
.btn-danger{background:var(--red);color:#fff;border-color:var(--red)}
.btn-danger:hover{background:#a11c15}
.btn-sm{padding:5px 10px;font-size:12px}
.btn-lg{padding:10px 20px;font-size:15px}
.btn-ghost{background:transparent;border-color:transparent;color:var(--text-secondary)}
.btn-ghost:hover{background:var(--bg-tag);color:var(--text)}
.btn-group{display:flex;gap:6px;flex-wrap:wrap}
/* ═══════════════════════════════════════════════════════════════════════════
   Cards & Grid
   ═══════════════════════════════════════════════════════════════════════════ */
.card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:18px 20px;transition:box-shadow var(--transition)}
.card:hover{box-shadow:var(--shadow-sm)}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.card-title{font-size:14px;font-weight:600;letter-spacing:-.1px}
.card-value{font-size:28px;font-weight:700;letter-spacing:-.5px;line-height:1.1}
.card-label{font-size:12px;color:var(--text-secondary);margin-top:2px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
.grid-4{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
/* ═══════════════════════════════════════════════════════════════════════════
   Status Badges
   ═══════════════════════════════════════════════════════════════════════════ */
.badge{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:20px;font-size:12px;font-weight:500}
.badge-green{background:var(--green-bg);color:var(--green)}
.badge-yellow{background:var(--yellow-bg);color:var(--yellow)}
.badge-red{background:var(--red-bg);color:var(--red)}
.badge-blue{background:var(--blue-bg);color:var(--blue)}
.badge-purple{background:var(--purple-bg);color:var(--purple)}
.badge-gray{background:var(--bg-tag);color:var(--text-secondary)}
.badge-dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.badge-dot.green{background:var(--green)}
.badge-dot.yellow{background:var(--yellow)}
.badge-dot.red{background:var(--red)}
/* ═══════════════════════════════════════════════════════════════════════════
   Table
   ═══════════════════════════════════════════════════════════════════════════ */
.table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:var(--radius-md);background:var(--bg-surface)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:9px 14px;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px;color:var(--text-secondary);background:var(--bg-tag);border-bottom:1px solid var(--border);white-space:nowrap;cursor:pointer;user-select:none}
th:hover{color:var(--text)}
th .sort{opacity:.3;margin-left:4px}
th.sorted .sort{opacity:1;color:var(--accent)}
td{padding:9px 14px;border-bottom:1px solid var(--border-light);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--bg-tag)}
/* ═══════════════════════════════════════════════════════════════════════════
   Search Results
   ═══════════════════════════════════════════════════════════════════════════ */
.result-card{padding:14px 16px;border:1px solid var(--border);border-radius:var(--radius-md);margin-bottom:8px;background:var(--bg-surface);transition:border-color var(--transition)}
.result-card:hover{border-color:var(--accent)}
.result-card .title{font-size:14px;font-weight:550;color:var(--accent);margin-bottom:2px}
.result-card .meta{font-size:12px;color:var(--text-secondary);display:flex;gap:12px;flex-wrap:wrap}
.result-card .snippet{font-family:var(--mono);font-size:12px;line-height:1.5;margin-top:8px;padding:10px 12px;background:var(--bg-code);border-radius:var(--radius-sm);overflow-x:auto;white-space:pre;max-height:200px;overflow-y:auto;color:var(--text)}
/* ═══════════════════════════════════════════════════════════════════════════
   Diff
   ═══════════════════════════════════════════════════════════════════════════ */
.diff-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
.diff-pane{font-family:var(--mono);font-size:12px;line-height:1.6;padding:14px;background:var(--bg-code);border:1px solid var(--border);border-radius:var(--radius-sm);white-space:pre;overflow:auto;max-height:500px}
.diff-added{background:var(--green-bg);color:var(--green)}
.diff-removed{background:var(--red-bg);color:var(--red)}
/* ═══════════════════════════════════════════════════════════════════════════
   Tabs
   ═══════════════════════════════════════════════════════════════════════════ */
.tabs{display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:16px}
.tab{padding:8px 18px;font-size:13px;font-weight:500;color:var(--text-secondary);cursor:pointer;border:none;background:none;border-bottom:2px solid transparent;transition:all var(--transition);margin-bottom:-1px}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
/* ═══════════════════════════════════════════════════════════════════════════
   Chat / Q&A
   ═══════════════════════════════════════════════════════════════════════════ */
.chat-container{max-width:800px;margin:0 auto;display:flex;flex-direction:column;height:calc(100vh - 160px)}
.chat-messages{flex:1;overflow-y:auto;padding:16px 0;display:flex;flex-direction:column;gap:16px}
.chat-bubble{max-width:85%;padding:12px 16px;border-radius:var(--radius-lg);font-size:14px;line-height:1.55;animation:fadeIn .2s ease}
.chat-bubble.question{background:var(--accent);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
.chat-bubble.answer{background:var(--bg-card);border:1px solid var(--border);align-self:flex-start;border-bottom-left-radius:4px}
.chat-bubble.answer .citations{font-size:12px;color:var(--text-secondary);margin-top:8px;padding-top:8px;border-top:1px solid var(--border-light)}
.chat-bubble.answer .citations a{color:var(--accent)}
.chat-input-row{display:flex;gap:8px;padding:12px 0;border-top:1px solid var(--border)}
.chat-input-row input{flex:1;padding:10px 14px}
/* ═══════════════════════════════════════════════════════════════════════════
   Loading / Empty / Error states
   ═══════════════════════════════════════════════════════════════════════════ */
.loading{display:flex;align-items:center;justify-content:center;padding:40px;color:var(--text-muted);gap:8px}
.spinner{width:18px;height:18px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.empty-state{text-align:center;padding:50px 20px;color:var(--text-muted)}
.empty-state .icon{font-size:40px;margin-bottom:12px}
.empty-state .title{font-size:16px;font-weight:600;color:var(--text-secondary);margin-bottom:4px}
.empty-state .desc{font-size:13px}
.error-banner{padding:10px 14px;background:var(--red-bg);color:var(--red);border-radius:var(--radius-sm);font-size:13px;margin-bottom:12px;display:flex;align-items:center;gap:8px}
/* ═══════════════════════════════════════════════════════════════════════════
   Toast notifications
   ═══════════════════════════════════════════════════════════════════════════ */
.toast-container{position:fixed;bottom:20px;right:20px;display:flex;flex-direction:column;gap:8px;z-index:9999}
.toast{padding:10px 16px;border-radius:var(--radius-md);font-size:13px;font-weight:500;background:var(--bg-surface);border:1px solid var(--border);box-shadow:var(--shadow-lg);animation:slideUp .25s ease;display:flex;align-items:center;gap:8px;max-width:360px}
.toast.success{border-left:3px solid var(--green);color:var(--green)}
.toast.error{border-left:3px solid var(--red);color:var(--red)}
.toast.info{border-left:3px solid var(--accent);color:var(--accent)}
@keyframes slideUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
/* ═══════════════════════════════════════════════════════════════════════════
   Forms
   ═══════════════════════════════════════════════════════════════════════════ */
.form-group{margin-bottom:14px}
.form-group label{display:block;font-size:13px;font-weight:550;margin-bottom:4px;color:var(--text-secondary)}
.form-group input,.form-group select,.form-group textarea{width:100%}
.form-row{display:flex;gap:10px}
.form-row>*{flex:1}
/* ═══════════════════════════════════════════════════════════════════════════
   Utility
   ═══════════════════════════════════════════════════════════════════════════ */
.flex{display:flex}.flex-col{flex-direction:column}.items-center{align-items:center}.gap-4{gap:4px}.gap-8{gap:8px}.gap-12{gap:12px}.gap-16{gap:16px}.mt-8{margin-top:8px}.mt-12{margin-top:12px}.mt-16{margin-top:16px}.mb-8{margin-bottom:8px}.mb-12{margin-bottom:12px}.mb-16{margin-bottom:16px}.text-sm{font-size:13px}.text-xs{font-size:12px}.text-muted{color:var(--text-secondary)}.truncate{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.section-title{font-size:18px;font-weight:650;letter-spacing:-.3px;margin-bottom:16px;display:flex;align-items:center;gap:8px}
/* ═══════════════════════════════════════════════════════════════════════════
   Responsive
   ═══════════════════════════════════════════════════════════════════════════ */
@media(max-width:768px){
  .sidebar{position:fixed;left:-260px;z-index:100;height:100vh;transition:left var(--transition)}
  .sidebar.open{left:0}
  .sidebar-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:99}
  .sidebar-overlay.show{display:block}
  .content{padding:14px}
  .grid-2,.grid-3{grid-template-columns:1fr}
  .diff-grid{grid-template-columns:1fr}
  .topbar{padding:10px 14px}
}
</style>
</head>
<body>

<!-- ════ Sidebar ════ -->
<aside class="sidebar" id="sidebar">
  <div class="sidebar-brand">
    <h1>⚡ CodeSpine</h1>
    <div class="sub">Code Intelligence</div>
  </div>
  <nav class="sidebar-nav" id="nav">
    <button class="nav-item active" data-view="dashboard"><span class="icon">◉</span> Dashboard</button>
    <button class="nav-item" data-view="projects"><span class="icon">⊞</span> Projects</button>
    <button class="nav-item" data-view="search"><span class="icon">⌕</span> Search</button>
    <button class="nav-item" data-view="analysis"><span class="icon">⚙</span> Analysis</button>
    <button class="nav-item" data-view="ask"><span class="icon">✦</span> Ask AI</button>
    <button class="nav-item" data-view="diff"><span class="icon">⇆</span> Diff</button>
    <button class="nav-item" data-view="settings"><span class="icon">⚐</span> Settings</button>
  </nav>
  <div class="sidebar-footer">
    <span class="version" id="sidebar-version">v—</span>
    <span id="sidebar-status"><span class="badge-dot yellow"></span> Connecting...</span>
  </div>
</aside>
<div class="sidebar-overlay" id="sidebar-overlay" onclick="toggleSidebar()"></div>

<!-- ════ Main ════ -->
<div class="main">
  <header class="topbar">
    <div style="display:flex;align-items:center;gap:10px">
      <button class="btn-icon" onclick="toggleSidebar()" title="Toggle sidebar">☰</button>
      <span class="page-title" id="page-title">Dashboard</span>
    </div>
    <div class="topbar-actions">
      <button class="btn-icon" onclick="document.getElementById('refresh-btn').click()" title="Refresh data">↻</button>
      <button class="btn-icon" onclick="toggleTheme()" id="theme-btn" title="Toggle theme">🌙</button>
    </div>
  </header>
  <div class="content" id="content"><div class="loading"><div class="spinner"></div> Loading...</div></div>
</div>

<!-- ════ Toast container ════ -->
<div class="toast-container" id="toasts"></div>

<script>
/* ═══════════════════════════════════════════════════════════════════════════
   Application State
   ═══════════════════════════════════════════════════════════════════════════ */
const STATE = {
  projects: [],
  health: null,
  tasks: [],
  version: '',
  currentView: 'dashboard',
  searchResults: [],
  chatHistory: [],
  diffResults: null,
  analysisResults: null,
  communities: [],
  theme: localStorage.getItem('codespine-theme') || 'light',
  sortKey: null,
  sortAsc: true,
};

/* ═══════════════════════════════════════════════════════════════════════════
   Router
   ═══════════════════════════════════════════════════════════════════════════ */
function navigate(view) {
  STATE.currentView = view;
  history.replaceState(null, '', '#' + view);
  document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  const titles = {dashboard:'Dashboard',projects:'Projects',search:'Search',analysis:'Analysis',ask:'Ask AI',diff:'Diff',settings:'Settings'};
  document.getElementById('page-title').textContent = titles[view] || 'Dashboard';
  renderView(view);
}

function renderView(view) {
  const c = document.getElementById('content');
  if (typeof VIEWS[view] === 'function') VIEWS[view](c);
  else VIEWS.dashboard(c);
}

window.addEventListener('hashchange', () => {
  const view = location.hash.slice(1) || 'dashboard';
  if (STATE.currentView !== view) navigate(view);
});

/* ═══════════════════════════════════════════════════════════════════════════
   API Helpers
   ═══════════════════════════════════════════════════════════════════════════ */
async function api(url) {
  const r = await fetch(url);
  if (!r.ok) { const t = await r.text().catch(()=>''); throw new Error(t || r.statusText); }
  return r.json();
}
async function apiPost(url, body) {
  const r = await fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
  if (!r.ok) { const t = await r.text().catch(()=>''); throw new Error(t || r.statusText); }
  return r.json();
}

function toast(msg, type='info') {
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => { el.style.opacity='0'; el.style.transform='translateY(-8px)'; setTimeout(()=>el.remove(),250); }, 3500);
}

/* ═══════════════════════════════════════════════════════════════════════════
   Data fetching
   ═══════════════════════════════════════════════════════════════════════════ */
async function refreshAll() {
  try {
    const [status, projects, health, tasks] = await Promise.all([
      api('/api/status').catch(()=>({})),
      api('/api/projects').catch(()=>[]),
      api('/api/health').catch(()=>({})),
      api('/api/tasks').catch(()=>[]),
    ]);
    STATE.projects = projects;
    STATE.health = health;
    STATE.tasks = tasks;
    STATE.version = status.version || '';
    document.getElementById('sidebar-version').textContent = 'v' + (status.version || '—');
    const running = status.running || status.mcp_running;
    const statusEl = document.getElementById('sidebar-status');
    statusEl.innerHTML = running
      ? '<span class="badge-dot green"></span> Running'
      : '<span class="badge-dot yellow"></span> Stopped';
    renderView(STATE.currentView);
  } catch(e) {
    document.getElementById('sidebar-status').innerHTML = '<span class="badge-dot red"></span> Offline';
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
   View Renderers
   ═══════════════════════════════════════════════════════════════════════════ */
const VIEWS = {};

/* ─── Dashboard ─── */
VIEWS.dashboard = function(el) {
  const h = STATE.health;
  const summary = (h && h.summary) || {};
  const pCount = summary.project_count || STATE.projects.length || 0;
  const totalFiles = STATE.projects.reduce((s,p) => s + (p.files||0), 0);
  const totalMethods = STATE.projects.reduce((s,p) => s + (p.methods||0), 0);
  const totalCalls = STATE.projects.reduce((s,p) => s + (p.calls||0), 0);
  const totalEmbed = STATE.projects.reduce((s,p) => s + (p.embeddings||0), 0);
  const anomalies = summary.anomaly_count || 0;
  const critical = summary.critical_count || 0;
  const statusClass = critical ? 'red' : anomalies ? 'yellow' : 'green';
  const statusLabel = critical ? 'Critical' : anomalies ? 'Warning' : 'Healthy';
  const running = STATE.tasks.some(t => t.status === 'running');

  el.innerHTML = `
    <div class="section-title">◉ Dashboard Overview</div>
    <div class="grid-4 mb-16">
      <div class="card"><div class="card-value">${pCount}</div><div class="card-label">Projects</div></div>
      <div class="card"><div class="card-value">${totalFiles.toLocaleString()}</div><div class="card-label">Files Indexed</div></div>
      <div class="card"><div class="card-value">${totalMethods.toLocaleString()}</div><div class="card-label">Methods</div></div>
      <div class="card"><div class="card-value">${totalEmbed.toLocaleString()}</div><div class="card-label">Embeddings</div></div>
    </div>
    <div class="grid-2 mb-16">
      <div class="card">
        <div class="card-header"><span class="card-title">Index Health</span><span class="badge badge-${statusClass}"><span class="badge-dot ${statusClass}"></span> ${statusLabel}</span></div>
        <div class="grid-2" style="margin-top:8px">
          <div><span class="text-xs text-muted">Total Calls</span><div style="font-size:18px;font-weight:600">${totalCalls.toLocaleString()}</div></div>
          <div><span class="text-xs text-muted">Anomalies</span><div style="font-size:18px;font-weight:600;color:${anomalies ? 'var(--yellow)' : 'var(--green)'}">${anomalies}</div></div>
          <div><span class="text-xs text-muted">Critical</span><div style="font-size:18px;font-weight:600;color:${critical ? 'var(--red)' : 'var(--green)'}">${critical}</div></div>
          <div><span class="text-xs text-muted">Call Coverage</span><div style="font-size:18px;font-weight:600">${(summary.lowest_call_coverage != null ? (summary.lowest_call_coverage*100).toFixed(1) : '-')}%</div></div>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">System Status</span></div>
        <div style="margin-top:8px">
          <div class="flex items-center gap-8" style="margin-bottom:6px"><span class="badge-dot ${running?'green':'yellow'}"></span> <span style="font-size:13px">${running ? 'Background tasks running' : 'No active tasks'}</span></div>
          <div class="flex items-center gap-8" style="margin-bottom:6px"><span class="badge-dot green"></span> <span style="font-size:13px">Read replica ${STATE.projects.some(p=>p.snapshot_valid) ? 'available' : 'pending'}</span></div>
          <div class="flex items-center gap-8"><span class="badge-dot ${STATE.projects.length ? 'green' : 'yellow'}"></span> <span style="font-size:13px">${STATE.projects.length ? `${STATE.projects.length} project(s) indexed` : 'No projects indexed'}</span></div>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Recent Background Tasks</span></div>
      <div id="dash-tasks">${renderTaskList(STATE.tasks.slice(0,8))}</div>
    </div>
  `;
};

/* ─── Projects ─── */
VIEWS.projects = function(el) {
  const q = (STATE._projectFilter || '').toLowerCase();
  const filtered = STATE.projects.filter(p => (`${p.id||''} ${p.path||''} ${p.project_state||''}`).toLowerCase().includes(q));
  const sorted = sortList(filtered, STATE.sortKey, STATE.sortAsc);

  el.innerHTML = `
    <div class="section-title">⊞ Projects <span class="text-sm text-muted" style="font-weight:400">(${STATE.projects.length})</span></div>
    <div class="flex gap-8 mb-12" style="flex-wrap:wrap;align-items:center">
      <input id="proj-filter" placeholder="Filter by id, path, or state..." style="flex:1;min-width:160px" value="${STATE._projectFilter||''}">
      <button class="btn btn-primary" onclick="refreshAll()">↻ Refresh</button>
      <button class="btn" onclick="navigate('search')">🔍 Search</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          ${['id','project_state','shard','files','methods','calls','embeddings','last_good_snapshot_at'].map(k =>
            `<th class="${STATE.sortKey===k?'sorted':''}" onclick="sortProjects('${k}')">${k.replace(/_/g,' ')}<span class="sort">▾</span></th>`
          ).join('')}
          <th>Actions</th>
        </tr></thead>
        <tbody>${sorted.length ? sorted.map(p => `
          <tr>
            <td><strong>${esc(p.id)}</strong>${p.last_error ? `<div class="text-xs text-muted" style="color:var(--red)">${esc(p.last_error)}</div>` : ''}</td>
            <td>${badge(p.project_state)}</td>
            <td>${p.shard}</td>
            <td>${p.files}</td>
            <td>${p.methods}</td>
            <td>${p.calls}</td>
            <td>${p.embeddings}</td>
            <td class="text-xs text-muted">${p.last_good_snapshot_at ? new Date(p.last_good_snapshot_at*1000).toLocaleString() : '-'}</td>
            <td>
              <div class="btn-group">
                <button class="btn btn-sm" onclick="apiPost('/api/repair',{project_id:'${p.id}',mode:'auto'}).then(r=>{toast('Repair started: '+r.task_id,'success');refreshAll()})">Repair</button>
                <button class="btn btn-sm btn-danger" onclick="if(confirm('Reindex ${esc(p.id)}?'))apiPost('/api/repair',{project_id:'${p.id}',mode:'full'}).then(r=>{toast('Reindex started: '+r.task_id,'success');refreshAll()})">Reindex</button>
              </div>
            </td>
          </tr>
        `).join('') : `<tr><td colspan="10" style="text-align:center;padding:30px;color:var(--text-muted)">${STATE.projects.length ? 'No projects match filter' : 'No projects indexed. Run codespine analyse <path> to get started.'}</td></tr>`}</tbody>
      </table>
    </div>
  `;
  document.getElementById('proj-filter')?.addEventListener('input', e => {
    STATE._projectFilter = e.target.value;
    VIEWS.projects(el);
  });
};

function sortProjects(key) {
  if (STATE.sortKey === key) STATE.sortAsc = !STATE.sortAsc;
  else { STATE.sortKey = key; STATE.sortAsc = false; }
  VIEWS.projects(document.getElementById('content'));
}
function sortList(arr, key, asc) {
  if (!key) return arr;
  return [...arr].sort((a,b) => {
    const va = a[key], vb = b[key];
    if (typeof va === 'number' && typeof vb === 'number') return asc ? va - vb : vb - va;
    return asc ? String(va||'').localeCompare(String(vb||'')) : String(vb||'').localeCompare(String(va||''));
  });
}

/* ─── Search ─── */
VIEWS.search = function(el) {
  const q = STATE._searchQuery || '';
  const project = STATE._searchProject || '';
  const results = STATE.searchResults;
  const projects = STATE.projects;

  el.innerHTML = `
    <div class="section-title">⌕ Hybrid Search</div>
    <div class="card mb-16">
      <div class="form-row" style="align-items:end">
        <div class="form-group" style="flex:3">
          <label>Search query</label>
          <input id="search-q" placeholder="Class name, method, or natural language query..." value="${esc(q)}">
        </div>
        <div class="form-group" style="flex:1">
          <label>Project (optional)</label>
          <select id="search-project">
            <option value="">All projects</option>
            ${projects.map(p => `<option value="${esc(p.id)}" ${p.id===project?'selected':''}>${esc(p.id)}</option>`).join('')}
          </select>
        </div>
        <div class="form-group">
          <label>&nbsp;</label>
          <button class="btn btn-primary btn-lg" id="search-btn">🔍 Search</button>
        </div>
      </div>
    </div>
    <div id="search-results">${results.length ? `
      <div class="flex items-center gap-12 mb-12 text-sm text-muted">
        <span>${results.length} result(s)</span>
        <button class="btn btn-sm btn-ghost" onclick="STATE.searchResults=[];VIEWS.search(document.getElementById('content'))">Clear</button>
      </div>
      ${results.map(r => `
        <div class="result-card">
          <div class="title">${esc(r.name||r.fqname||r.id)}</div>
          <div class="meta">
            <span class="badge badge-${r.confidence==='high'?'green':r.confidence==='medium'?'yellow':'gray'}">${r.confidence||'—'}</span>
            <span>${r.kind||'—'}</span>
            <span class="truncate" style="max-width:300px">${esc(r.file_path||'')}</span>
            <span>line ${r.line||'—'}</span>
            <span>score: ${typeof r.score==='number' ? r.score.toFixed(3) : '—'}</span>
          </div>
          ${r.snippet ? `<div class="snippet">${esc(r.snippet)}</div>` : ''}
        </div>
      `).join('')}
    ` : `<div class="empty-state"><div class="icon">⌕</div><div class="title">Search your codebase</div><div class="desc">Enter a class name, method, or natural language query above</div></div>`}</div>
  `;

  document.getElementById('search-btn')?.addEventListener('click', doSearch);
  document.getElementById('search-q')?.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
};

async function doSearch() {
  const q = document.getElementById('search-q')?.value?.trim();
  if (!q) return;
  STATE._searchQuery = q;
  STATE._searchProject = document.getElementById('search-project')?.value || '';
  const el = document.getElementById('content');
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Searching...</div>';
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(q)}&project=${encodeURIComponent(STATE._searchProject)}`);
    STATE.searchResults = data.results || data || [];
    VIEWS.search(el);
  } catch(e) {
    el.innerHTML = `<div class="error-banner">✗ Search failed: ${esc(e.message)}</div>`;
  }
}

/* ─── Analysis ─── */
VIEWS.analysis = function(el) {
  const tab = STATE._analysisTab || 'impact';
  el.innerHTML = `
    <div class="section-title">⚙ Code Analysis</div>
    <div class="tabs">
      <button class="tab ${tab==='impact'?'active':''}" onclick="switchAnalysis('impact')">Impact Analysis</button>
      <button class="tab ${tab==='deadcode'?'active':''}" onclick="switchAnalysis('deadcode')">Dead Code</button>
      <button class="tab ${tab==='communities'?'active':''}" onclick="switchAnalysis('communities')">Communities</button>
    </div>
    <div id="analysis-content">${renderAnalysisTab(tab)}</div>
  `;
};
function switchAnalysis(tab) { STATE._analysisTab = tab; VIEWS.analysis(document.getElementById('content')); }

function renderAnalysisTab(tab) {
  const projects = STATE.projects;
  if (tab === 'impact') return `
    <div class="card">
      <div class="form-row" style="align-items:end">
        <div class="form-group" style="flex:2">
          <label>Symbol (class or method name)</label>
          <input id="impact-symbol" placeholder="e.g. TransactionService, ScreeningStateMachine">
        </div>
        <div class="form-group" style="flex:1">
          <label>Project</label>
          <select id="impact-project"><option value="">All</option>${projects.map(p=>`<option value="${esc(p.id)}">${esc(p.id)}</option>`).join('')}</select>
        </div>
        <div class="form-group">
          <label>Depth</label>
          <input id="impact-depth" type="number" value="3" min="1" max="10" style="width:70px">
        </div>
        <div class="form-group">
          <label>&nbsp;</label>
          <button class="btn btn-primary" onclick="doImpact()">Analyze</button>
        </div>
      </div>
    </div>
    <div id="impact-results"></div>
  `;
  if (tab === 'deadcode') return `
    <div class="card">
      <div class="form-row" style="align-items:end">
        <div class="form-group"><label>Project</label><select id="dc-project"><option value="">All</option>${projects.map(p=>`<option value="${esc(p.id)}">${esc(p.id)}</option>`).join('')}</select></div>
        <div class="form-group"><label>Limit</label><input id="dc-limit" type="number" value="200" style="width:80px"></div>
        <div class="form-group"><label>Strict</label><select id="dc-strict"><option value="false">No</option><option value="true">Yes</option></select></div>
        <div class="form-group"><label>&nbsp;</label><button class="btn btn-primary" onclick="doDeadCode()">Detect</button></div>
      </div>
    </div>
    <div id="dc-results"></div>
  `;
  if (tab === 'communities') return `
    <div class="card">
      <div class="form-row" style="align-items:end">
        <div class="form-group"><label>Project (optional)</label><select id="comm-project"><option value="">All</option>${projects.map(p=>`<option value="${esc(p.id)}">${esc(p.id)}</option>`).join('')}</select></div>
        <div class="form-group"><label>&nbsp;</label><button class="btn btn-primary" onclick="doCommunities()">List Communities</button></div>
      </div>
    </div>
    <div id="comm-results"></div>
  `;
  return '';
}

async function doImpact() {
  const sym = document.getElementById('impact-symbol')?.value?.trim();
  if (!sym) return toast('Enter a symbol name', 'error');
  const project = document.getElementById('impact-project')?.value || '';
  const depth = document.getElementById('impact-depth')?.value || '3';
  const el = document.getElementById('impact-results');
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Analyzing...</div>';
  try {
    const data = await api(`/api/impact?symbol=${encodeURIComponent(sym)}&project=${encodeURIComponent(project)}&depth=${depth}`);
    const callers = data.callers || data.calls || [];
    const callees = data.callees || [];
    const info = data.symbol_info || {};
    el.innerHTML = `
      <div class="card mt-12">
        <div class="card-header"><span class="card-title">Impact: ${esc(sym)}</span> <span class="text-xs text-muted">${info.fqname||''}</span></div>
        ${info.description ? `<div class="text-sm mb-12" style="padding:8px 12px;background:var(--bg-code);border-radius:var(--radius-sm)">${esc(info.description)}</div>` : ''}
        <div class="grid-2">
          <div>
            <div class="card-title mb-8" style="color:var(--red)">← Callers (${callers.length})</div>
            ${callers.length ? callers.slice(0,50).map(c => `<div class="result-card" style="margin-bottom:4px;padding:8px 12px"><span class="text-sm">${esc(c.name||c.symbol||c.source||'')}</span> <span class="text-xs text-muted">${esc(c.location||c.file_path||'')}</span></div>`).join('') : '<div class="text-sm text-muted">No callers found</div>'}
          </div>
          <div>
            <div class="card-title mb-8" style="color:var(--green)">→ Callees (${callees.length})</div>
            ${callees.length ? callees.slice(0,50).map(c => `<div class="result-card" style="margin-bottom:4px;padding:8px 12px"><span class="text-sm">${esc(c.name||c.symbol||c.target||'')}</span> <span class="text-xs text-muted">${esc(c.location||c.file_path||'')}</span></div>`).join('') : '<div class="text-sm text-muted">No callees found</div>'}
          </div>
        </div>
      </div>
    `;
  } catch(e) { el.innerHTML = `<div class="error-banner mt-12">✗ ${esc(e.message)}</div>`; }
}

async function doDeadCode() {
  const project = document.getElementById('dc-project')?.value || '';
  const limit = document.getElementById('dc-limit')?.value || '200';
  const strict = document.getElementById('dc-strict')?.value === 'true';
  const el = document.getElementById('dc-results');
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Scanning...</div>';
  try {
    const data = await api(`/api/deadcode?project=${encodeURIComponent(project)}&limit=${limit}&strict=${strict}`);
    const items = data.results || data || [];
    el.innerHTML = `
      <div class="mt-12">
        <div class="flex items-center gap-8 mb-12 text-sm text-muted"><span>${items.length} potential dead code item(s)</span></div>
        ${items.length ? items.slice(0,100).map(item => `
          <div class="result-card" style="margin-bottom:4px">
            <div class="flex items-center gap-8">
              <span style="font-weight:550">${esc(item.name||item.symbol||'')}</span>
              <span class="text-xs text-muted">${esc(item.kind||'')}</span>
              <span class="badge badge-${item.confidence === 'high' ? 'red' : 'yellow'}">${item.confidence||'low'}</span>
            </div>
            <div class="meta">${esc(item.location||item.file_path||'')} ${item.line ? ':'+item.line : ''}</div>
            ${item.reason ? `<div class="text-xs text-muted mt-8">${esc(item.reason)}</div>` : ''}
          </div>
        `).join('') : '<div class="empty-state"><div class="icon">✓</div><div class="title">No dead code detected</div></div>'}
      </div>
    `;
  } catch(e) { el.innerHTML = `<div class="error-banner mt-12">✗ ${esc(e.message)}</div>`; }
}

async function doCommunities() {
  const project = document.getElementById('comm-project')?.value || '';
  const el = document.getElementById('comm-results');
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading communities...</div>';
  try {
    const data = await api(`/api/communities?project=${encodeURIComponent(project)}`);
    const items = data.results || data || [];
    el.innerHTML = `
      <div class="mt-12">
        <div class="flex items-center gap-8 mb-12 text-sm text-muted"><span>${items.length} communit${items.length===1?'y':'ies'}</span></div>
        ${items.length ? items.slice(0,50).map(c => `
          <div class="result-card" style="margin-bottom:4px">
            <div class="flex items-center gap-8">
              <span style="font-weight:550">${esc(c.label||c.id||'Community')}</span>
              <span class="badge badge-purple">cohesion: ${typeof c.cohesion==='number' ? c.cohesion.toFixed(2) : c.cohesion||'—'}</span>
            </div>
            <div class="text-xs text-muted">${c.id||''}</div>
          </div>
        `).join('') : '<div class="empty-state"><div class="icon">◉</div><div class="title">No communities found</div><div class="desc">Run codespine analyse --deep to compute communities</div></div>'}
      </div>
    `;
  } catch(e) { el.innerHTML = `<div class="error-banner mt-12">✗ ${esc(e.message)}</div>`; }
}

/* ─── Ask AI ─── */
VIEWS.ask = function(el) {
  const history = STATE.chatHistory;
  const projects = STATE.projects;
  el.innerHTML = `
    <div class="section-title">✦ Ask AI</div>
    <div class="card chat-container">
      <div class="chat-messages" id="chat-msgs">
        ${history.length ? history.map((m,i) => `
          <div class="chat-bubble ${m.role}">${esc(m.content)}${m.citations ? `<div class="citations">${m.citations}</div>` : ''}</div>
        `).join('') : `<div class="empty-state" style="padding:30px"><div class="icon">✦</div><div class="title">Ask about your codebase</div><div class="desc">Natural language questions about architecture, dependencies, and logic</div></div>`}
      </div>
      <div class="chat-input-row">
        <select id="ask-project" style="max-width:160px;flex-shrink:0">
          <option value="">All projects</option>
          ${projects.map(p => `<option value="${esc(p.id)}">${esc(p.id)}</option>`).join('')}
        </select>
        <input id="ask-input" placeholder="Ask about your codebase..." value="${STATE._askInput||''}">
        <button class="btn btn-primary" onclick="doAsk()">Ask</button>
      </div>
    </div>
  `;
  document.getElementById('ask-input')?.addEventListener('keydown', e => { if (e.key === 'Enter') doAsk(); });
  setTimeout(() => { const c = document.getElementById('chat-msgs'); if(c) c.scrollTop = c.scrollHeight; }, 50);
};

async function doAsk() {
  const input = document.getElementById('ask-input');
  const q = input?.value?.trim();
  if (!q) return;
  STATE._askInput = '';
  const project = document.getElementById('ask-project')?.value || '';
  const el = document.getElementById('chat-msgs');
  STATE.chatHistory.push({role:'question', content: q});
  const msgEl = document.createElement('div');
  msgEl.className = 'chat-bubble question';
  msgEl.textContent = q;
  el.appendChild(msgEl);
  el.scrollTop = el.scrollHeight;
  const answerEl = document.createElement('div');
  answerEl.className = 'chat-bubble answer';
  answerEl.innerHTML = '<div class="loading"><div class="spinner"></div> Thinking...</div>';
  el.appendChild(answerEl);
  el.scrollTop = el.scrollHeight;
  input.value = '';
  try {
    const data = await api(`/api/ask?question=${encodeURIComponent(q)}&project=${encodeURIComponent(project)}`);
    const answerText = data.answer || data.result || JSON.stringify(data);
    const citations = data.citations || data.sources || [];
    STATE.chatHistory.push({role:'answer', content: answerText, citations: citations.length ? 'Sources: ' + citations.join(', ') : ''});
    answerEl.innerHTML = esc(answerText) + (citations.length ? `<div class="citations">Sources: ${citations.map(c=>esc(c)).join(', ')}</div>` : '');
    el.scrollTop = el.scrollHeight;
  } catch(e) {
    answerEl.innerHTML = `<div style="color:var(--red)">✗ ${esc(e.message)}</div>`;
  }
}

/* ─── Diff ─── */
VIEWS.diff = function(el) {
  const projects = STATE.projects;
  const dr = STATE.diffResults || null;
  el.innerHTML = `
    <div class="section-title">⇆ Branch Diff</div>
    <div class="card mb-16">
      <div class="form-row" style="align-items:end">
        <div class="form-group"><label>Base ref</label><input id="diff-base" placeholder="main" value="${esc(STATE._diffBase||'')}"></div>
        <div class="form-group"><label>Head ref</label><input id="diff-head" placeholder="feature-branch" value="${esc(STATE._diffHead||'')}"></div>
        <div class="form-group"><label>Project</label><select id="diff-project"><option value="">Select project</option>${projects.filter(p=>p.path).map(p=>`<option value="${esc(p.path)}">${esc(p.id)}</option>`).join('')}</select></div>
        <div class="form-group"><label>&nbsp;</label><button class="btn btn-primary" onclick="doDiff()">Compare</button></div>
      </div>
    </div>
    <div id="diff-results">${dr ? `
      <div class="flex items-center gap-12 mb-12 text-sm text-muted">
        <span>${dr.base || '?'} → ${dr.head || '?'}</span>
        <span>+${(dr.added||[]).length} −${(dr.removed||[]).length} ~${(dr.modified||[]).length}</span>
        ${dr.warnings ? dr.warnings.map(w => `<span class="badge badge-yellow">${esc(w)}</span>`).join('') : ''}
      </div>
      <div class="diff-grid">
        <div><div class="text-xs text-muted mb-4">Added (${(dr.added||[]).length})</div>${(dr.added||[]).slice(0,20).map(a => `<div class="diff-added" style="padding:4px 8px;border-radius:4px;margin-bottom:2px;font-size:12px">+ ${esc(a.file||a.name||'')}</div>`).join('')||'<div class="text-xs text-muted">None</div>'}</div>
        <div><div class="text-xs text-muted mb-4">Removed (${(dr.removed||[]).length})</div>${(dr.removed||[]).slice(0,20).map(r => `<div class="diff-removed" style="padding:4px 8px;border-radius:4px;margin-bottom:2px;font-size:12px">− ${esc(r.file||r.name||'')}</div>`).join('')||'<div class="text-xs text-muted">None</div>'}</div>
      </div>
      ${(dr.modified||[]).length ? `<div class="mt-12"><div class="text-xs text-muted mb-4">Modified (${dr.modified.length})</div>${dr.modified.slice(0,20).map(m => `<div style="padding:4px 8px;border-radius:4px;margin-bottom:2px;font-size:12px;background:var(--bg-code)">~ ${esc(m.file||m.name||'')}</div>`).join('')}</div>` : ''}
    ` : `<div class="empty-state"><div class="icon">⇆</div><div class="title">Compare branches</div><div class="desc">Enter two git refs and a project to see code-level differences</div></div>`}
  `;
};

async function doDiff() {
  const base = document.getElementById('diff-base')?.value?.trim();
  const head = document.getElementById('diff-head')?.value?.trim();
  const project = document.getElementById('diff-project')?.value?.trim();
  if (!base || !head || !project) return toast('Fill in base, head, and project', 'error');
  STATE._diffBase = base; STATE._diffHead = head;
  const el = document.getElementById('diff-results');
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Comparing...</div>';
  try {
    const data = await api(`/api/diff?base=${encodeURIComponent(base)}&head=${encodeURIComponent(head)}&project=${encodeURIComponent(project)}`);
    STATE.diffResults = data;
    VIEWS.diff(document.getElementById('content'));
  } catch(e) {
    el.innerHTML = `<div class="error-banner">✗ ${esc(e.message)}</div>`;
  }
}

/* ─── Settings ─── */
VIEWS.settings = function(el) {
  const theme = STATE.theme;
  el.innerHTML = `
    <div class="section-title">⚐ Settings</div>
    <div class="card mb-12">
      <div class="card-header"><span class="card-title">Appearance</span></div>
      <div class="flex items-center gap-12">
        <span class="text-sm">Theme</span>
        <button class="btn ${theme==='light'?'btn-primary':'btn'}" onclick="setTheme('light')">☀ Light</button>
        <button class="btn ${theme==='dark'?'btn-primary':'btn'}" onclick="setTheme('dark')">🌙 Dark</button>
      </div>
    </div>
    <div class="card mb-12">
      <div class="card-header"><span class="card-title">Server</span></div>
      <div class="text-sm" style="line-height:2">
        <div><span class="text-muted">Version:</span> v${esc(STATE.version||'—')}</div>
        <div><span class="text-muted">Projects:</span> ${STATE.projects.length}</div>
        <div><span class="text-muted">Auto-refresh:</span> Every 5 seconds</div>
        <div><span class="text-muted">API:</span> <code>/api/status /api/projects /api/health /api/tasks /api/search /api/impact /api/deadcode /api/ask /api/diff /api/communities</code></div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Data</span></div>
      <div class="btn-group">
        <button class="btn" onclick="refreshAll();toast('Refreshed','success')">↻ Refresh All Data</button>
        <button class="btn btn-danger" onclick="if(confirm('Clear all local state?')){fetch('/api/reset',{method:'POST'}).then(()=>{toast('Reset initiated','info');refreshAll()})}">Force Reset</button>
      </div>
    </div>
  `;
};

/* ═══════════════════════════════════════════════════════════════════════════
   Theme
   ═══════════════════════════════════════════════════════════════════════════ */
function setTheme(t) {
  STATE.theme = t;
  document.documentElement.dataset.theme = t;
  localStorage.setItem('codespine-theme', t);
  document.getElementById('theme-btn').textContent = t === 'dark' ? '☀' : '🌙';
}
function toggleTheme() { setTheme(STATE.theme === 'dark' ? 'light' : 'dark'); }

/* ═══════════════════════════════════════════════════════════════════════════
   Sidebar toggle (mobile)
   ═══════════════════════════════════════════════════════════════════════════ */
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebar-overlay').classList.toggle('show');
}

/* ═══════════════════════════════════════════════════════════════════════════
   Utilities
   ═══════════════════════════════════════════════════════════════════════════ */
function esc(s) { if (typeof s !== 'string') return s||''; return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function badge(state) {
  const m = {ready:'green',succeeded:'green',healthy:'green',
    indexing:'blue',enriching:'blue',running:'blue',queued:'blue',
    partial:'yellow',degraded:'yellow',warning:'yellow',
    failed:'red',repair_required:'red',critical:'red',error:'red'};
  const c = m[state] || 'gray';
  return `<span class="badge badge-${c}">${state||'—'}</span>`;
}
function renderTaskList(tasks) {
  if (!tasks.length) return '<div class="text-sm text-muted">No background tasks.</div>';
  return tasks.map(t => `
    <div class="flex items-center gap-8" style="padding:6px 0;border-bottom:1px solid var(--border-light);font-size:13px">
      ${badge(t.status)}
      <span class="truncate" style="flex:1">${esc(t.last_phase||t.phase||t.kind||'')}</span>
      <span class="text-xs text-muted">${t.path ? esc(t.path).substring(0,30)+'…' : ''}</span>
    </div>
  `).join('');
}

/* ═══════════════════════════════════════════════════════════════════════════
   Init
   ═══════════════════════════════════════════════════════════════════════════ */
setTheme(STATE.theme);
const initialView = location.hash.slice(1) || 'dashboard';
navigate(initialView);
refreshAll();
setInterval(refreshAll, 5000);
</script>
</body>
</html>"""
