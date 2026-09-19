/* Dashboard client. Polls data/state.json (via /api/state) every 2s and re-renders.
   No framework: the state document is small and fully replaces the view each tick,
   so there is nothing to reconcile. */

'use strict';

const PHASES = [
  ['analyzing', 'Analyse logs'],
  ['implementing', 'Agent codes'],
  ['running_ab', 'Eval + A/B'],
  ['decided', 'Decide'],
  ['idle', 'Idle'],
];

const STATUS_STYLE = {
  adopted: ['pill-good', 'auto-adopted'],
  pending_approval: ['pill-warn', 'awaiting approval'],
  rolled_back: ['pill-dim', 'rolled back'],
  rollback: ['pill-dim', 'rolled back'],
  invalid: ['pill-bad', 'invalid'],
  baseline: ['pill-accent', 'baseline'],
  analyzing: ['pill-accent', 'analysing'],
  implementing: ['pill-accent', 'implementing'],
  testing: ['pill-accent', 'testing'],
};

const pct = (v, d = 1) => (v == null ? '–' : `${(v * 100).toFixed(d)}%`);
const signed = (v, d = 1) => (v == null ? '–' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(d)}pp`);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let lastSerialised = '';
const openDiffs = new Set();
const shotViewport = new Map();

/* ------------------------------------------------------------------- pipeline */

function renderPipeline(state) {
  const host = document.getElementById('pipeline');
  const activeIdx = PHASES.findIndex(([k]) => k === state.phase);
  host.replaceChildren(...PHASES.map(([key, label], i) => {
    const li = el('li');
    if (key === state.phase) li.classList.add('active');
    else if (activeIdx > i || (state.phase === 'idle' && state.rounds?.length)) li.classList.add('done');
    li.append(el('i', null, String(i + 1)), el('span', null, label));
    return li;
  }));
}

/* ---------------------------------------------------------------------- chart */

function renderChart(state) {
  const host = document.getElementById('chart');
  const series = state.conversion_series || [];
  if (!series.length) {
    host.replaceChildren(el('p', 'shot-empty', 'No rounds yet.'));
    return;
  }

  const W = 1080, H = 320, padL = 54, padR = 42, padT = 40, padB = 56;
  const vals = series.flatMap((p) => [p.control, p.treatment]).filter((v) => v != null);
  const holdouts = (state.rounds || []).flatMap((r) => Object.values(r.holdout || {})).filter((v) => v != null);
  const peak = Math.max(0.1, ...vals, ...holdouts);
  // Round the axis up to a clean 10% step so the gridline labels read as percentages
  // a human would choose, not as fractions of an arbitrary maximum.
  const maxV = Math.min(1, Math.ceil((peak * 1.18) / 0.1) * 0.1);
  const x = (i) => padL + (series.length === 1 ? (W - padL - padR) / 2 : (i * (W - padL - padR)) / (series.length - 1));
  const y = (v) => H - padB - (v / maxV) * (H - padT - padB);

  const parts = [];
  const ticks = Math.round(maxV / 0.1);
  for (let t = 0; t <= ticks; t++) {
    const v = t * 0.1;
    parts.push(`<line class="grid-line" x1="${padL}" y1="${y(v)}" x2="${W - padR}" y2="${y(v)}"/>`);
    parts.push(`<text class="axis-label" x="${padL - 10}" y="${y(v) + 3.5}" text-anchor="end">${(v * 100).toFixed(0)}%</text>`);
  }
  // Outcome marker under the axis rather than a full-height band: the chart is about
  // the rates, and a column tall enough to tint the plot area competes with them.
  series.forEach((p, i) => {
    if (!p.adopted && !p.pending) return;
    parts.push(`<rect class="${p.adopted ? 'adopt-band' : 'pending-band'}" x="${x(i) - 26}" y="${H - padB + 26}" width="52" height="5" rx="2.5"/>`);
    parts.push(`<text class="outcome-tag ${p.adopted ? 'lift-up' : 'pending-text'}" x="${x(i)}" y="${H - padB + 45}" text-anchor="middle">${p.adopted ? 'adopted' : 'pending'}</text>`);
  });
  // control line
  const ctrlPts = series.map((p, i) => `${x(i)},${y(p.control ?? 0)}`).join(' ');
  parts.push(`<polyline class="series-control" points="${ctrlPts}"/>`);
  // treatment segments (control -> treatment for the same round)
  series.forEach((p, i) => {
    if (p.treatment == null) return;
    parts.push(`<line class="series-treat" x1="${x(i)}" y1="${y(p.control)}" x2="${x(i)}" y2="${y(p.treatment)}"/>`);
  });
  const treatPts = series.filter((p) => p.treatment != null);
  if (treatPts.length > 1) {
    parts.push(`<polyline class="series-treat" points="${series.map((p, i) => (p.treatment != null ? `${x(i)},${y(p.treatment)}` : null)).filter(Boolean).join(' ')}"/>`);
  }
  // points + labels
  series.forEach((p, i) => {
    const cx = x(i);
    const cy = y(p.control ?? 0);
    // Round 0 sits on the axis, so a centred label lands on top of the gridline
    // percentages; nudge the first and last points' labels inwards.
    const lx = i === 0 ? cx + 16 : i === series.length - 1 ? cx - 8 : cx;
    parts.push(`<circle class="pt pt-control" cx="${cx}" cy="${cy}" r="5"/>`);
    if (p.treatment != null) {
      const ty = y(p.treatment);
      parts.push(`<circle class="pt pt-treat" cx="${cx}" cy="${ty}" r="5.5"/>`);
      const rel = p.control > 0 ? (p.treatment - p.control) / p.control : 0;
      const up = p.treatment >= p.control;
      // Put the treatment tag on the far side of the control point so the two
      // labels cannot collide when the arms land close together.
      const tagY = up ? ty - 13 : ty + 20;
      parts.push(`<text class="lift-tag ${up ? 'lift-up' : 'lift-down'}" x="${cx}" y="${tagY}" text-anchor="middle">${up ? '+' : ''}${(rel * 100).toFixed(0)}%</text>`);
      parts.push(`<text class="axis-label" x="${lx}" y="${up ? cy + 19 : cy - 12}" text-anchor="middle">${pct(p.control, 0)}</text>`);
    } else {
      parts.push(`<text class="axis-label" x="${lx}" y="${cy + 19}" text-anchor="middle">${pct(p.control, 0)}</text>`);
    }
    const hold = (state.rounds || []).find((r) => r.round === p.round)?.holdout;
    const hv = hold ? (hold.treatment ?? hold.control) : null;
    // Keep the holdout marker inside the plot on the last round.
    if (hv != null) parts.push(`<circle class="pt pt-hold" cx="${Math.min(cx + 13, W - padR - 4)}" cy="${y(hv)}" r="4"/>`);
    parts.push(`<text class="round-tick" x="${cx}" y="${H - padB + 20}" text-anchor="middle">R${p.round}</text>`);
  });

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${parts.join('')}</svg>`;
}

function renderHoldout(state) {
  const host = document.getElementById('holdoutRow');
  const rounds = (state.rounds || []).filter((r) => r.holdout);
  if (!rounds.length) {
    host.replaceChildren(el('span', 'tag', 'holdout'), el('span', null, 'no holdout data yet'));
    return;
  }
  const bits = rounds.map((r) => {
    const h = r.holdout;
    const t = h.treatment != null ? ` → <b>${pct(h.treatment)}</b>` : '';
    return `R${r.round}: <b>${pct(h.control)}</b>${t}`;
  });
  host.innerHTML = `<span class="tag">holdout validation</span>
    <span>${bits.join(' &nbsp;·&nbsp; ')}</span>
    <span style="color:var(--dim)">personas the agent never sees — guards against overfitting to the training mix</span>`;
}

function renderKpis(state) {
  const host = document.getElementById('kpis');
  const series = state.conversion_series || [];
  const first = series[0];
  const latestAdopted = [...series].reverse().find((p) => p.adopted);
  const current = latestAdopted?.treatment ?? series[series.length - 1]?.control ?? null;
  const base = first?.control ?? null;
  const totalRel = base && current ? (current - base) / base : null;
  const adopted = series.filter((p) => p.adopted).length;
  const rounds = (state.rounds || []).filter((r) => r.round > 0).length;

  const cards = [
    ['baseline at round 0', pct(base), ''],
    ['current baseline', pct(current), 'good'],
    ['total relative gain', totalRel == null ? '–' : `${totalRel >= 0 ? '+' : ''}${(totalRel * 100).toFixed(0)}%`, 'accent'],
    ['rounds run', String(rounds), ''],
    ['changes adopted', `${adopted}/${rounds}`, ''],
  ];
  host.replaceChildren(...cards.map(([label, value, cls]) => {
    const d = el('div', `kpi ${cls}`.trim());
    d.append(el('b', null, value), el('span', null, label));
    return d;
  }));
}

/* -------------------------------------------------------------------- friction */

function renderFriction(state) {
  const host = document.getElementById('friction');
  const latest = [...(state.rounds || [])].reverse().find((r) => (r.top_friction || []).length);
  document.getElementById('frictionRound').textContent = latest ? `from round ${latest.round} logs` : '';
  const items = latest?.top_friction || [];
  if (!items.length) {
    host.replaceChildren(el('p', 'shot-empty', 'No friction data yet.'));
    return;
  }
  const max = Math.max(...items.map((f) => f.count || 0), 1);
  host.replaceChildren(...items.map((f) => {
    const row = el('div', 'fr');
    const name = el('div', 'fr-name');
    name.innerHTML = `<code>${esc(f.value)}</code>`;
    const meta = el('div', 'fr-meta');
    meta.textContent = [f.step ? `step ${f.step}` : null, f.device, f.field_count ? `${f.field_count} fields` : null, f.type]
      .filter(Boolean).join(' · ');
    name.append(meta);
    const count = el('div', 'fr-count', `${f.count}`);
    const bar = el('div', 'fr-bar');
    const fill = el('i');
    fill.style.width = `${((f.count || 0) / max) * 100}%`;
    bar.append(fill);
    row.append(name, count, bar);
    return row;
  }));
}

function renderTimeline(state) {
  const host = document.getElementById('timeline');
  const items = [...(state.timeline || [])].reverse().slice(0, 40);
  host.replaceChildren(...items.map((t) => {
    const li = el('li');
    const time = el('time', null, new Date(t.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
    li.append(time, el('span', null, t.msg));
    return li;
  }));
}

/* ----------------------------------------------------------------- round cards */

function colorizeDiff(text) {
  return text.split('\n').slice(0, 900).map((line) => {
    let cls = 'ctx';
    if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ')) cls = 'hdr';
    else if (line.startsWith('@@')) cls = 'hunk';
    else if (line.startsWith('+')) cls = 'add';
    else if (line.startsWith('-')) cls = 'del';
    return `<span class="ln ${cls}">${esc(line) || ' '}</span>`;
  }).join('');
}

function shotsFor(round, vp) {
  const names = round.shots || [];
  const pick = (phase) => names.find((n) => n === `${phase}_${vp}_payment.png`)
    || names.find((n) => n === `${phase}_${vp}.png`)
    || names.find((n) => n.startsWith(`${phase}_${vp}`));
  return { before: pick('before'), after: pick('after') };
}

function renderRound(round) {
  const statusKey = round.status || 'baseline';
  const [pillCls, pillText] = STATUS_STYLE[statusKey] || ['pill-dim', statusKey];
  const card = el('article', `round is-${statusKey === 'pending_approval' ? 'pending' : statusKey === 'adopted' ? 'adopted' : statusKey === 'invalid' ? 'invalid' : 'rolled'}`);

  /* head */
  const head = el('div', 'r-head');
  head.append(el('span', 'r-idx', `round ${round.round}`));
  const title = el('div', 'r-title');
  title.append(el('h3', null, round.title || `Round ${round.round}`));
  const meta = el('p');
  meta.innerHTML = [
    round.hypothesis_id ? `<span class="hyp">${esc(round.hypothesis_id)}</span>` : null,
    round.selection_mode ? `selected by ${esc(round.selection_mode)} ranking` : null,
    round.autonomy && round.autonomy !== 'n/a' ? `autonomy: <b>${esc(round.autonomy)}</b>` : null,
  ].filter(Boolean).join(' &nbsp;·&nbsp; ');
  title.append(meta);
  head.append(title, el('span', `pill ${pillCls}`, pillText));
  card.append(head);

  /* stats */
  if (round.conversion) {
    const stats = el('div', 'r-stats');
    const rows = [
      ['control', pct(round.conversion.control), ''],
      ['treatment', pct(round.conversion.treatment), ''],
      ['absolute lift', signed(round.lift_abs), round.lift_abs > 0 ? 'up' : round.lift_abs < 0 ? 'down' : ''],
      ['relative lift', round.lift_rel == null ? '–' : `${round.lift_rel >= 0 ? '+' : ''}${(round.lift_rel * 100).toFixed(0)}%`, round.lift_rel > 0 ? 'up' : round.lift_rel < 0 ? 'down' : ''],
      ['p-value', round.p_value == null ? '–' : round.p_value.toFixed(3), round.p_value != null && round.p_value < 0.1 ? 'up' : ''],
      ['95% CI', round.ci95 ? `${signed(round.ci95[0], 0)} … ${signed(round.ci95[1], 0)}` : '–', ''],
    ];
    rows.forEach(([label, value, cls]) => {
      const s = el('div', `r-stat ${cls}`.trim());
      s.append(el('b', null, value), el('span', null, label));
      stats.append(s);
    });
    card.append(stats);
  }

  const body = el('div', 'r-body');

  if (round.decision_reason) {
    const tone = statusKey === 'adopted' ? 'good' : statusKey === 'pending_approval' ? 'warn' : statusKey === 'invalid' ? 'bad' : '';
    body.append(el('div', `reason ${tone}`.trim(), round.decision_reason));
  }

  /* approval actions */
  if (statusKey === 'pending_approval') {
    const wrap = el('div');
    wrap.append(el('div', 'sec-h', 'Human decision required'));
    const actions = el('div', 'actions');
    const approve = el('button', 'btn btn-approve', 'Approve & promote');
    const reject = el('button', 'btn btn-reject', 'Reject');
    const note = el('span', 'note', 'The diff touches a path the policy reserves for a human.');
    approve.onclick = () => decide(round.round, 'approve', [approve, reject]);
    reject.onclick = () => decide(round.round, 'reject', [approve, reject]);
    actions.append(approve, reject, note);
    wrap.append(actions);
    body.append(wrap);
  }

  /* guardrails */
  if (round.guardrails || round.diff_stats) {
    const wrap = el('div');
    wrap.append(el('div', 'sec-h', 'Guardrails'));
    const g = el('div', 'guard');
    const gr = round.guardrails || {};
    const ds = round.diff_stats || {};
    const chips = [
      ['smoke / eval gate', gr.smoke_test, gr.smoke_test ? 'passed' : 'failed'],
      ['protected paths', !ds.protected_touched, ds.protected_touched ? 'TOUCHED' : 'untouched'],
      ['http error delta', gr.http_error_delta != null ? gr.http_error_delta <= 0.02 : true,
        gr.http_error_delta == null ? 'n/a' : signed(gr.http_error_delta, 1)],
      ['diff size', !(ds.lines > 150), `${ds.files ?? 0} files / ${ds.lines ?? 0} lines`],
    ];
    chips.forEach(([label, ok, value]) => {
      g.append(el('span', `g ${ok ? 'ok' : 'no'}`, `${label}: ${value}`));
    });
    wrap.append(g);
    if ((ds.paths || []).length) {
      const paths = el('div', 'paths');
      paths.style.marginTop = '9px';
      ds.paths.forEach((p) => {
        const c = el('code', (ds.needs_approval || []).includes(p) ? 'approval' : null, p);
        paths.append(c);
      });
      wrap.append(paths);
    }
    body.append(wrap);
  }

  /* hypotheses */
  if ((round.hypotheses || []).length) {
    const wrap = el('div');
    wrap.append(el('div', 'sec-h', `Hypotheses considered (${round.hypotheses.length} proposed, 1 implemented)`));
    const table = el('table', 'hyp-t');
    table.innerHTML = `<thead><tr><th>#</th><th>hypothesis</th><th>evidence</th><th>sessions</th><th>risk</th><th>status</th></tr></thead>`;
    const tb = el('tbody');
    round.hypotheses.forEach((h, i) => {
      const tr = el('tr', h.selected ? 'sel' : null);
      tr.innerHTML = `<td>${i + 1}</td>
        <td><div>${esc(h.title)}</div><div class="id">${esc(h.id)}</div></td>
        <td class="ev">${esc(h.evidence)}</td>
        <td style="font-family:var(--mono)">${h.friction_sessions ?? 0}</td>
        <td>${esc(h.risk)}</td>
        <td>${h.selected ? '<b style="color:var(--accent)">implemented</b>' : (h.already_shipped ? 'already shipped' : 'deferred')}</td>`;
      tb.append(tr);
    });
    table.append(tb);
    wrap.append(table);
    body.append(wrap);
  }

  /* eval gate */
  if ((round.smoke?.cases || []).length) {
    const wrap = el('div');
    wrap.append(el('div', 'sec-h',
      `Eval gate — ${round.smoke.pass_count}/${round.smoke.case_count} cases passed${round.smoke.passed ? '' : ' · BLOCKED'}`));
    const cases = el('div', 'cases');
    round.smoke.cases.forEach((c) => {
      const d = el('div', `case ${c.passed ? 'pass' : 'fail'}`);
      d.append(el('i', null, c.passed ? '✓' : '✕'), el('span', null, c.case), el('span', 'kind', c.kind));
      if (c.detail) d.append(el('span', 'detail', c.detail));
      cases.append(d);
    });
    wrap.append(cases);
    body.append(wrap);
  }

  /* screenshots */
  if ((round.shots || []).length) {
    const wrap = el('div');
    wrap.append(el('div', 'sec-h', 'Before / after — the page the round changed'));
    const vp = shotViewport.get(round.round) || 'mobile';
    const tabs = el('div', 'shot-tabs');
    ['mobile', 'desktop'].forEach((v) => {
      const b = el('button', null, v);
      b.setAttribute('aria-pressed', String(v === vp));
      b.onclick = () => { shotViewport.set(round.round, v); lastSerialised = ''; tick(); };
      tabs.append(b);
    });
    wrap.append(tabs);

    const { before, after } = shotsFor(round, vp);
    const shots = el('div', 'shots');
    [['before', before, 'baseline'], ['after', after, 'candidate']].forEach(([phase, name, label]) => {
      const fig = el('figure', 'shot');
      const cap = el('figcaption');
      cap.innerHTML = `<span class="tag tag-${phase}">${phase}</span><span>${label} · ${vp}</span>`;
      fig.append(cap);
      if (name) {
        const img = el('img');
        img.src = `/api/file/runs/round_${round.round}/${name}`;
        img.alt = `${label} ${vp} screenshot`;
        img.loading = 'lazy';
        img.onclick = () => openLightbox(img.src, `round ${round.round} · ${phase} · ${vp}`);
        fig.append(img);
      } else {
        fig.append(el('div', 'shot-empty', phase === 'after' ? 'no candidate for this round' : 'not captured'));
      }
      shots.append(fig);
    });
    wrap.append(shots);
    body.append(wrap);
  }

  /* diff + change note */
  if (round.diff_path) {
    const det = el('details', 'diff');
    const key = `r${round.round}`;
    if (openDiffs.has(key)) det.open = true;
    const sum = el('summary');
    sum.innerHTML = `<b>diff.patch</b> <span>${round.diff_stats?.files ?? 0} file(s), ${round.diff_stats?.lines ?? 0} changed line(s)</span>`;
    det.append(sum);
    const pre = el('pre', 'diff-body');
    pre.innerHTML = '<span class="ln ctx">loading…</span>';
    det.append(pre);
    det.addEventListener('toggle', async () => {
      if (det.open) {
        openDiffs.add(key);
        if (!det.dataset.loaded) {
          const res = await fetch(`/api/file/${round.diff_path}`);
          pre.innerHTML = colorizeDiff(res.ok ? await res.text() : 'diff unavailable');
          det.dataset.loaded = '1';
        }
      } else {
        openDiffs.delete(key);
      }
    });
    body.append(det);
  }

  if (round.change_md) {
    const det = el('details', 'diff');
    const key = `c${round.round}`;
    if (openDiffs.has(key)) det.open = true;
    const sum = el('summary');
    sum.innerHTML = `<b>change.md</b> <span>the agent's own write-up of this round</span>`;
    det.append(sum);
    const pre = el('pre', 'diff-body');
    pre.innerHTML = '<span class="ln ctx">loading…</span>';
    det.append(pre);
    det.addEventListener('toggle', async () => {
      if (det.open) {
        openDiffs.add(key);
        if (!det.dataset.loaded) {
          const res = await fetch(`/api/file/${round.change_md}`);
          const text = res.ok ? await res.text() : 'not available';
          pre.innerHTML = text.split('\n').map((l) => `<span class="ln ctx">${esc(l) || ' '}</span>`).join('');
          det.dataset.loaded = '1';
        }
      } else {
        openDiffs.delete(key);
      }
    });
    body.append(det);
  }

  card.append(body);
  return card;
}

async function decide(roundNo, action, buttons) {
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const res = await fetch(`/api/${action}/${roundNo}`, { method: 'POST' });
    const body = await res.json();
    if (!body.ok) alert(`Could not ${action} round ${roundNo}: ${body.error || 'unknown error'}`);
  } catch (err) {
    alert(`Request failed: ${err}`);
  } finally {
    lastSerialised = '';
    tick();
  }
}

/* ------------------------------------------------------------------ lightbox */

function openLightbox(src, caption) {
  document.getElementById('lbImg').src = src;
  document.getElementById('lbCap').textContent = caption;
  document.getElementById('lightbox').hidden = false;
}
document.getElementById('lbClose').onclick = () => { document.getElementById('lightbox').hidden = true; };
document.getElementById('lightbox').onclick = (e) => {
  if (e.target.id === 'lightbox') document.getElementById('lightbox').hidden = true;
};
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') document.getElementById('lightbox').hidden = true;
});

/* ---------------------------------------------------------------------- poll */

function render(state) {
  document.getElementById('roundNo').textContent = state.current_round ?? '–';
  renderPipeline(state);
  renderChart(state);
  renderHoldout(state);
  renderKpis(state);
  renderFriction(state);
  renderTimeline(state);

  const host = document.getElementById('rounds');
  const rounds = [...(state.rounds || [])].sort((a, b) => b.round - a.round);
  if (!rounds.length) {
    const empty = el('div', 'card empty-state');
    empty.innerHTML = `<p>No rounds recorded yet.</p>
      <p>Start one with <code>python orchestrator/run_loop.py --rounds 3 --n 80 --seed 7</code></p>`;
    host.replaceChildren(empty);
  } else {
    host.replaceChildren(...rounds.map(renderRound));
  }
  document.getElementById('updated').textContent = state.updated_at
    ? `state.json updated ${new Date(state.updated_at * 1000).toLocaleTimeString()}`
    : 'no state yet';
}

async function tick() {
  const dot = document.getElementById('liveDot');
  const text = document.getElementById('liveText');
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    const state = await res.json();
    dot.className = 'dot on';
    text.textContent = state.phase === 'idle' ? 'idle' : `${String(state.phase || '').replace(/_/g, ' ')}`;
    const serialised = JSON.stringify(state);
    if (serialised !== lastSerialised) {
      lastSerialised = serialised;
      render(state);
    }
  } catch (err) {
    dot.className = 'dot err';
    text.textContent = 'disconnected';
  }
}

tick();
setInterval(tick, 2000);
