/* Applies the saved appearance before the page paints (no flash of the wrong theme).
   Loaded synchronously in <head>; the page's CSP allows no inline script. */
'use strict';
(function () {
  const THEMES = [
    { id: 'system', name: 'System' },
    { id: 'light', name: 'Light' },
    { id: 'ash', name: 'Ash' },
    { id: 'dark', name: 'Dark' },
    { id: 'onyx', name: 'Onyx' },
  ];
  const COLORS = [
    { id: 'none', name: 'Neutral' },
    { id: 'ocean', name: 'Ocean' },
    { id: 'forest', name: 'Forest' },
    { id: 'sunset', name: 'Sunset' },
    { id: 'lavender', name: 'Lavender' },
    { id: 'rose', name: 'Rose' },
    { id: 'sand', name: 'Sand' },
    { id: 'slate', name: 'Slate' },
    { id: 'crimson', name: 'Crimson' },
  ];
  const root = document.documentElement;
  const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;

  function read(key, fallback, allowed) {
    try {
      const value = JSON.parse(localStorage.getItem('mirrordesk.' + key));
      return allowed.some((item) => item.id === value) ? value : fallback;
    } catch (e) { return fallback; }
  }
  function write(key, value) {
    try { localStorage.setItem('mirrordesk.' + key, JSON.stringify(value)); } catch (e) { /* private mode */ }
  }

  let theme = read('theme', 'system', THEMES);
  let color = read('color', 'none', COLORS);
  const listeners = [];

  function resolved() {
    if (theme !== 'system') return theme;
    return media && media.matches ? 'light' : 'dark';
  }

  function apply() {
    const active = resolved();
    root.dataset.theme = active;
    root.dataset.color = color;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      const bg = getComputedStyle(root).getPropertyValue('--bg').trim();
      if (bg) meta.setAttribute('content', bg);
    }
    listeners.forEach((fn) => fn(active));
  }

  if (media) {
    const changed = () => { if (theme === 'system') apply(); };
    if (media.addEventListener) media.addEventListener('change', changed); else if (media.addListener) media.addListener(changed);
  }
  apply();
  document.addEventListener('DOMContentLoaded', apply);  // theme-color meta exists by now

  window.mirrordeskTheme = {
    THEMES, COLORS,
    get theme() { return theme; },
    get color() { return color; },
    get resolved() { return resolved(); },
    set(next) {
      if (next.theme && THEMES.some((t) => t.id === next.theme)) { theme = next.theme; write('theme', theme); }
      if (next.color && COLORS.some((c) => c.id === next.color)) { color = next.color; write('color', color); }
      apply();
    },
    onChange(fn) { listeners.push(fn); },
  };
})();
