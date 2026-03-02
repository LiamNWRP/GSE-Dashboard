from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from dateutil.relativedelta import relativedelta
from flask import Flask, g, redirect, render_template_string, request, url_for
from waitress import serve

APP = Flask(__name__)

APP_NAME = "Freeman Jet Center PUB"
DB_PATH = Path(__file__).with_name("freeman_fbo.sqlite")

DUE_SOON_DAYS = 7  # “Due soon” window


# ------------------ Interval logic (true calendar months/quarters/years) ------------------
def add_interval(d: date, interval: str, every: int) -> date:
    every = max(1, int(every))
    if interval == "daily":
        return d + timedelta(days=every)
    if interval == "weekly":
        return d + timedelta(weeks=every)
    if interval == "monthly":
        return d + relativedelta(months=+every)
    if interval == "quarterly":
        return d + relativedelta(months=+(3 * every))
    if interval == "yearly":
        return d + relativedelta(years=+every)
    raise ValueError(f"Unknown interval: {interval}")


def status_of(next_due: Optional[date]) -> str:
    if next_due is None:
        return "NO DATE"
    today = date.today()
    if next_due < today:
        return "OVERDUE"
    if next_due == today:
        return "DUE TODAY"
    if next_due <= today + timedelta(days=DUE_SOON_DAYS):
        return "DUE SOON"
    return "OK"


def status_class(status: str) -> str:
    return {
        "OVERDUE": "pill pill-red",
        "DUE TODAY": "pill pill-orange",
        "DUE SOON": "pill pill-yellow",
        "OK": "pill pill-green",
        "NO DATE": "pill",
    }.get(status, "pill")


# ------------------ DB helpers ------------------
def db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        # Better concurrency for multi-PC LAN usage
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        g.db = conn
    return g.db


@APP.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON;")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        asset_tag TEXT,
        group_name TEXT NOT NULL DEFAULT 'GSE',   -- Fuel Farm/Fuel Truck/GSE/Crew Cars/etc.
        location TEXT,
        status TEXT NOT NULL DEFAULT 'In Service',
        notes TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        schedule_type TEXT NOT NULL,        -- 'MAINT' or 'ATA103'
        interval TEXT NOT NULL,             -- daily/weekly/monthly/quarterly/yearly
        every INTEGER NOT NULL DEFAULT 1,
        start_date DATE NOT NULL,
        last_completed DATE,
        next_due DATE,
        instructions TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE CASCADE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS completions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_id INTEGER NOT NULL,
        completed_at TEXT NOT NULL,         -- ISO datetime
        completed_date DATE NOT NULL,       -- ISO date
        tech TEXT NOT NULL,                 -- no login; tech initials/name required
        result TEXT NOT NULL DEFAULT 'PASS',-- PASS/FAIL/NA
        notes TEXT,
        FOREIGN KEY(schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_assets_group ON assets(group_name);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sched_asset ON schedules(asset_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sched_type ON schedules(schedule_type);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sched_next_due ON schedules(next_due);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_comp_sched ON completions(schedule_id);")

    conn.commit()
    conn.close()


def recompute_next_due(schedule_row) -> Optional[date]:
    start = date.fromisoformat(schedule_row["start_date"])
    last = schedule_row["last_completed"]
    if not last:
        return start
    last_d = date.fromisoformat(last)
    return add_interval(last_d, schedule_row["interval"], schedule_row["every"])


def refresh_next_due_all():
    """
    Keeps next_due accurate. Cheap for ~50 assets.
    """
    cur = db().cursor()
    scheds = cur.execute("SELECT * FROM schedules WHERE active=1").fetchall()
    for s in scheds:
        nd = recompute_next_due(s)
        cur.execute("UPDATE schedules SET next_due=? WHERE id=?", (nd.isoformat() if nd else None, s["id"]))
    db().commit()


# ------------------ UI Shell ------------------
BASE_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{{ title }}</title>
  <style>
    :root{
      --bg:#070b14;
      --card:#0d1630;
      --card2:#0b1225;
      --text:#e5e7eb;
      --muted:#9ca3af;
      --line:#203051;
      --gold:#cdb56a;
      --accent:#60a5fa;
      --red:#ef4444;
      --orange:#f97316;
      --yellow:#fbbf24;
      --green:#22c55e;
    }
    body{margin:0;font-family:Segoe UI,Arial,sans-serif;background:radial-gradient(1200px 600px at 30% -10%, rgba(205,181,106,.18), transparent 55%), var(--bg);color:var(--text);}
    a{color:var(--accent);text-decoration:none}
    .topbar{
      position:sticky;top:0;z-index:10;
      background:rgba(7,11,20,.92);backdrop-filter: blur(10px);
      border-bottom:1px solid var(--line);
      padding:12px 16px;display:flex;align-items:center;gap:16px;
    }
    .brand{display:flex;align-items:center;gap:10px;min-width:260px}
    .brand-logo{height:32px;width:auto;display:block;filter: drop-shadow(0 8px 18px rgba(0,0,0,.35))}
    .brand-title{font-weight:900;letter-spacing:.2px}
    .brand-sub{font-size:12px;color:var(--muted);margin-top:2px}
    .nav{display:flex;gap:10px;flex-wrap:wrap}
    .nav a{
      color:var(--text);opacity:.88;
      padding:8px 10px;border-radius:10px;border:1px solid transparent;
    }
    .nav a.active{
      opacity:1;border-color:rgba(205,181,106,.45);
      background:rgba(205,181,106,.12);
      color:var(--gold);
    }
    .wrap{max-width:1280px;margin:0 auto;padding:18px}
    .grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}
    .card{
      background:linear-gradient(180deg, rgba(13,22,48,.88), rgba(11,18,37,.88));
      border:1px solid rgba(32,48,81,.95);
      border-radius:16px;padding:14px;
      box-shadow: 0 10px 30px rgba(0,0,0,.25);
    }
    .card h3{margin:0 0 10px 0;font-size:13px;color:var(--muted);font-weight:700;letter-spacing:.2px;text-transform:uppercase}
    .metric{font-size:34px;font-weight:900;line-height:1}
    .sub{color:var(--muted);font-size:12px;margin-top:6px}
    .pill{display:inline-block;padding:4px 10px;border-radius:999px;border:1px solid var(--line);font-size:12px;color:var(--text)}
    .pill-red{border-color:rgba(239,68,68,.5);background:rgba(239,68,68,.15)}
    .pill-orange{border-color:rgba(249,115,22,.5);background:rgba(249,115,22,.15)}
    .pill-yellow{border-color:rgba(251,191,36,.5);background:rgba(251,191,36,.12)}
    .pill-green{border-color:rgba(34,197,94,.5);background:rgba(34,197,94,.12)}
    table{width:100%;border-collapse:collapse;margin-top:10px}
    th,td{border-bottom:1px solid rgba(32,48,81,.9);padding:10px 8px;text-align:left;font-size:13px;vertical-align:top}
    th{color:var(--muted);font-weight:700}
    tr:hover td{background:rgba(96,165,250,.06)}
    .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
    .btn{
      display:inline-block;background:rgba(205,181,106,.14);
      border:1px solid rgba(205,181,106,.42);
      color:var(--text);
      padding:8px 10px;border-radius:12px;font-size:13px;font-weight:700;
    }
    .btn:hover{background:rgba(205,181,106,.22)}
    .btn-blue{
      background:rgba(96,165,250,.15);
      border:1px solid rgba(96,165,250,.45);
    }
    .btn-blue:hover{background:rgba(96,165,250,.22)}
    input,select,textarea{
      width:100%;background:#070f22;color:var(--text);
      border:1px solid rgba(32,48,81,.95);border-radius:12px;padding:10px;font-size:13px;
    }
    textarea{min-height:90px}
    .muted{color:var(--muted)}
    .small{font-size:12px}
    .divider{height:1px;background:rgba(32,48,81,.9);margin:12px 0}
    .chips{display:flex;gap:8px;flex-wrap:wrap}
    .chip{
      display:inline-flex;align-items:center;gap:6px;
      padding:6px 10px;border-radius:999px;
      border:1px solid rgba(32,48,81,.95);
      background:rgba(11,18,37,.65);
      color:var(--text);font-size:12px;
    }
    .chip.active{
      border-color:rgba(205,181,106,.55);
      background:rgba(205,181,106,.12);
      color:var(--gold);
      font-weight:800;
    }
    .col-3{grid-column:span 3}
    .col-4{grid-column:span 4}
    .col-5{grid-column:span 5}
    .col-6{grid-column:span 6}
    .col-7{grid-column:span 7}
    .col-8{grid-column:span 8}
    .col-12{grid-column:span 12}
    @media (max-width:1000px){
      .col-3,.col-4,.col-5,.col-6,.col-7,.col-8{grid-column:span 12}
      .metric{font-size:30px}
      .brand{min-width:auto}
      .brand-logo{height:26px}
    }
  
    .table-wrap{width:100%;overflow:auto;border-radius:14px}
    .table-wrap table{min-width:860px}
    @media (max-width:760px){
      .topbar{padding:10px 12px;gap:10px}
      .brand-sub{display:none}
      .nav{flex-wrap:wrap}
      .nav a{padding:7px 9px}
      .grid{gap:10px}
      .card{border-radius:18px}
      .btn{border-radius:14px}
      th,td{font-size:12px;padding:9px 7px}
      .kpi{padding:12px}
      .table-wrap table{min-width:720px}
    }

  </style>
</head>
<body>
  <div class="topbar">
    <div class="brand">
      <img class="brand-logo" src="{{ url_for('static', filename='fjc_logo.png') }}" alt="Freeman Jet Center" />
      <div>
        <div class="brand-title">Freeman Jet Center PUB</div>
        <div class="brand-sub">Maintenance & ATA 103 Compliance Board</div>
      </div>
    </div>

    <div class="nav">
      <a href="{{ url_for('dashboard') }}" class="{{ 'active' if active=='dash' else '' }}">Dashboard</a>
      <a href="{{ url_for('equipment') }}" class="{{ 'active' if active=='equip' else '' }}">Equipment</a>
      <a href="{{ url_for('maintenance') }}" class="{{ 'active' if active=='maint' else '' }}">Maintenance</a>
      <a href="{{ url_for('ata103') }}" class="{{ 'active' if active=='ata' else '' }}">ATA 103</a>
      <a href="{{ url_for('log_completion') }}" class="{{ 'active' if active=='log' else '' }}">Log</a>
    </div>

    <div style="margin-left:auto" class="row" style="align-items:center;gap:10px">
      <button class="btn btn-blue" type="button" onclick="setTech()" style="padding:8px 10px">Set Initials</button>
      <div class="muted small" style="text-align:right">
        Today: {{ today }}<br/>
        <span class="small">Due soon window: {{ soon_days }} days</span>
      </div>
    </div>
  </div>

  <div class="wrap">
    {{ body|safe }}
  </div>

  <script>
    function getTech(){ return localStorage.getItem('fjcTech') || ''; }
    function setTech(){
      const cur = getTech();
      const t = prompt('Enter your initials / name for audit logs:', cur);
      if(t && t.trim()){
        localStorage.setItem('fjcTech', t.trim());
      }
    }
    async function oneClickLog(scheduleId){
      let tech = getTech();
      if(!tech){
        const t = prompt('Enter your initials / name for audit logs:','');
        if(!t || !t.trim()) return;
        tech = t.trim();
        localStorage.setItem('fjcTech', tech);
      }
      const form = new URLSearchParams();
      form.set('schedule_id', String(scheduleId));
      form.set('tech', tech);
      try{
        const res = await fetch('{{ url_for("oneclick_log") }}', {
          method: 'POST',
          headers: {'Content-Type':'application/x-www-form-urlencoded'},
          body: form.toString()
        });
        const data = await res.json().catch(()=>null);
        if(!res.ok || !data || data.ok === false){
          alert((data && data.error) ? data.error : ('Log failed: ' + res.status));
          return;
        }
        window.location.reload();
      }catch(e){
        alert('Log failed: ' + e);
      }
    }
  </script>
</body>
</html>
"""


def render_page(title: str, active: str, body: str):
    return render_template_string(
        BASE_TEMPLATE,
        title=f"{APP_NAME} — {title}",
        active=active,
        body=body,
        today=str(date.today()),
        soon_days=DUE_SOON_DAYS,
    )


def get_groups() -> list[str]:
    cur = db().cursor()
    rows = cur.execute("SELECT DISTINCT group_name FROM assets ORDER BY group_name").fetchall()
    # Always keep these common ones present for filter UI:
    base = ["Fuel Farm", "Fuel Truck", "GSE", "Crew Cars"]
    existing = [r["group_name"] for r in rows]
    merged = []
    for x in base + existing:
        if x and x not in merged:
            merged.append(x)
    return merged


def parse_date_maybe(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    return date.fromisoformat(s)


def build_action_list(schedule_type: Optional[str], group_filter: Optional[str]):
    """
    Returns due/soon/today/overdue schedules list filtered by type and/or asset group.
    """
    refresh_next_due_all()

    cur = db().cursor()
    query = """
      SELECT s.*, a.name AS asset_name, a.asset_tag, a.group_name, a.location
      FROM schedules s
      JOIN assets a ON a.id = s.asset_id
      WHERE s.active=1
    """
    params = []
    if schedule_type:
        query += " AND s.schedule_type=?"
        params.append(schedule_type)
    if group_filter and group_filter != "All":
        query += " AND a.group_name=?"
        params.append(group_filter)
    query += " ORDER BY s.next_due ASC"

    rows = []
    for r in cur.execute(query, params).fetchall():
        nd = parse_date_maybe(r["next_due"])
        st = status_of(nd)
        if st in ("OVERDUE", "DUE TODAY", "DUE SOON"):
            rows.append((r, nd, st))
    return rows


# ------------------ Routes ------------------
@APP.get("/")
def dashboard():
    group = request.args.get("group", "All")
    groups = ["All"] + get_groups()

    # counts across everything (or filtered group if user selected one)
    refresh_next_due_all()
    cur = db().cursor()

    q = """
      SELECT s.next_due
      FROM schedules s
      JOIN assets a ON a.id=s.asset_id
      WHERE s.active=1
    """
    params = []
    if group != "All":
        q += " AND a.group_name=?"
        params.append(group)

    counts = {"OVERDUE": 0, "DUE TODAY": 0, "DUE SOON": 0, "OK": 0, "NO DATE": 0}
    for r in cur.execute(q, params).fetchall():
        nd = parse_date_maybe(r["next_due"])
        st = status_of(nd)
        counts[st] = counts.get(st, 0) + 1

    action_rows = build_action_list(schedule_type=None, group_filter=group)

    body = render_template_string("""
    <div class="grid">
      <div class="card col-12">
        <div class="row" style="justify-content:space-between">
          <div>
            <div style="font-weight:950;font-size:20px">Operations Board</div>
            <div class="muted small">Overdue / Due today / Due soon across Equipment, Maintenance, and ATA 103.</div>
          </div>
          <div class="row">
            <a class="btn btn-blue" href="{{ url_for('log_completion') }}">Log completion</a>
            <a class="btn" href="{{ url_for('equipment') }}">Manage equipment</a>
          </div>
        </div>

        <div class="divider"></div>

        <div class="chips">
          {% for g in groups %}
            <a class="chip {{ 'active' if g==group else '' }}" href="{{ url_for('dashboard', group=g) }}">{{ g }}</a>
          {% endfor %}
        </div>
      </div>

      <div class="card col-3">
        <h3>Overdue</h3>
        <div class="metric" style="color:var(--red)">{{ counts['OVERDUE'] }}</div>
        <div class="sub">Past due date</div>
      </div>
      <div class="card col-3">
        <h3>Due today</h3>
        <div class="metric" style="color:var(--orange)">{{ counts['DUE TODAY'] }}</div>
        <div class="sub">Due on {{ today }}</div>
      </div>
      <div class="card col-3">
        <h3>Due soon</h3>
        <div class="metric" style="color:var(--yellow)">{{ counts['DUE SOON'] }}</div>
        <div class="sub">Next {{ soon_days }} days</div>
      </div>
      <div class="card col-3">
        <h3>OK</h3>
        <div class="metric" style="color:var(--green)">{{ counts['OK'] }}</div>
        <div class="sub">Not due soon</div>
      </div>

      <div class="card col-12">
        <div class="row" style="justify-content:space-between">
          <div>
            <div style="font-weight:900;font-size:18px">Action List</div>
            <div class="muted small">Filtered: <b>{{ group }}</b></div>
          </div>
          <div class="row">
            <a class="btn btn-blue" href="{{ url_for('maintenance', group=group) }}">Maintenance tab</a>
            <a class="btn btn-blue" href="{{ url_for('ata103', group=group) }}">ATA 103 tab</a>
          </div>
        </div>

        <div class="table-wrap"><div class="table-wrap"><table>
          <tr>
            <th>Status</th><th>Due</th><th>Group</th><th>Asset</th><th>Schedule</th><th>Type</th><th>Location</th><th>Log</th><th></th>
          </tr>
          {% for r, nd, st in action_rows %}
            <tr>
              <td><span class="{{ status_class(st) }}">{{ st }}</span></td>
              <td>{{ nd }}</td>
              <td>{{ r['group_name'] }}</td>
              <td>{{ r['asset_name'] }} {% if r['asset_tag'] %}<span class="muted">({{ r['asset_tag'] }})</span>{% endif %}</td>
              <td>{{ r['title'] }}</td>
              <td>{{ r['schedule_type'] }}</td>
              <td>{{ r['location'] or '—' }}</td>
              <td>{% if r['interval'] == 'daily' %}<button class="btn btn-blue" type="button" onclick="oneClickLog({{ r['id'] }})">1‑Click</button>{% else %}<a class="btn btn-blue" href="{{ url_for('quicklog', schedule_id=r['id'], next=request.full_path) }}">Log</a>{% endif %}</td>
              <td class="small"><a href="{{ url_for('asset_detail', asset_id=r['asset_id']) }}">open</a></td>
            </tr>
          {% endfor %}
        </table></div></div>
      </div>
    </div>
    """, counts=counts, today=str(date.today()), soon_days=DUE_SOON_DAYS, groups=groups, group=group,
         action_rows=action_rows, status_class=status_class)

    return render_page("Dashboard", "dash", body)


@APP.get("/equipment")
def equipment():
    group = request.args.get("group", "All")
    groups = ["All"] + get_groups()
    cur = db().cursor()

    q = "SELECT * FROM assets"
    params = []
    if group != "All":
        q += " WHERE group_name=?"
        params.append(group)
    q += " ORDER BY group_name, name"

    assets = cur.execute(q, params).fetchall()

    body = render_template_string("""
    <div class="grid">
      <div class="card col-12">
        <div class="row" style="justify-content:space-between">
          <div>
            <div style="font-weight:950;font-size:20px">Equipment</div>
            <div class="muted small">Add and manage assets (Fuel Farm / Fuel Truck / GSE / Crew Cars).</div>
          </div>
        </div>

        <div class="divider"></div>

        <div class="chips">
          {% for g in groups %}
            <a class="chip {{ 'active' if g==group else '' }}" href="{{ url_for('equipment', group=g) }}">{{ g }}</a>
          {% endfor %}
        </div>

        <div class="divider"></div>

        <form method="post" action="{{ url_for('add_asset') }}">
          <div class="grid">
            <div class="col-6"><input name="name" placeholder="Asset name (e.g., TLD GPU #2)" required></div>
            <div class="col-6"><input name="asset_tag" placeholder="Asset tag / serial / ID (optional)"></div>

            <div class="col-4">
              <select name="group_name" required>
                {% for g in groups if g!='All' %}
                  <option value="{{ g }}">{{ g }}</option>
                {% endfor %}
              </select>
              <div class="muted small" style="margin-top:6px">Group</div>
            </div>

            <div class="col-4"><input name="location" placeholder="Location (Line Shack / Fuel Farm / Hangar A)"></div>

            <div class="col-4">
              <select name="status">
                <option>In Service</option>
                <option>Out of Service</option>
              </select>
              <div class="muted small" style="margin-top:6px">Status</div>
            </div>

            <div class="col-12"><input name="notes" placeholder="Notes (optional)"></div>
            <div class="col-12"><button class="btn" type="submit">Add asset</button></div>
          </div>
        </form>

        <table>
          <tr><th>Group</th><th>Asset</th><th>Location</th><th>Status</th><th></th></tr>
          {% for a in assets %}
            <tr>
              <td>{{ a['group_name'] }}</td>
              <td>{{ a['name'] }} {% if a['asset_tag'] %}<span class="muted">({{ a['asset_tag'] }})</span>{% endif %}</td>
              <td>{{ a['location'] or '—' }}</td>
              <td>{{ a['status'] }}</td>
              <td class="small"><a href="{{ url_for('asset_detail', asset_id=a['id']) }}">open</a></td>
            </tr>
          {% endfor %}
        </table>
      </div>
    </div>
    """, assets=assets, groups=groups, group=group)

    return render_page("Equipment", "equip", body)


@APP.post("/equipment/add")
def add_asset():
    name = request.form["name"].strip()
    asset_tag = (request.form.get("asset_tag") or "").strip() or None
    group_name = (request.form.get("group_name") or "GSE").strip()
    location = (request.form.get("location") or "").strip() or None
    status = (request.form.get("status") or "In Service").strip()
    notes = (request.form.get("notes") or "").strip() or None

    db().execute(
        "INSERT INTO assets(name, asset_tag, group_name, location, status, notes) VALUES(?,?,?,?,?,?)",
        (name, asset_tag, group_name, location, status, notes),
    )
    db().commit()
    return redirect(url_for("equipment"))


@APP.get("/maintenance")
def maintenance():
    group = request.args.get("group", "All")
    groups = ["All"] + get_groups()
    rows = build_action_list(schedule_type="MAINT", group_filter=group)

    body = render_template_string("""
    <div class="grid">
      <div class="card col-12">
        <div class="row" style="justify-content:space-between">
          <div>
            <div style="font-weight:950;font-size:20px">Maintenance</div>
            <div class="muted small">Only MAINT schedules. Use filters to focus on Fuel/GSE/Crew Cars.</div>
          </div>
          <div class="row">
            <a class="btn btn-blue" href="{{ url_for('log_completion') }}">Log completion</a>
          </div>
        </div>

        <div class="divider"></div>

        <div class="chips">
          {% for g in groups %}
            <a class="chip {{ 'active' if g==group else '' }}" href="{{ url_for('maintenance', group=g) }}">{{ g }}</a>
          {% endfor %}
        </div>

        <table>
          <tr>
            <th>Status</th><th>Due</th><th>Group</th><th>Asset</th><th>Schedule</th><th>Interval</th><th>Location</th><th>Log</th><th></th>
          </tr>
          {% for r, nd, st in rows %}
            <tr>
              <td><span class="{{ status_class(st) }}">{{ st }}</span></td>
              <td>{{ nd }}</td>
              <td>{{ r['group_name'] }}</td>
              <td>{{ r['asset_name'] }} {% if r['asset_tag'] %}<span class="muted">({{ r['asset_tag'] }})</span>{% endif %}</td>
              <td>{{ r['title'] }}</td>
              <td>{{ r['interval'] }} (x{{ r['every'] }})</td>
              <td>{{ r['location'] or '—' }}</td>
              <td>{% if r['interval'] == 'daily' %}<button class="btn btn-blue" type="button" onclick="oneClickLog({{ r['id'] }})">1‑Click</button>{% else %}<a class="btn btn-blue" href="{{ url_for('quicklog', schedule_id=r['id'], next=request.full_path) }}">Log</a>{% endif %}</td>
              <td class="small"><a href="{{ url_for('asset_detail', asset_id=r['asset_id']) }}">open</a></td>
            </tr>
          {% endfor %}
        </table>

        <div class="divider"></div>
        <div class="muted small">
          Tip: Schedules are added per-asset inside the asset “open” page. Use MAINT for equipment PM work.
        </div>
      </div>
    </div>
    """, rows=rows, groups=groups, group=group, status_class=status_class)

    return render_page("Maintenance", "maint", body)


@APP.get("/ata103")
def ata103():
    group = request.args.get("group", "All")
    groups = ["All"] + get_groups()
    rows = build_action_list(schedule_type="ATA103", group_filter=group)

    body = render_template_string("""
    <div class="grid">
      <div class="card col-12">
        <div class="row" style="justify-content:space-between">
          <div>
            <div style="font-weight:950;font-size:20px">ATA 103</div>
            <div class="muted small">Only ATA103 schedules. Logs become your audit entries.</div>
          </div>
          <div class="row">
            <a class="btn btn-blue" href="{{ url_for('log_completion') }}">Log ATA 103</a>
          </div>
        </div>

        <div class="divider"></div>

        <div class="chips">
          {% for g in groups %}
            <a class="chip {{ 'active' if g==group else '' }}" href="{{ url_for('ata103', group=g) }}">{{ g }}</a>
          {% endfor %}
        </div>

        <table>
          <tr>
            <th>Status</th><th>Due</th><th>Group</th><th>Asset</th><th>Inspection</th><th>Interval</th><th>Location</th><th>Log</th><th></th>
          </tr>
          {% for r, nd, st in rows %}
            <tr>
              <td><span class="{{ status_class(st) }}">{{ st }}</span></td>
              <td>{{ nd }}</td>
              <td>{{ r['group_name'] }}</td>
              <td>{{ r['asset_name'] }} {% if r['asset_tag'] %}<span class="muted">({{ r['asset_tag'] }})</span>{% endif %}</td>
              <td>{{ r['title'] }}</td>
              <td>{{ r['interval'] }} (x{{ r['every'] }})</td>
              <td>{{ r['location'] or '—' }}</td>
              <td>{% if r['interval'] == 'daily' %}<button class="btn btn-blue" type="button" onclick="oneClickLog({{ r['id'] }})">1‑Click</button>{% else %}<a class="btn btn-blue" href="{{ url_for('quicklog', schedule_id=r['id'], next=request.full_path) }}">Log</a>{% endif %}</td>
              <td class="small"><a href="{{ url_for('asset_detail', asset_id=r['asset_id']) }}">open</a></td>
            </tr>
          {% endfor %}
        </table>

        <div class="divider"></div>
        <div class="muted small">
          Tip: Put readings/corrective action in the log “Notes” field (ex: sump clear, DP reading, water detected, filter change, etc.).
        </div>
      </div>
    </div>
    """, rows=rows, groups=groups, group=group, status_class=status_class)

    return render_page("ATA 103", "ata", body)


@APP.get("/asset/<int:asset_id>")
def asset_detail(asset_id: int):
    refresh_next_due_all()
    cur = db().cursor()
    asset = cur.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not asset:
        return "Asset not found", 404

    schedules = cur.execute("""
      SELECT * FROM schedules WHERE asset_id=? ORDER BY active DESC, schedule_type, title
    """, (asset_id,)).fetchall()

    sched_view = []
    for s in schedules:
        nd = parse_date_maybe(s["next_due"]) or recompute_next_due(s)
        st = status_of(nd)
        sched_view.append((s, nd, st))

    comps = cur.execute("""
      SELECT c.*, s.title, s.schedule_type
      FROM completions c
      JOIN schedules s ON s.id = c.schedule_id
      WHERE s.asset_id=?
      ORDER BY c.completed_at DESC
      LIMIT 50
    """, (asset_id,)).fetchall()

    groups = get_groups()

    body = render_template_string("""
    <div class="grid">
      <div class="card col-12">
        <div class="row" style="justify-content:space-between">
          <div>
            <div style="font-weight:950;font-size:22px">{{ asset['name'] }}
              {% if asset['asset_tag'] %}<span class="muted">({{ asset['asset_tag'] }})</span>{% endif %}
            </div>
            <div class="muted small">{{ asset['group_name'] }} • {{ asset['location'] or '—' }} • {{ asset['status'] }}</div>
          </div>
          <div class="row">
            <a class="btn btn-blue" href="{{ url_for('log_completion', preset_asset=asset['id']) }}">Log for this asset</a>
            <a class="btn" href="{{ url_for('equipment') }}">Back to equipment</a>
          </div>
        </div>

        <div class="divider"></div>

        <h3>Asset details</h3>
        <form method="post" action="{{ url_for('update_asset', asset_id=asset['id']) }}">
          <div class="grid">
            <div class="col-6"><input name="name" value="{{ asset['name'] }}" required></div>
            <div class="col-6"><input name="asset_tag" value="{{ asset['asset_tag'] or '' }}" placeholder="Asset tag / serial / ID"></div>
            <div class="col-4">
              <select name="group_name">
                {% for g in groups %}
                  <option value="{{ g }}" {% if g==asset['group_name'] %}selected{% endif %}>{{ g }}</option>
                {% endfor %}
              </select>
              <div class="muted small" style="margin-top:6px">Group</div>
            </div>
            <div class="col-4"><input name="location" value="{{ asset['location'] or '' }}" placeholder="Location"></div>
            <div class="col-4">
              <select name="status">
                <option {% if asset['status']=='In Service' %}selected{% endif %}>In Service</option>
                <option {% if asset['status']=='Out of Service' %}selected{% endif %}>Out of Service</option>
              </select>
              <div class="muted small" style="margin-top:6px">Status</div>
            </div>
            <div class="col-12"><input name="notes" value="{{ asset['notes'] or '' }}" placeholder="Notes"></div>
            <div class="col-12"><button class="btn" type="submit">Save asset</button></div>
          </div>
        </form>

        <div class="divider"></div>

        <h3>Add schedule (Maintenance or ATA 103)</h3>
        <form method="post" action="{{ url_for('add_schedule', asset_id=asset['id']) }}">
          <div class="grid">
            <div class="col-6"><input name="title" placeholder="Schedule title (e.g., ATA 103 Daily Fuel Sump Check)" required></div>
            <div class="col-6">
              <select name="schedule_type" required>
                <option value="MAINT">MAINT</option>
                <option value="ATA103">ATA103</option>
              </select>
              <div class="muted small" style="margin-top:6px">Type</div>
            </div>

            <div class="col-4">
              <select name="interval" required>
                <option>daily</option><option>weekly</option><option>monthly</option><option>quarterly</option><option>yearly</option>
              </select>
              <div class="muted small" style="margin-top:6px">Interval</div>
            </div>

            <div class="col-4"><input name="every" type="number" min="1" value="1" /></div>
            <div class="col-4"><input name="start_date" type="date" value="{{ today }}" /></div>

            <div class="col-12"><textarea name="instructions" placeholder="Instructions/checklist notes (optional)"></textarea></div>
            <div class="col-12"><button class="btn" type="submit">Add schedule</button></div>
          </div>
        </form>

        <div class="divider"></div>

        <h3>Schedules</h3>
        <table>
          <tr><th>Status</th><th>Next due</th><th>Schedule</th><th>Type</th><th>Interval</th><th>Active</th><th></th></tr>
          {% for s, nd, st in sched_view %}
            <tr>
              <td><span class="{{ status_class(st) }}">{{ st }}</span></td>
              <td>{{ nd }}</td>
              <td>
                <div style="font-weight:800">{{ s['title'] }}</div>
                {% if s['instructions'] %}<div class="muted small">{{ s['instructions'] }}</div>{% endif %}
              </td>
              <td>{{ s['schedule_type'] }}</td>
              <td>{{ s['interval'] }} (x{{ s['every'] }})</td>
              <td>{{ 'Yes' if s['active'] else 'No' }}</td>
              <td class="small">
                <form method="post" action="{{ url_for('toggle_schedule', schedule_id=s['id'], asset_id=asset['id']) }}" style="margin:0">
                  <button class="btn btn-blue" type="submit">{{ 'Disable' if s['active'] else 'Enable' }}</button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </table>

        <div class="divider"></div>

        <h3>Audit entries (last 50)</h3>
        <table>
          <tr><th>When</th><th>Type</th><th>Schedule</th><th>Tech</th><th>Result</th><th>Notes</th></tr>
          {% for c in comps %}
            <tr>
              <td>{{ c['completed_at'] }}</td>
              <td>{{ c['schedule_type'] }}</td>
              <td>{{ c['title'] }}</td>
              <td>{{ c['tech'] }}</td>
              <td>{{ c['result'] }}</td>
              <td>{{ c['notes'] or '' }}</td>
            </tr>
          {% endfor %}
        </table>

        <div class="divider"></div>
        <form method="post" action="{{ url_for('delete_asset', asset_id=asset['id']) }}" onsubmit="return confirm('Delete this asset and all its schedules/audit entries?');">
          <button class="btn" type="submit" style="border-color:rgba(239,68,68,.5);background:rgba(239,68,68,.12)">Delete asset</button>
        </form>

      </div>
    </div>
    """, asset=asset, sched_view=sched_view, status_class=status_class, today=str(date.today()), comps=comps, groups=groups)

    return render_page(asset["name"], "equip", body)


@APP.post("/asset/<int:asset_id>/update")
def update_asset(asset_id: int):
    name = request.form["name"].strip()
    asset_tag = (request.form.get("asset_tag") or "").strip() or None
    group_name = (request.form.get("group_name") or "GSE").strip()
    location = (request.form.get("location") or "").strip() or None
    status = (request.form.get("status") or "In Service").strip()
    notes = (request.form.get("notes") or "").strip() or None

    cur = db().cursor()
    cur.execute("""
      UPDATE assets
      SET name=?, asset_tag=?, group_name=?, location=?, status=?, notes=?
      WHERE id=?
    """, (name, asset_tag, group_name, location, status, notes, asset_id))
    db().commit()
    return redirect(url_for("asset_detail", asset_id=asset_id))


@APP.post("/asset/<int:asset_id>/delete")
def delete_asset(asset_id: int):
    db().execute("DELETE FROM assets WHERE id=?", (asset_id,))
    db().commit()
    return redirect(url_for("equipment"))


@APP.post("/asset/<int:asset_id>/schedule/add")
def add_schedule(asset_id: int):
    title = request.form["title"].strip()
    schedule_type = request.form["schedule_type"].strip()
    interval = request.form["interval"].strip()
    every = int(request.form.get("every") or 1)
    start_date = request.form.get("start_date") or str(date.today())
    instructions = (request.form.get("instructions") or "").strip() or None

    db().execute("""
      INSERT INTO schedules(asset_id,title,schedule_type,interval,every,start_date,last_completed,next_due,instructions,active)
      VALUES(?,?,?,?,?,?,?,?,?,1)
    """, (asset_id, title, schedule_type, interval, every, start_date, None, start_date, instructions))
    db().commit()
    return redirect(url_for("asset_detail", asset_id=asset_id))


@APP.post("/schedule/<int:schedule_id>/toggle/<int:asset_id>")
def toggle_schedule(schedule_id: int, asset_id: int):
    cur = db().cursor()
    s = cur.execute("SELECT active FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    if not s:
        return "Schedule not found", 404
    new_val = 0 if s["active"] else 1
    cur.execute("UPDATE schedules SET active=? WHERE id=?", (new_val, schedule_id))
    db().commit()
    return redirect(url_for("asset_detail", asset_id=asset_id))


@APP.get("/log")
def log_completion():
    preset_asset = request.args.get("preset_asset", type=int)
    preset_type = request.args.get("type")  # optional: MAINT or ATA103

    cur = db().cursor()
    schedules = cur.execute("""
      SELECT s.id, s.title, s.schedule_type, s.asset_id,
             a.name AS asset_name, a.group_name
      FROM schedules s
      JOIN assets a ON a.id = s.asset_id
      WHERE s.active=1
      ORDER BY a.group_name, a.name, s.schedule_type, s.title
    """).fetchall()
    next_url = request.args.get("next") or request.referrer or url_for("dashboard")


    body = render_template_string("""
    <div class="grid">
      <div class="card col-12">
        <div style="font-weight:950;font-size:20px">Log completion</div>
        <div class="muted small">No login required — Tech initials/name is required for audit trail.</div>
        <div class="divider"></div>

        <form method="post" action="{{ url_for('log_completion_post') }}">
          <input type="hidden" name="next_url" value="{{ next_url }}">
          <div class="grid">
            <div class="col-8">
              <select name="schedule_id" required>
                <option value="">Select schedule…</option>
                {% for s in schedules %}
                  {% if (not preset_asset or s['asset_id'] == preset_asset) and (not preset_type or s['schedule_type'] == preset_type) %}
                  <option value="{{ s['id'] }}">{{ s['group_name'] }} — {{ s['asset_name'] }} — {{ s['schedule_type'] }} — {{ s['title'] }}</option>
                  {% endif %}
                {% endfor %}
              </select>
            </div>
            <div class="col-4"><input name="tech" placeholder="Tech initials/name (required)" required></div>

            <div class="col-4"><input type="date" name="completed_date" value="{{ today }}"></div>
            <div class="col-4">
              <select name="result">
                <option>PASS</option>
                <option>FAIL</option>
                <option>NA</option>
              </select>
            </div>
            <div class="col-12"><textarea name="notes" placeholder="Notes / readings / corrective action (optional)"></textarea></div>

            <div class="col-12">
              <button class="btn btn-blue" type="submit">Save audit entry</button>
              <span class="muted small" style="margin-left:10px">Tip: For ATA 103, put readings (DP, sump result, water found, etc.) in Notes.</span>
            </div>
          </div>
        </form>
      </div>
    </div>
    """, schedules=schedules, today=str(date.today()), preset_asset=preset_asset, preset_type=preset_type, next_url=next_url)

    return render_page("Log", "log", body)


@APP.post("/log")
def log_completion_post():
    schedule_id = int(request.form["schedule_id"])
    tech = request.form["tech"].strip()
    completed_date = request.form.get("completed_date") or str(date.today())
    result = (request.form.get("result") or "PASS").strip().upper()
    notes = (request.form.get("notes") or "").strip() or None

    now_iso = datetime.now().replace(microsecond=0).isoformat(sep=" ")

    cur = db().cursor()
    s = cur.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    if not s:
        return "Schedule not found", 404

    # Insert completion audit entry
    cur.execute("""
      INSERT INTO completions(schedule_id, completed_at, completed_date, tech, result, notes)
      VALUES(?,?,?,?,?,?)
    """, (schedule_id, now_iso, completed_date, tech, result, notes))

    # Update schedule last_completed and recompute next_due
    cur.execute("UPDATE schedules SET last_completed=? WHERE id=?", (completed_date, schedule_id))
    s2 = cur.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    nd = recompute_next_due(s2)
    cur.execute("UPDATE schedules SET next_due=? WHERE id=?", (nd.isoformat() if nd else None, schedule_id))

    db().commit()
    return redirect(next_url)

@APP.get("/quicklog/<int:schedule_id>")
def quicklog(schedule_id: int):
    refresh_next_due_all()
    cur = db().cursor()

    row = cur.execute("""
      SELECT s.*, a.name AS asset_name, a.asset_tag, a.group_name, a.location
      FROM schedules s
      JOIN assets a ON a.id = s.asset_id
      WHERE s.id=?
    """, (schedule_id,)).fetchone()
    if not row:
        return "Schedule not found", 404

    nd = parse_date_maybe(row["next_due"])
    st = status_of(nd)

    next_url = request.args.get("next") or request.referrer or url_for("dashboard")

    body = render_template_string("""
    <div class="grid">
      <div class="card col-12">
        <div class="row" style="justify-content:space-between">
          <div>
            <div style="font-weight:950;font-size:20px">Quick Log</div>
            <div class="muted small">One-step completion entry (audit)</div>
          </div>
          <div class="row">
            <a class="btn" href="{{ next_url }}">Back</a>
          </div>
        </div>

        <div class="divider"></div>

        <div class="row" style="gap:14px;align-items:flex-start">
          <div style="flex:1">
            <div style="font-weight:900;font-size:18px">
              {{ row['group_name'] }} — {{ row['asset_name'] }}
              {% if row['asset_tag'] %}<span class="muted">({{ row['asset_tag'] }})</span>{% endif %}
            </div>
            <div class="muted small">
              {{ row['location'] or '—' }} • <span class="{{ status_class(st) }}">{{ st }}</span> • Next due: <b>{{ nd }}</b>
            </div>
            <div style="margin-top:10px;font-weight:800">{{ row['schedule_type'] }} — {{ row['title'] }}</div>
            {% if row['instructions'] %}
              <div class="muted small" style="margin-top:6px">{{ row['instructions'] }}</div>
            {% endif %}
          </div>
        </div>

        <div class="divider"></div>

        <form method="post" action="{{ url_for('quicklog_post', schedule_id=row['id']) }}">
          <input type="hidden" name="next_url" value="{{ next_url }}">
          <div class="grid">
            <div class="col-4"><input name="tech" placeholder="Tech initials/name (required)" required></div>
            <div class="col-4"><input type="date" name="completed_date" value="{{ today }}"></div>
            <div class="col-4">
              <select name="result">
                <option>PASS</option>
                <option>FAIL</option>
                <option>NA</option>
              </select>
            </div>
            <div class="col-12"><input name="notes" placeholder="Notes / readings / corrective action (optional)"></div>
            <div class="col-12">
              <button class="btn btn-blue" type="submit">Save completion</button>
              <span class="muted small" style="margin-left:10px">This creates an audit entry and updates next due date.</span>
            </div>
          </div>
        </form>
      </div>
    </div>
    """, row=row, nd=nd, st=st, status_class=status_class, today=str(date.today()), next_url=next_url)

    return render_page("Quick Log", "log", body)


@APP.post("/quicklog/<int:schedule_id>")
def quicklog_post(schedule_id: int):
    tech = request.form["tech"].strip()
    completed_date = request.form.get("completed_date") or str(date.today())
    result = (request.form.get("result") or "PASS").strip().upper()
    notes = (request.form.get("notes") or "").strip() or None
    next_url = request.form.get("next_url") or url_for("dashboard")

    now_iso = datetime.now().replace(microsecond=0).isoformat(sep=" ")

    cur = db().cursor()
    s = cur.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    if not s:
        return "Schedule not found", 404

    # Audit entry
    cur.execute("""
      INSERT INTO completions(schedule_id, completed_at, completed_date, tech, result, notes)
      VALUES(?,?,?,?,?,?)
    """, (schedule_id, now_iso, completed_date, tech, result, notes))

    # Update schedule last_completed and next_due
    cur.execute("UPDATE schedules SET last_completed=? WHERE id=?", (completed_date, schedule_id))
    s2 = cur.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    nd = recompute_next_due(s2)
    cur.execute("UPDATE schedules SET next_due=? WHERE id=?", (nd.isoformat() if nd else None, schedule_id))

    db().commit()
    return redirect(next_url)




@APP.post("/oneclick")
def oneclick_log():
    """One-click logging for daily recurring items.

    The browser stores initials/name in localStorage (fjcTech). If missing, UI prompts.
    """
    schedule_id = int(request.form.get("schedule_id", "0") or 0)
    tech = (request.form.get("tech") or "").strip()
    if schedule_id <= 0:
        return {"ok": False, "error": "Missing schedule_id"}, 400
    if not tech:
        return {"ok": False, "error": "Missing tech initials/name"}, 400

    completed_date = str(date.today())
    result = (request.form.get("result") or "PASS").strip().upper()
    notes = (request.form.get("notes") or "").strip() or None
    now_iso = datetime.now().replace(microsecond=0).isoformat(sep=" ")

    cur = db().cursor()
    s = cur.execute("SELECT * FROM schedules WHERE id=? AND active=1", (schedule_id,)).fetchone()
    if not s:
        return {"ok": False, "error": "Schedule not found / inactive"}, 404

    cur.execute(
        """
        INSERT INTO completions(schedule_id, completed_at, completed_date, tech, result, notes)
        VALUES(?,?,?,?,?,?)
        """,
        (schedule_id, now_iso, completed_date, tech, result, notes),
    )

    cur.execute("UPDATE schedules SET last_completed=? WHERE id=?", (completed_date, schedule_id))
    s2 = cur.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    nd = recompute_next_due(s2)
    cur.execute("UPDATE schedules SET next_due=? WHERE id=?", (nd.isoformat() if nd else None, schedule_id))

    db().commit()
    return {"ok": True, "next_due": (nd.isoformat() if nd else None)}


# ------------------ Main ------------------
if __name__ == "__main__":
    init_db()
    # LAN server: everyone uses http://HOST_IP:5000
    serve(APP, host="0.0.0.0", port=5000)
