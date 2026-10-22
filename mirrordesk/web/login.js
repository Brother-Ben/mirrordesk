'use strict';
(function () {
  const form = document.getElementById('login-form');
  const message = document.getElementById('login-msg');
  const params = new URLSearchParams(location.search);
  // Already watching with the viewer code: this page upgrades to the owner code.
  fetch('/api/status', { credentials: 'same-origin' }).then((r) => (r.ok ? r.json() : null)).then((s) => {
    if (!s || s.client.role !== 'viewer') return;
    document.getElementById('login-title').textContent = 'Sign in as the owner';
    document.getElementById('login-hint').textContent = 'This device can only watch. Enter the owner code to change settings.';
    document.getElementById('login-back').hidden = false;
  }).catch(() => {});

  async function signIn() {
    message.textContent = '';
    try {
      const response = await fetch('/api/login', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-MirrorDesk': '1' },
        body: JSON.stringify({ code: document.getElementById('code').value }),
      });
      if (response.ok) { location.replace('/'); return; }
      message.textContent = response.status === 429 ? 'Too many attempts. Wait a minute and try again.' : 'That code is not right.';
    } catch (error) {
      message.textContent = 'Cannot reach MirrorDesk: ' + error.message;
    }
  }

  form.onsubmit = (event) => { event.preventDefault(); signIn(); };
  // An invitation link (Viewers page) carries the viewer code: sign in at once.
  if (params.get('code')) {
    document.getElementById('code').value = params.get('code');
    history.replaceState(null, '', '/login');   // the code does not stay in the address bar or history
    signIn();
  }
})();
