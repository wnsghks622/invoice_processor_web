// Keep your place in the list when a button reloads the page.
//
// Every action here is a form POST that redirects back to the page you were on, so the
// browser fetches it fresh and lands at the top. On a long list - ticking off invoices, or
// working down the month - that means hunting for your row again after every single click.
//
// So: stash the scroll offset when a form is submitted, and put it back on the next load of
// that same URL. Keying on pathname+search matters. Pressing "Open" or changing the month
// lands on a different URL, which is genuinely new content and should start at the top; only
// a redirect back to where you already were restores the offset. The key is cleared once
// used, so arriving by a link or the back button doesn't inherit a stale position.
// Note it is NOT the window that scrolls on most of these pages - the layout puts the list
// inside a container (.table-card on Invoices) that scrolls on its own, so window.scrollY is
// always 0 and saving it would restore nothing. Record every scrollable element in document
// order, and the window too for the pages where that is the scroller.
(function () {
  var key = 'ip-scroll:' + location.pathname + location.search;

  function scrollers() {
    return Array.prototype.filter.call(document.querySelectorAll('*'), function (el) {
      return el.scrollHeight - el.clientHeight > 8 &&
             /(auto|scroll)/.test(getComputedStyle(el).overflowY);
    });
  }

  addEventListener('submit', function () {
    try {
      sessionStorage.setItem(key, JSON.stringify({
        w: window.scrollY,
        e: scrollers().map(function (el) { return el.scrollTop; })
      }));
    } catch (e) { /* private mode, or storage full - not worth breaking the page over */ }
  }, true);   // capture: fires even if a handler stops the event bubbling

  // Returns true once every offset has actually taken. Assigning scrollTop before the
  // container has its full height silently clamps to 0, which is why this retries rather
  // than firing once: on a 200-row table the element is not tall enough to scroll yet at
  // DOMContentLoaded, so a single attempt restores nothing at all.
  function restore(saved) {
    window.scrollTo(0, saved.w || 0);
    var wanted = saved.e || [];
    var done = true;
    scrollers().forEach(function (el, i) {
      var want = wanted[i] || 0;
      if (!want) return;
      el.scrollTop = want;
      if (Math.abs(el.scrollTop - want) > 1) done = false;
    });
    return done;
  }

  addEventListener('DOMContentLoaded', function () {
    var saved;
    try {
      saved = sessionStorage.getItem(key);
      if (saved !== null) sessionStorage.removeItem(key);
    } catch (e) { return; }
    if (!saved) return;
    try { saved = JSON.parse(saved); } catch (e) { return; }

    var tries = 0;
    (function attempt() {
      // Give up after ~20 frames. The row may genuinely have gone - ticking the last item
      // off a list makes the page shorter - and in that case scrolling as far as it now
      // goes is the right answer, not looping forever.
      if (restore(saved) || ++tries > 20) return;
      requestAnimationFrame(attempt);
    })();
  });
})();

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
