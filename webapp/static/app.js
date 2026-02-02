const state = {
  concat: {
    filesA: [],
    filesB: [],
    jobId: null,
    poller: null,
  },
  ugc: {
    files: [],
    add1: null,
    add2: null,
    clip: null,
    jobId: null,
    poller: null,
  },
};

const qs = (sel) => document.querySelector(sel);
const qsa = (sel) => Array.from(document.querySelectorAll(sel));

function setActiveTab(tabId) {
  qsa('.tabs button').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.tab === tabId);
  });
  qsa('.tab').forEach((tab) => {
    tab.classList.toggle('active', tab.id === tabId);
  });
}

qsa('.tabs button').forEach((btn) => {
  btn.addEventListener('click', () => setActiveTab(btn.dataset.tab));
});

function formatList(list) {
  if (!list.length) return 'No files uploaded.';
  return list.map((item) => `• ${item.name}`).join('\n');
}

async function uploadFiles(files, role, listEl, stateKey, field) {
  const uploads = [];
  for (const file of files) {
    const form = new FormData();
    form.append('file', file);
    if (role) form.append('role', role);
    const res = await fetch('/api/uploads', { method: 'POST', body: form });
    if (!res.ok) {
      throw new Error('Upload failed');
    }
    const data = await res.json();
    uploads.push(data);
  }
  state[stateKey][field] = uploads.map((u) => u.id);
  listEl.textContent = formatList(uploads);
}

async function uploadSingle(file, role, stateKey, field) {
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  if (role) form.append('role', role);
  const res = await fetch('/api/uploads', { method: 'POST', body: form });
  if (!res.ok) {
    throw new Error('Upload failed');
  }
  const data = await res.json();
  state[stateKey][field] = data.id;
}

function clearConcat() {
  state.concat.filesA = [];
  state.concat.filesB = [];
  qs('#concat-a-list').textContent = 'No files uploaded.';
  qs('#concat-b-list').textContent = 'No files uploaded.';
  qs('#concat-a').value = '';
  qs('#concat-b').value = '';
}

function clearUgc() {
  state.ugc.files = [];
  state.ugc.add1 = null;
  state.ugc.add2 = null;
  state.ugc.clip = null;
  qs('#ugc-videos-list').textContent = 'No files uploaded.';
  qs('#ugc-videos').value = '';
  qs('#ugc-folder').value = '';
  qs('#ugc-add1').value = '';
  qs('#ugc-add2').value = '';
  qs('#ugc-clip').value = '';
}

qs('#concat-a').addEventListener('change', async (e) => {
  try {
    await uploadFiles(e.target.files, 'concat_a', qs('#concat-a-list'), 'concat', 'filesA');
  } catch (err) {
    alert(err.message);
  }
});

qs('#concat-b').addEventListener('change', async (e) => {
  try {
    await uploadFiles(e.target.files, 'concat_b', qs('#concat-b-list'), 'concat', 'filesB');
  } catch (err) {
    alert(err.message);
  }
});

qs('#concat-clear').addEventListener('click', clearConcat);

qs('#ugc-videos').addEventListener('change', async (e) => {
  try {
    await uploadFiles(e.target.files, 'ugc', qs('#ugc-videos-list'), 'ugc', 'files');
  } catch (err) {
    alert(err.message);
  }
});

qs('#ugc-folder').addEventListener('change', async (e) => {
  try {
    await uploadFiles(e.target.files, 'ugc', qs('#ugc-videos-list'), 'ugc', 'files');
  } catch (err) {
    alert(err.message);
  }
});

qs('#ugc-add1').addEventListener('change', async (e) => {
  try {
    await uploadSingle(e.target.files[0], 'ugc_add1', 'ugc', 'add1');
  } catch (err) {
    alert(err.message);
  }
});

qs('#ugc-add2').addEventListener('change', async (e) => {
  try {
    await uploadSingle(e.target.files[0], 'ugc_add2', 'ugc', 'add2');
  } catch (err) {
    alert(err.message);
  }
});

qs('#ugc-clip').addEventListener('change', async (e) => {
  try {
    await uploadSingle(e.target.files[0], 'ugc_clip', 'ugc', 'clip');
  } catch (err) {
    alert(err.message);
  }
});

qs('#ugc-clear').addEventListener('click', clearUgc);

function overlayPayload(prefix) {
  const enabled = qs(`#overlay-${prefix}-enable`).checked;
  if (!enabled) return null;
  return {
    text: qs(`#overlay-${prefix}-text`).value,
    x: Number(qs(`#overlay-${prefix}-x`).value || 0),
    y: Number(qs(`#overlay-${prefix}-y`).value || 0),
    duration: Number(qs(`#overlay-${prefix}-duration`).value || 0),
    font_size: Number(qs(`#overlay-${prefix}-size`).value || 48),
    font_color: qs(`#overlay-${prefix}-color`).value || 'white',
  };
}

qs('#concat-start').addEventListener('click', async () => {
  if (!state.concat.filesA.length || !state.concat.filesB.length) {
    alert('Upload files for both A and B.');
    return;
  }

  const payload = {
    files_a: state.concat.filesA,
    files_b: state.concat.filesB,
    order: qs('#concat-order').value,
    crf: Number(qs('#concat-crf').value || 18),
    try_fast_copy: qs('#concat-fast').value === 'true',
    flat_folder: 'flat',
    nested_folder: 'nested',
    overlay_a: overlayPayload('a'),
    overlay_b: overlayPayload('b'),
  };

  const res = await fetch('/api/jobs/concat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const err = await res.json();
    alert(err.detail || 'Failed to start job');
    return;
  }

  const data = await res.json();
  state.concat.jobId = data.job_id;
  startPolling('concat');
});

qs('#ugc-start').addEventListener('click', async () => {
  if (!state.ugc.files.length) {
    alert('Upload UGC videos.');
    return;
  }

  const payload = {
    files: state.ugc.files,
    add1_file: state.ugc.add1,
    add2_file: state.ugc.add2,
    clip_end_file: state.ugc.clip,
    add1_x: Number(qs('#ugc-add1-x').value || 190),
    add1_y: Number(qs('#ugc-add1-y').value || 890),
    add2_opacity: Number(qs('#ugc-add2-opacity').value || 0.5),
    crf: Number(qs('#ugc-crf').value || 18),
    enable_captions: qs('#ugc-captions').checked,
    api_key: qs('#ugc-api-key').value || null,
  };

  const res = await fetch('/api/jobs/ugc', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const err = await res.json();
    alert(err.detail || 'Failed to start job');
    return;
  }

  const data = await res.json();
  state.ugc.jobId = data.job_id;
  startPolling('ugc');
});

function encodePath(path) {
  return path
    .split('/')
    .map((part) => encodeURIComponent(part))
    .join('/');
}

async function pollJob(section) {
  const jobId = state[section].jobId;
  if (!jobId) return;

  const jobRes = await fetch(`/api/jobs/${jobId}`);
  if (!jobRes.ok) return;
  const job = await jobRes.json();

  qs(`#${section}-job-id`).textContent = job.id || '-';
  qs(`#${section}-job-state`).textContent = job.status || 'unknown';

  const current = job.progress?.current || 0;
  const total = job.progress?.total || 0;
  const percent = total ? Math.min(100, Math.round((current / total) * 100)) : 0;
  qs(`#${section}-progress`).style.width = `${percent}%`;

  if (job.summary) {
    qs(`#${section}-summary`).textContent = JSON.stringify(job.summary);
  }

  const logsRes = await fetch(`/api/jobs/${jobId}/logs?tail=200`);
  if (logsRes.ok) {
    const logs = await logsRes.json();
    qs(`#${section}-logs`).textContent = logs.logs || 'No logs yet.';
  }

  if (section !== 'ugc' && section !== 'concat' && job.outputs && job.outputs.length) {
    const outputEl = qs(`#${section}-outputs`);
    outputEl.innerHTML = '';
    job.outputs.forEach((path) => {
      const link = document.createElement('a');
      link.href = `/api/jobs/${jobId}/download/${encodePath(path)}`;
      link.textContent = path;
      link.target = '_blank';
      outputEl.appendChild(link);
    });
  }

  if (job.status === 'finished' || job.status === 'failed') {
    clearInterval(state[section].poller);
    state[section].poller = null;
  }

  if (section === 'ugc') {
    const downloadAll = qs('#ugc-download-all');
    const zipReady = Boolean(job.summary && job.summary.zip_ready);
    if (zipReady && job.status === 'finished') {
      downloadAll.disabled = false;
      downloadAll.classList.add('ready');
      downloadAll.onclick = () => {
        window.location.href = `/api/jobs/${jobId}/download-zip`;
      };
    } else {
      downloadAll.disabled = true;
      downloadAll.classList.remove('ready');
      downloadAll.onclick = null;
    }
  }

  if (section === 'concat') {
    const flatBtn = qs('#concat-download-flat');
    const nestedBtn = qs('#concat-download-nested');
    const flatReady = Boolean(job.summary && job.summary.flat_zip_ready);
    const nestedReady = Boolean(job.summary && job.summary.nested_zip_ready);

    if (flatReady && job.status === 'finished') {
      flatBtn.disabled = false;
      flatBtn.classList.add('ready');
      flatBtn.onclick = () => {
        window.location.href = `/api/jobs/${jobId}/download-zip/flat`;
      };
    } else {
      flatBtn.disabled = true;
      flatBtn.classList.remove('ready');
      flatBtn.onclick = null;
    }

    if (nestedReady && job.status === 'finished') {
      nestedBtn.disabled = false;
      nestedBtn.classList.add('ready');
      nestedBtn.onclick = () => {
        window.location.href = `/api/jobs/${jobId}/download-zip/nested`;
      };
    } else {
      nestedBtn.disabled = true;
      nestedBtn.classList.remove('ready');
      nestedBtn.onclick = null;
    }
  }
}

function startPolling(section) {
  if (state[section].poller) {
    clearInterval(state[section].poller);
  }
  pollJob(section);
  state[section].poller = setInterval(() => pollJob(section), 3000);
}
