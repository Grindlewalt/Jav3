import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { Button, Input, Select } from '../components/index.js'

// First run: the one page a fresh server shows before anyone can log in.
// App.jsx routes here while GET /api/setup/status says `needed` and away once
// it does not; the backend refuses every /api/setup call after the first user
// exists, so this page is a door that closes behind the operator.
//
// Three steps on one sheet, no wizard chrome: the login, an optional model
// provider (tested before anything is saved), finish. The provider list comes
// from /api/setup/providers, which serves PR1's catalogue when it exists; the
// seed below is only for a server too old to answer at all.

const SEED = [
  { id: 'deepseek', label: 'DeepSeek', needs_key: true },
  { id: 'openai', label: 'OpenAI', needs_key: true },
  { id: 'anthropic', label: 'Anthropic', needs_key: true },
  { id: 'google', label: 'Google', needs_key: true },
  { id: 'openrouter', label: 'OpenRouter', needs_key: true },
  { id: 'ollama', label: 'Ollama (local, no key)', needs_key: false },
]
const MIN_PASSWORD = 8

// A hint, not a gate: the server enforces only the minimum length.
function strength(pw) {
  if (!pw) return null
  if (pw.length < MIN_PASSWORD) return { label: `at least ${MIN_PASSWORD} characters`, tone: 'warn' }
  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((r) => r.test(pw)).length
  const score = (pw.length >= 12) + (pw.length >= 16) + (classes >= 3) + (/\s/.test(pw))
  if (score >= 2) return { label: 'strong', tone: 'good' }
  if (score === 1) return { label: 'fair — longer is stronger', tone: '' }
  return { label: 'weak — a few unrelated words beat one clever one', tone: 'warn' }
}

export default function Setup({ onDone }) {
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [providers, setProviders] = useState(null)
  const [provider, setProvider] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [test, setTest] = useState(null)    // {busy} | {ok, text}
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    api('/api/setup/providers')
      .then((r) => setProviders(r.providers?.length ? r.providers : SEED))
      .catch(() => setProviders(SEED))
  }, [])

  const entry = providers?.find((p) => p.id === provider)
  const showBase = entry && (!entry.needs_key || entry.needs_base_url)
  const hint = strength(password)
  const mismatch = confirm && confirm !== password

  function pick(id) {
    setProvider(id)
    setApiKey('')
    setBaseUrl('')
    setTest(null)
  }

  async function runTest() {
    setTest({ busy: true })
    try {
      const r = await api('/api/setup/test', {
        method: 'POST',
        body: JSON.stringify({ provider, api_key: apiKey, base_url: baseUrl }),
      })
      const n = r.models_found?.length || 0
      setTest(r.ok ? { ok: true, text: `ok · ${n} model${n === 1 ? '' : 's'}` }
                   : { ok: false, text: r.detail || 'test failed' })
    } catch (err) {
      setTest({ ok: false, text: err.status === 404
        ? 'this server cannot test providers yet — finish and add the key in Settings'
        : (err.detail || 'test failed') })
    }
  }

  async function finish(e) {
    e.preventDefault()
    setError(null)
    if (password !== confirm) { setError('the passwords do not match'); return }
    setBusy(true)
    try {
      const body = { username, password }
      if (provider) Object.assign(body, { provider, api_key: apiKey, base_url: baseUrl })
      const r = await api('/api/setup', { method: 'POST', body: JSON.stringify(body) })
      onDone({ username: r.username })
      navigate('/', { replace: true })
    } catch (err) {
      setBusy(false)
      if (err.status === 409) { onDone(null); navigate('/login', { replace: true }); return }
      setError(err.detail || 'setup failed')
    }
  }

  const needsKey = entry?.needs_key && !apiKey.trim()
  const needsBase = entry?.needs_base_url && !baseUrl.trim()
  const ready = username.trim() && password.length >= MIN_PASSWORD && !mismatch
    && confirm && !needsKey && !needsBase

  return (
    <div className="setup-wrap">
      <form className="login setup" onSubmit={finish}>
        <h1>Jav3</h1>

        <section className="setup-step">
          <h2><span className="setup-num">1</span>Create your login</h2>
          <Input label="Username" value={username} autoFocus
                 autoComplete="username" autoCapitalize="none" spellCheck={false}
                 onChange={(e) => setUsername(e.target.value)} />
          <Input label="Password" type="password" value={password}
                 autoComplete="new-password"
                 onChange={(e) => setPassword(e.target.value)}
                 hint={hint && <span className={`setup-strength ${hint.tone}`}>{hint.label}</span>} />
          <Input label="Confirm password" type="password" value={confirm}
                 autoComplete="new-password"
                 onChange={(e) => setConfirm(e.target.value)}
                 error={mismatch ? 'does not match' : null} />
        </section>

        <section className="setup-step">
          <h2><span className="setup-num">2</span>Connect a model provider</h2>
          <Select label="Provider" value={provider} disabled={!providers}
                  onChange={(e) => pick(e.target.value)}
                  options={[{ value: '', label: 'Skip for now' },
                            ...(providers || []).map((p) => ({ value: p.id, label: p.label }))]} />
          {entry?.needs_key && (
            <Input label="API key" type="password" value={apiKey}
                   autoComplete="off" spellCheck={false} placeholder="paste it here"
                   onChange={(e) => { setApiKey(e.target.value); setTest(null) }} />
          )}
          {showBase && (
            <Input label={entry.needs_base_url ? 'Base URL' : 'Base URL (optional)'}
                   value={baseUrl} placeholder={entry.base_url || ''}
                   autoComplete="off" spellCheck={false} inputMode="url"
                   onChange={(e) => { setBaseUrl(e.target.value); setTest(null) }} />
          )}
          {entry && (
            <div className="setup-test">
              <Button variant="ghost" onClick={runTest}
                      disabled={test?.busy || needsKey || needsBase}>
                {test?.busy ? 'Testing…' : 'Test'}</Button>
              {test && !test.busy && (
                <span className={test.ok ? 'setup-ok' : 'error'} role="status">{test.text}</span>
              )}
            </div>
          )}
          <p className="field-hint setup-where">
            Keys are stored on this server, never in the sandbox VM.</p>
        </section>

        {error && <div className="error" role="alert">{error}</div>}
        <Button type="submit" disabled={!ready || busy}>
          {busy ? 'Finishing…' : 'Finish'}</Button>
      </form>
    </div>
  )
}
