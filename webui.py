#!/usr/bin/env python3
"""
Web UI for Harvestr (universal video downloader).

Features:
  - Add/remove performers
  - Select which sites to probe (or all)
  - Start/stop background downloads
  - Live progress: running, completed, failed
  - View history.json / failed.json entries
  - Trigger dedup
  - Browse downloaded files
  - Tail log in real-time

Usage:
  python webui.py [--port 7860] [--host 127.0.0.1]

Then open http://127.0.0.1:7860 in a browser.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

try:
    from flask import Flask, jsonify, render_template_string, request, send_file
except ImportError:
    print("ERROR: Flask is required. Install with: pip install flask")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
DOWNLOADS_DIR = SCRIPT_DIR / "downloads"
HISTORY_PATH = DOWNLOADS_DIR / "history.json"
FAILED_PATH = DOWNLOADS_DIR / "failed.json"
LOG_PATH = DOWNLOADS_DIR / "universal.log"

app = Flask(__name__)

# ── State shared with background task ────────────────────────────────────────
_state = {
    "running": False,
    "pid": None,
    "started_at": None,
    "current_performer": "",
    "last_output_line": "",
    "log_tail": deque(maxlen=500),
}
_state_lock = threading.Lock()
_runner_thread: subprocess.Popen | None = None


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"performers": [], "enabled_sites": []}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def load_sites() -> list[str]:
    """Return list of all site names (yt-dlp + custom scrapers)."""
    sites = []
    sites_json = SCRIPT_DIR / "sites.json"
    if sites_json.exists():
        try:
            data = json.loads(sites_json.read_text(encoding="utf-8"))
            for name in data.get("sites", {}).keys():
                if not name.startswith("_"):
                    sites.append(name)
        except Exception:
            pass
    # Add custom scraper names
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from custom_scrapers import ALL_SCRAPER_CLASSES
        for cls in ALL_SCRAPER_CLASSES:
            sites.append(cls.NAME)
    except Exception:
        pass
    return sorted(set(sites))


# ── HTML UI ──────────────────────────────────────────────────────────────────
INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Harvestr — video downloader</title>
<style>
  * { box-sizing: border-box; }
  body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; background: #0f1115; color: #e6e8eb; }
  header { background: #181b22; padding: 12px 20px; border-bottom: 1px solid #2a2e38;
           display: flex; align-items: center; gap: 12px; position: sticky; top: 0; z-index: 10; }
  h1 { margin: 0; font-size: 18px; }
  .status-dot { width: 10px; height: 10px; border-radius: 50%; background: #666; }
  .status-dot.running { background: #3cb371; box-shadow: 0 0 8px #3cb37160; animation: pulse 2s infinite; }
  .status-dot.error { background: #e74c3c; }
  @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:.4;} }
  .container { max-width: 1400px; margin: 0 auto; padding: 16px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 960px) { .grid { grid-template-columns: 1fr; } }
  .card { background: #181b22; border: 1px solid #2a2e38; border-radius: 8px;
          padding: 14px 16px; margin-bottom: 16px; }
  .card h2 { margin: 0 0 10px 0; font-size: 15px; color: #a0d7ff; }
  button { background: #2a2e38; color: #e6e8eb; border: 1px solid #3a3f4b;
           padding: 6px 12px; border-radius: 5px; cursor: pointer; font-size: 13px; }
  button:hover { background: #3a3f4b; }
  button.primary { background: #2a6cb3; border-color: #3a7cc3; }
  button.primary:hover { background: #3a7cc3; }
  button.danger { background: #a8381b; border-color: #c04830; }
  button.danger:hover { background: #c04830; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  input[type="text"], textarea, select {
    width: 100%; background: #0f1115; color: #e6e8eb; border: 1px solid #2a2e38;
    border-radius: 5px; padding: 6px 8px; font-size: 13px; font-family: inherit;
  }
  textarea { resize: vertical; min-height: 100px; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #2a2e38; }
  th { background: #0f1115; font-weight: 600; color: #a0d7ff; position: sticky; top: 0; }
  tbody tr:hover { background: #1c1f28; }
  .log-viewer {
    background: #070810; color: #c8d0da; border: 1px solid #2a2e38; border-radius: 5px;
    padding: 8px 10px; height: 340px; overflow: auto; font-family: Consolas, monospace;
    font-size: 12px; white-space: pre-wrap; line-height: 1.35;
  }
  .log-viewer .INFO { color: #c8d0da; }
  .log-viewer .WARN, .log-viewer .WARNING { color: #f6b73d; }
  .log-viewer .ERROR { color: #ff6b6b; }
  .log-viewer .DEBUG { color: #7a8ba0; }
  .pill { display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 11px;
          background: #2a2e38; color: #a0d7ff; margin-right: 4px; }
  .pill.ok { background: #1b4d28; color: #8cea9c; }
  .pill.fail { background: #4d1b1b; color: #ea8c8c; }
  .pill.private { background: #4d401b; color: #eac98c; }
  .flex { display: flex; gap: 8px; align-items: center; }
  .mb { margin-bottom: 10px; }
  .perf-row { display: flex; align-items: center; gap: 8px; padding: 6px 8px;
              border-bottom: 1px solid #2a2e38; }
  .perf-row:hover { background: #1c1f28; }
  .perf-row .count { margin-left: auto; color: #7a8ba0; font-size: 12px; }
  .perf-list { max-height: 380px; overflow-y: auto; }
  details summary { cursor: pointer; padding: 4px 0; color: #a0d7ff; }
  details[open] summary { margin-bottom: 8px; }
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .stat-box { background: #0f1115; padding: 10px; border-radius: 5px; text-align: center; }
  .stat-box .value { font-size: 20px; font-weight: 700; color: #a0d7ff; }
  .stat-box .label { font-size: 11px; color: #7a8ba0; margin-top: 4px; }
  .site-checkboxes { display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px;
                     max-height: 280px; overflow-y: auto; padding: 4px; background: #0f1115;
                     border: 1px solid #2a2e38; border-radius: 5px; }
  .site-checkboxes label { display: flex; align-items: center; gap: 4px; font-size: 12px;
                            cursor: pointer; padding: 2px 4px; }
  .site-checkboxes label:hover { background: #1c1f28; border-radius: 3px; }
  .site-checkboxes input[type="checkbox"] { margin: 0; }
  .config-table { font-size: 13px; }
  .config-table td:first-child { color: #7a8ba0; width: 40%; }
  .toast { position: fixed; top: 70px; right: 20px; padding: 10px 14px;
           background: #2a6cb3; color: white; border-radius: 5px; font-size: 13px;
           opacity: 0; transform: translateY(-10px); transition: all .25s;
           box-shadow: 0 2px 10px #00000080; z-index: 100; }
  .toast.show { opacity: 1; transform: translateY(0); }
  .toast.error { background: #a8381b; }
  .toast.success { background: #1b7d3b; }
</style>
</head>
<body>
<header>
  <div class="status-dot" id="status-dot"></div>
  <h1>Harvestr</h1>
  <span style="color: #7a8ba0; font-size: 12px; letter-spacing: .5px;">one username &rarr; every video</span>
  <span id="status-text" style="color:#7a8ba0; font-size:12px;">idle</span>
  <div style="flex:1"></div>
  <button class="primary" id="start-btn" onclick="startDownload()">▶ Start All</button>
  <button class="danger" id="stop-btn" onclick="stopDownload()" disabled>■ Stop</button>
  <button onclick="runDedup()">🧹 Dedup</button>
  <button onclick="refreshAll()">↻ Refresh</button>
</header>

<div id="toast" class="toast"></div>

<div class="container">

  <div class="stats">
    <div class="stat-box"><div class="value" id="stat-perf">–</div><div class="label">Performers</div></div>
    <div class="stat-box"><div class="value" id="stat-hist">–</div><div class="label">Downloaded</div></div>
    <div class="stat-box"><div class="value" id="stat-fail">–</div><div class="label">Permanently Failed</div></div>
    <div class="stat-box"><div class="value" id="stat-disk">–</div><div class="label">Total Size</div></div>
  </div>

  <div class="grid">

    <div class="card">
      <h2>Performers</h2>
      <div class="flex mb">
        <input id="new-perf" type="text" placeholder="username (e.g. blondie_254)"
               onkeydown="if(event.key==='Enter'){addPerformer();}" />
        <button class="primary" onclick="addPerformer()">Add</button>
      </div>
      <div class="perf-list" id="perf-list"></div>
      <div class="flex" style="margin-top: 8px;">
        <button onclick="runSinglePerformer()">▶ Run Selected Only</button>
        <span style="color:#7a8ba0; font-size:12px;">(Ctrl-click perf to select)</span>
      </div>
    </div>

    <div class="card">
      <h2>Settings</h2>
      <table class="config-table">
        <tr><td>Output dir</td><td><input id="cfg-output-dir" type="text"/></td></tr>
        <tr><td>Max videos per site</td><td><input id="cfg-max-videos" type="text"/></td></tr>
        <tr><td>Max parallel downloads</td><td><input id="cfg-max-parallel" type="text"/></td></tr>
        <tr><td>aria2c connections</td><td><input id="cfg-aria2c-conn" type="text"/></td></tr>
        <tr><td>Min disk GB</td><td><input id="cfg-min-disk" type="text"/></td></tr>
        <tr><td>Min duration (s)</td><td><input id="cfg-min-dur" type="text"/></td></tr>
        <tr><td>Rate limit (e.g. 500K)</td><td><input id="cfg-rate" type="text"/></td></tr>
        <tr><td>Cookies file</td><td><input id="cfg-cookies" type="text" placeholder="(optional) path to cookies.txt"/></td></tr>
        <tr><td>Impersonate</td><td><input id="cfg-imp" type="text" placeholder="chrome"/></td></tr>
      </table>
      <div style="margin-top: 10px;">
        <button class="primary" onclick="saveSettings()">💾 Save settings</button>
      </div>
    </div>

    <div class="card">
      <h2>Sites (empty = all enabled)</h2>
      <div class="flex mb">
        <button onclick="setSitesAll(true)">Select all</button>
        <button onclick="setSitesAll(false)">Clear</button>
        <button onclick="setSitesCategory('custom')">Custom only</button>
        <button onclick="setSitesCategory('ytdlp')">yt-dlp only</button>
      </div>
      <div class="site-checkboxes" id="sites-list"></div>
    </div>

    <div class="card">
      <h2>Live log</h2>
      <div class="log-viewer" id="log-viewer"></div>
    </div>

    <div class="card" style="grid-column: 1 / -1;">
      <h2>Downloaded videos
        <span style="float:right; font-size:12px; color:#7a8ba0; font-weight:normal;">
          <input id="hist-filter" type="text" placeholder="filter by performer or title..."
                 style="width: 250px; display: inline-block;" oninput="renderHistory()"/>
        </span>
      </h2>
      <div style="max-height: 420px; overflow-y: auto;">
        <table>
          <thead>
            <tr><th>Performer</th><th>Site</th><th>Title</th><th>Size</th><th>Date</th><th></th></tr>
          </thead>
          <tbody id="hist-body"></tbody>
        </table>
      </div>
    </div>

    <div class="card" style="grid-column: 1 / -1;">
      <h2>Failed / Skipped (<span id="fail-count">0</span>)</h2>
      <div style="max-height: 280px; overflow-y: auto;">
        <table>
          <thead>
            <tr><th>Video ID</th><th>Site</th><th>Reason</th><th>Fails</th><th>Permanent</th></tr>
          </thead>
          <tbody id="fail-body"></tbody>
        </table>
      </div>
    </div>

  </div>
</div>

<script>
let _config = {};
let _allSites = [];
let _history = {};
let _failed = {};
let _selectedPerformer = null;

function toast(msg, type='') {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show ' + type;
  setTimeout(() => t.className = 'toast ' + type, 3000);
}

async function api(path, opts={}) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

async function refreshStatus() {
  try {
    const s = await api('/api/status');
    const dot = document.getElementById('status-dot');
    const txt = document.getElementById('status-text');
    if (s.running) {
      dot.className = 'status-dot running';
      txt.textContent = s.current_performer || 'running...';
    } else {
      dot.className = 'status-dot';
      txt.textContent = 'idle';
    }
    document.getElementById('start-btn').disabled = s.running;
    document.getElementById('stop-btn').disabled = !s.running;
    // Live log
    const lv = document.getElementById('log-viewer');
    const shouldScroll = lv.scrollTop + lv.clientHeight >= lv.scrollHeight - 10;
    lv.innerHTML = s.log_tail.map(line => {
      let cls = '';
      if (line.includes('ERROR')) cls = 'ERROR';
      else if (line.includes('WARN')) cls = 'WARN';
      else if (line.includes('DEBUG')) cls = 'DEBUG';
      return `<span class="${cls}">${escapeHtml(line)}</span>\n`;
    }).join('');
    if (shouldScroll) lv.scrollTop = lv.scrollHeight;
  } catch (e) { console.error(e); }
}

function escapeHtml(s) {
  return (s||'').replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}

async function loadConfig() {
  _config = await api('/api/config');
  document.getElementById('cfg-output-dir').value = _config.output_dir || '';
  document.getElementById('cfg-max-videos').value = _config.max_videos_per_site || '';
  document.getElementById('cfg-max-parallel').value = _config.max_parallel_downloads || '';
  document.getElementById('cfg-aria2c-conn').value = _config.aria2c_connections || '';
  document.getElementById('cfg-min-disk').value = _config.min_disk_gb || '';
  document.getElementById('cfg-min-dur').value = _config.min_duration_seconds || '';
  document.getElementById('cfg-rate').value = _config.rate_limit || '';
  document.getElementById('cfg-cookies').value = _config.cookies_file || '';
  document.getElementById('cfg-imp').value = _config.impersonate_target || '';
  renderPerformers();
  renderSites();
}

function renderPerformers() {
  const list = document.getElementById('perf-list');
  const perfs = _config.performers || [];
  list.innerHTML = perfs.map(p => {
    const hist_count = Object.keys(_history[p.toLowerCase()] || {}).length;
    const sel = (p === _selectedPerformer) ? 'style="background:#2a6cb3;"' : '';
    return `<div class="perf-row" ${sel} onclick="togglePerf('${p}')">
      <span>${escapeHtml(p)}</span>
      <span class="count">${hist_count} videos</span>
      <button class="danger" onclick="removePerformer('${p}'); event.stopPropagation()"
        style="padding: 1px 6px; font-size: 11px;">×</button>
    </div>`;
  }).join('');
  document.getElementById('stat-perf').textContent = perfs.length;
}

function togglePerf(name) {
  _selectedPerformer = (_selectedPerformer === name) ? null : name;
  renderPerformers();
}

async function addPerformer() {
  const name = document.getElementById('new-perf').value.trim();
  if (!name) return;
  try {
    await api('/api/config/performer/add', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name})
    });
    document.getElementById('new-perf').value = '';
    toast('Added ' + name, 'success');
    loadConfig();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function removePerformer(name) {
  if (!confirm('Remove ' + name + '?')) return;
  try {
    await api('/api/config/performer/remove', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name})
    });
    toast('Removed ' + name);
    loadConfig();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function loadSites() {
  const d = await api('/api/sites');
  _allSites = d.sites;
}

function renderSites() {
  const el = document.getElementById('sites-list');
  const enabled = new Set(_config.enabled_sites || []);
  const isEmpty = enabled.size === 0;   // empty = all
  el.innerHTML = _allSites.map(s => `
    <label><input type="checkbox" value="${s}" ${(isEmpty || enabled.has(s)) ? 'checked' : ''}
      onchange="toggleSite(this)"/> ${s}</label>
  `).join('');
}

function toggleSite(cb) {
  const enabled = new Set(_config.enabled_sites || []);
  if (enabled.size === 0) {
    // Was "all"; start from all, then remove this one
    _allSites.forEach(s => enabled.add(s));
  }
  if (cb.checked) enabled.add(cb.value);
  else enabled.delete(cb.value);
  // If all sites are selected, normalize to []  (means all)
  const arr = (enabled.size === _allSites.length) ? [] : Array.from(enabled);
  _config.enabled_sites = arr;
  // debounce save
  clearTimeout(window._sitesSaveT);
  window._sitesSaveT = setTimeout(saveSettings, 500);
}

function setSitesAll(on) {
  _config.enabled_sites = on ? [] : ['_NONE_'];   // [] = all, dummy = none
  saveSettings();
  renderSites();
}

function setSitesCategory(cat) {
  const CUSTOM = ['camwhores_tv','camwhores_video','camwhores_co','camwhoreshd','camwhoresbay',
                  'camwhoresbay_tv','camwhores_bz','camwhorescloud','camvideos_tv','camhub_cc',
                  'camwh_com','cambro_tv','camcaps_tv','camstreams_tv','porntrex','camsrip',
                  'recordbate','archivebate','camcaps_io','coomer','kemono','redgifs','reddit',
                  'xcom','recume'];
  if (cat === 'custom') _config.enabled_sites = CUSTOM;
  else if (cat === 'ytdlp') {
    _config.enabled_sites = _allSites.filter(s => !CUSTOM.includes(s));
  }
  saveSettings();
  renderSites();
}

async function saveSettings() {
  const cfg = {..._config,
    output_dir: document.getElementById('cfg-output-dir').value,
    max_videos_per_site: parseInt(document.getElementById('cfg-max-videos').value) || 10,
    max_parallel_downloads: parseInt(document.getElementById('cfg-max-parallel').value) || 3,
    aria2c_connections: parseInt(document.getElementById('cfg-aria2c-conn').value) || 16,
    min_disk_gb: parseFloat(document.getElementById('cfg-min-disk').value) || 5.0,
    min_duration_seconds: parseFloat(document.getElementById('cfg-min-dur').value) || 30.0,
    rate_limit: document.getElementById('cfg-rate').value,
    cookies_file: document.getElementById('cfg-cookies').value,
    impersonate_target: document.getElementById('cfg-imp').value,
  };
  try {
    await api('/api/config', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(cfg)
    });
    _config = cfg;
    toast('Settings saved', 'success');
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function loadHistory() {
  _history = await api('/api/history');
  _failed = await api('/api/failed');
  renderHistory();
  renderFailed();
}

function renderHistory() {
  const filter = document.getElementById('hist-filter').value.toLowerCase();
  const tbody = document.getElementById('hist-body');
  let rows = [];
  let totalSize = 0;
  for (const [perf, entries] of Object.entries(_history)) {
    for (const [gid, info] of Object.entries(entries)) {
      if (filter && !perf.toLowerCase().includes(filter) &&
          !(info.title || '').toLowerCase().includes(filter)) continue;
      rows.push({perf, ...info, gid});
      totalSize += info.filesize || 0;
    }
  }
  rows.sort((a,b) => (b.date || '').localeCompare(a.date || ''));
  const totalCount = Object.values(_history).reduce((a, v) => a + Object.keys(v).length, 0);
  document.getElementById('stat-hist').textContent = totalCount;
  document.getElementById('stat-disk').textContent = (totalSize / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  tbody.innerHTML = rows.slice(0, 500).map(r => `
    <tr>
      <td>${escapeHtml(r.perf)}</td>
      <td><span class="pill">${escapeHtml(r.site || '')}</span></td>
      <td>${escapeHtml((r.title||'').slice(0,80))}</td>
      <td>${((r.filesize||0)/1024/1024).toFixed(1)} MB</td>
      <td>${escapeHtml((r.date||'').slice(0,16).replace('T',' '))}</td>
      <td><button onclick="playVideo('${escapeHtml(r.output||'')}')">▶</button></td>
    </tr>
  `).join('');
  if (rows.length > 500) {
    tbody.innerHTML += `<tr><td colspan="6" style="text-align:center;color:#7a8ba0;">
      ...+${rows.length - 500} more (filter to narrow)</td></tr>`;
  }
}

function renderFailed() {
  const tbody = document.getElementById('fail-body');
  const rows = Object.entries(_failed).map(([gid, info]) => ({gid, ...info}));
  const permCount = rows.filter(r => r.permanent).length;
  document.getElementById('stat-fail').textContent = permCount;
  document.getElementById('fail-count').textContent = rows.length;
  rows.sort((a,b) => (b.date || '').localeCompare(a.date || ''));
  tbody.innerHTML = rows.slice(0, 200).map(r => `
    <tr>
      <td>${escapeHtml(r.gid)}</td>
      <td>${escapeHtml(r.site || '')}</td>
      <td>${escapeHtml((r.reason||'').slice(0,60))}</td>
      <td>${r.fail_count || 0}</td>
      <td>${r.permanent ? '<span class="pill fail">yes</span>' : ''}</td>
    </tr>
  `).join('');
}

function playVideo(path) {
  if (!path) { toast('No file path', 'error'); return; }
  window.open('/file?path=' + encodeURIComponent(path));
}

async function startDownload() {
  if (!confirm('Start download for ' +
    (_config.performers || []).length + ' performers?')) return;
  try {
    await api('/api/run', {method: 'POST'});
    toast('Started', 'success');
    setTimeout(refreshStatus, 1000);
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function runSinglePerformer() {
  if (!_selectedPerformer) { toast('Select a performer (click it) first', 'error'); return; }
  try {
    await api('/api/run', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({performer: _selectedPerformer})});
    toast('Started ' + _selectedPerformer, 'success');
    setTimeout(refreshStatus, 1000);
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function stopDownload() {
  if (!confirm('Stop running download?')) return;
  try {
    await api('/api/stop', {method:'POST'});
    toast('Stopped');
    setTimeout(refreshStatus, 1000);
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function runDedup() {
  if (!confirm('Run dedup? This will DELETE duplicate video files (keeps the best copy).')) return;
  try {
    const r = await api('/api/dedup', {method:'POST'});
    toast(r.message || 'Dedup complete', 'success');
    loadHistory();
  } catch(e) { toast('Error: ' + e.message, 'error'); }
}

async function refreshAll() {
  await loadSites();
  await loadConfig();
  await loadHistory();
  await refreshStatus();
  toast('Refreshed');
}

// Initial load
(async () => {
  await loadSites();
  await loadConfig();
  await loadHistory();
  await refreshStatus();
  setInterval(refreshStatus, 2000);
  setInterval(loadHistory, 15000);
})();
</script>
</body>
</html>
"""


# ── Background runner ────────────────────────────────────────────────────────
def _tail_log():
    """Continuously read tail of universal.log into _state['log_tail']."""
    last_size = 0
    while True:
        try:
            if LOG_PATH.exists():
                size = LOG_PATH.stat().st_size
                if size < last_size:
                    last_size = 0  # log rotated
                if size > last_size:
                    with open(LOG_PATH, "rb") as f:
                        f.seek(last_size)
                        new_data = f.read()
                        last_size = size
                    for line in new_data.decode("utf-8", errors="replace").splitlines():
                        with _state_lock:
                            _state["log_tail"].append(line)
                            # Try to extract current performer
                            if "Searching for: " in line or "───" in line:
                                # ─── performer ───
                                m = line.strip().replace("─", "").strip()
                                if m and len(m) < 50:
                                    _state["current_performer"] = m
        except Exception:
            pass
        time.sleep(1.5)


_tail_thread = threading.Thread(target=_tail_log, daemon=True)
_tail_thread.start()


def _monitor_subprocess():
    """Monitor the download subprocess and update _state when done."""
    global _runner_thread
    while True:
        with _state_lock:
            proc = _runner_thread
        if proc is not None:
            proc.wait()
            with _state_lock:
                _state["running"] = False
                _state["pid"] = None
                _runner_thread = None
        time.sleep(0.5)


threading.Thread(target=_monitor_subprocess, daemon=True).start()


# ── API routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/api/status")
def api_status():
    with _state_lock:
        return jsonify({
            "running": _state["running"],
            "pid": _state["pid"],
            "started_at": _state["started_at"],
            "current_performer": _state["current_performer"],
            "log_tail": list(_state["log_tail"])[-200:],
        })


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        new_cfg = request.get_json(force=True)
        # Merge (don't wipe fields we don't know about)
        cur = load_config()
        cur.update(new_cfg)
        save_config(cur)
        return jsonify({"ok": True})
    return jsonify(load_config())


@app.route("/api/config/performer/add", methods=["POST"])
def api_add_performer():
    name = (request.get_json(force=True).get("name") or "").strip()
    if not name:
        return jsonify({"error": "empty name"}), 400
    cfg = load_config()
    perfs = cfg.setdefault("performers", [])
    if name not in perfs:
        perfs.append(name)
        save_config(cfg)
    return jsonify({"ok": True, "performers": perfs})


@app.route("/api/config/performer/remove", methods=["POST"])
def api_remove_performer():
    name = (request.get_json(force=True).get("name") or "").strip()
    cfg = load_config()
    perfs = cfg.get("performers", [])
    cfg["performers"] = [p for p in perfs if p != name]
    save_config(cfg)
    return jsonify({"ok": True, "performers": cfg["performers"]})


@app.route("/api/sites")
def api_sites():
    return jsonify({"sites": load_sites()})


@app.route("/api/history")
def api_history():
    return jsonify(load_json(HISTORY_PATH))


@app.route("/api/failed")
def api_failed():
    return jsonify(load_json(FAILED_PATH))


@app.route("/api/run", methods=["POST"])
def api_run():
    global _runner_thread
    with _state_lock:
        if _state["running"]:
            return jsonify({"error": "already running"}), 400

    # Optional: specific performer
    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}
    performer = body.get("performer")

    cmd = [sys.executable, str(SCRIPT_DIR / "universal_downloader.py")]
    if performer:
        cmd.append(performer)
    else:
        cmd.append("--all")

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(SCRIPT_DIR),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    with _state_lock:
        _state["running"] = True
        _state["pid"] = proc.pid
        _state["started_at"] = datetime.now().isoformat()
        _state["current_performer"] = performer or "(all)"
        _runner_thread = proc
    return jsonify({"ok": True, "pid": proc.pid})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global _runner_thread
    with _state_lock:
        proc = _runner_thread
    if not proc:
        return jsonify({"error": "not running"}), 400
    try:
        # On Windows, terminate kills the process tree
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                          capture_output=True, timeout=10)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    with _state_lock:
        _state["running"] = False
        _state["pid"] = None
        _runner_thread = None
    return jsonify({"ok": True})


@app.route("/api/dedup", methods=["POST"])
def api_dedup():
    try:
        r = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "dedupe.py"), "--apply"],
            capture_output=True, text=True, cwd=str(SCRIPT_DIR), timeout=600,
        )
        # Parse last "GRAND TOTAL" line
        out = r.stdout or ""
        message = "Dedup complete."
        for line in out.splitlines():
            if "GRAND TOTAL" in line or "freed" in line:
                message = line.strip()
        return jsonify({"ok": True, "message": message, "stdout": out[-2000:]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/file")
def api_file():
    """Serve a video file from disk (for inline playback)."""
    path = request.args.get("path", "")
    if not path:
        return "path required", 400
    # Safety: only allow files inside downloads/
    p = Path(path).resolve()
    if not str(p).startswith(str(DOWNLOADS_DIR.resolve())):
        return "access denied", 403
    if not p.exists() or not p.is_file():
        return "not found", 404
    return send_file(str(p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7860)
    args = ap.parse_args()
    # Force UTF-8 output on Windows cp1252 consoles
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    msg = f"\n==  Harvestr UI running at http://{args.host}:{args.port}  ==\n"
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
