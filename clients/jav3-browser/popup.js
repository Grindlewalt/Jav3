const $ = id => document.getElementById(id);
const cmd = msg => chrome.runtime.sendMessage(msg);

function li(text, buttons) {
  const el = document.createElement('li');
  const s = document.createElement('span');
  s.textContent = text;
  el.append(s);
  for (const [label, fn, cls] of buttons) {
    const b = document.createElement('button');
    b.textContent = label;
    if (cls) b.className = cls;
    b.onclick = fn;
    el.append(b);
  }
  return el;
}

async function render() {
  const l = await chrome.storage.local.get({ paused: false, sites: {}, name: '', token: '' });
  const s = await chrome.storage.session.get({ status: 'offline', error: '', asks: [] });
  const status = l.token ? s.status : 'unpaired';
  $('dot').className = 'dot ' + status;
  $('status').textContent = status === 'connected' ? `connected as ${l.name}` : status;
  $('err').textContent = s.error || '';
  $('pause').textContent = l.paused ? 'Resume Jav3' : 'Pause Jav3';
  $('pause').classList.toggle('paused', l.paused);
  $('pause').disabled = !l.token;
  $('pausedNote').textContent = l.paused ? 'Paused: Jav3 cannot use this browser until you resume.' : '';
  $('disconnect').disabled = !l.token;
  $('asksBox').hidden = !s.asks.length;
  $('asks').replaceChildren(...s.asks.map(site => li(`Allow Jav3 on ${site}?`, [
    ['Allow', () => cmd({ cmd: 'answer', site, allow: true }), 'primary'],
    ['Deny', () => cmd({ cmd: 'answer', site, allow: false })]])));
  const entries = Object.entries(l.sites);
  $('sites').replaceChildren(...(entries.length ? entries.map(([site, v]) => li(`${site} — ${v}`, [
    ['Forget', async () => {
      const cur = (await chrome.storage.local.get({ sites: {} })).sites;
      delete cur[site];
      await chrome.storage.local.set({ sites: cur });
    }]])) : [li('none yet; Jav3 asks on its first visit to each site', [])]));
}

$('pause').onclick = async () => {
  const { paused } = await chrome.storage.local.get({ paused: false });
  await cmd({ cmd: paused ? 'resume' : 'pause' });
};
$('disconnect').onclick = async () => {
  if (confirm('Disconnect this browser from Jav3? Its token is revoked; you will need a new code to pair again.')) {
    await cmd({ cmd: 'disconnect' });
  }
};
$('options').onclick = () => chrome.runtime.openOptionsPage();
chrome.storage.onChanged.addListener(render);
render();
