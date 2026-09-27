import { siteKey } from './lib/verbs.js';

const $ = id => document.getElementById(id);

async function render() {
  const l = await chrome.storage.local.get({ token: '', address: '', name: '', notify: true, sites: {} });
  const s = await chrome.storage.session.get({ status: 'offline' });
  $('pairState').textContent = l.token
    ? `Paired with ${l.address} as “${l.name}” (${s.status}). Pairing again replaces it.`
    : 'Not paired.';
  $('notify').checked = l.notify;
  if (document.activeElement !== $('sites')) {
    $('sites').value = Object.entries(l.sites)
      .map(([k, v]) => (v === 'deny' ? '!' : '') + k).join('\n');
  }
}

$('pair').onclick = async () => {
  $('pairMsg').textContent = 'Pairing…';
  const r = await chrome.runtime.sendMessage({ cmd: 'pair', line: $('line').value, name: $('name').value });
  $('line').value = '';
  $('pairMsg').textContent = r && r.ok
    ? `Paired as “${r.name}”.` + (r.plainHttp ? ' Warning: plain http — the token crosses the network unencrypted.' : '')
      + ' Now grant it to a project in Jav3 → Settings → Browser use.'
    : `Failed: ${(r && r.error) || 'unknown error'}`;
};

$('notify').onchange = () => chrome.storage.local.set({ notify: $('notify').checked });

$('saveSites').onclick = async () => {
  const sites = {};
  const bad = [];
  for (const raw of $('sites').value.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const deny = line.startsWith('!');
    const k = siteKey(deny ? line.slice(1) : line);
    if (k) sites[k] = deny ? 'deny' : 'allow';
    else bad.push(line);
  }
  await chrome.storage.local.set({ sites });
  $('sitesMsg').textContent = bad.length ? `Saved; ignored: ${bad.join(', ')}` : 'Saved.';
};

chrome.storage.onChanged.addListener(render);
render();
