"""Derive service/static/app.html from web/index.html (auth header, settings modal, CSRF header, quotas).

    python scripts/build_app_html.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "phonepilot"
src = (ROOT / "web" / "index.html").read_text(encoding="utf-8")


def sub(s: str, old: str, new: str) -> str:
    if old not in s:
        raise SystemExit(f"anchor not found: {old[:60]!r}")
    return s.replace(old, new, 1)


s = src
s = sub(s, '''  <span class="spacer"></span>
  <span class="chip">balance <b id="balance">—</b></span>
</header>''', '''  <span class="chip">today <b id="quota">—</b></span>
  <span class="spacer"></span>
  <span class="chip"><b id="who">—</b></span>
  <button class="btn" onclick="openSettings()">Settings</button>
  <button class="btn" onclick="logout()">Sign out</button>
</header>
<div id="settings" class="modal" hidden>
  <div class="modal-card">
    <h2 style="margin:0 0 4px">Your keys</h2>
    <p style="color:var(--muted);margin:0 0 14px;font-size:13px">Stored encrypted on the server, never shown again, never sent to the browser. Your phone and model run on <em>your</em> accounts.</p>
    <div class="krow"><b>Phone Harness</b><span id="k-phone_harness" class="khint"></span><input id="in-phone_harness" placeholder="pck_…"><button class="btn" onclick="saveKey('phone_harness')">Save</button><button class="btn danger" onclick="delKey('phone_harness')">✕</button></div>
    <div class="krow"><b>Gemini</b><span id="k-gemini" class="khint"></span><input id="in-gemini" placeholder="AIza…"><button class="btn" onclick="saveKey('gemini')">Save</button><button class="btn danger" onclick="delKey('gemini')">✕</button></div>
    <div class="krow"><b>Anthropic</b><span id="k-anthropic" class="khint"></span><input id="in-anthropic" placeholder="sk-ant-…"><button class="btn" onclick="saveKey('anthropic')">Save</button><button class="btn danger" onclick="delKey('anthropic')">✕</button></div>
    <div id="admin" hidden style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px"><b>Admin</b> <button class="btn" onclick="invite()">New invite code</button> <code id="invite-code" style="color:var(--accent)"></code></div>
    <div class="err" id="k-err" style="color:var(--bad);min-height:18px;margin-top:8px;font-size:13px"></div>
    <div style="text-align:right;margin-top:10px"><button class="btn" onclick="closeSettings()">Close</button></div>
  </div>
</div>''')
s = sub(s, '  #lightbox{', '''  .modal{position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;z-index:30}
  .modal-card{width:min(640px,94vw);background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px}
  .krow{display:grid;grid-template-columns:110px 130px 1fr auto auto;gap:8px;align-items:center;margin:8px 0}
  .krow input{background:var(--bg);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:7px 9px;font:13px var(--mono)}
  .khint{font:12px var(--mono);color:var(--muted)}
  #lightbox{''')
s = s.replace("headers:{'Content-Type':'application/json'}", "headers:{'Content-Type':'application/json','X-PhonePilot':'1'}")
s = sub(s, "es.onerror = () => { setTimeout(()=>{ es.close(); connect(); }, 2000); };",
        "es.onerror = () => { fetch('/api/state').then(r=>{ if (r.status===401) location.href='/login'; }); setTimeout(()=>{ es.close(); connect(); }, 2000); };")
s = sub(s, "  const j = await r.json();\n  if (!r.ok) render({kind:'log', text:'✗ '+(j.error||r.status)});",
        "  if (r.status===401){ location.href='/login'; return {}; }\n  const j = await r.json();\n  if (!r.ok) render({kind:'log', text:'✗ '+(j.error||r.status)});")
s = sub(s, "  $('balance').textContent = s.balance_cents==null ? '—' : ('$'+(s.balance_cents/100).toFixed(2)+' · '+(s.price_cents_per_minute||0)+'¢/min');",
        """  if (s.user){ $('who').textContent = s.user.email; $('admin').hidden = !s.user.is_admin; }
  if (s.quota){ $('quota').textContent = s.quota.phone_minutes_used_today+' / '+s.quota.daily_phone_minutes+' phone-min'; }
  if (s.keys){ for (const p of ['phone_harness','gemini','anthropic']){ const k=s.keys[p]; $('k-'+p).textContent = k.own ? ('yours: '+k.own) : (k.pool ? 'using shared key' : 'not set'); } }""")
s = sub(s, "connect();\n</script>", """function openSettings(){ $('settings').hidden=false; }
function closeSettings(){ $('settings').hidden=true; $('k-err').textContent=''; }
async function saveKey(p){ const v=$('in-'+p).value.trim(); if(!v) return; const j=await post('/api/keys',{provider:p,value:v}); if(j.error){ $('k-err').textContent=j.error; return; } $('in-'+p).value=''; $('k-err').textContent=''; fetch('/api/state').then(r=>r.json()).then(applyState); }
async function delKey(p){ await post('/api/keys/delete',{provider:p}); fetch('/api/state').then(r=>r.json()).then(applyState); }
async function invite(){ const j=await post('/api/admin/invite',{}); if(j.invite) $('invite-code').textContent=j.invite; }
async function logout(){ await post('/api/auth/logout',{}); location.href='/login'; }
connect();
</script>""")
assert "$('balance')" not in s
out = ROOT / "service" / "static" / "app.html"
out.write_text(s, encoding="utf-8", newline="\n")
print(f"wrote {out} ({len(s.splitlines())} lines)")
