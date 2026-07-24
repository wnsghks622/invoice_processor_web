// Mode toggle (Simple / Advanced). Persists in localStorage.
(function () {
  const KEY = 'ip-web-mode';
  const html = document.documentElement;
  const toggle = document.getElementById('mode-toggle');
  const saved = localStorage.getItem(KEY) || 'simple';
  html.setAttribute('data-mode', saved);
  if (toggle) {
    toggle.checked = saved === 'advanced';
    toggle.addEventListener('change', () => {
      const mode = toggle.checked ? 'advanced' : 'simple';
      html.setAttribute('data-mode', mode);
      localStorage.setItem(KEY, mode);
    });
  }
})();
