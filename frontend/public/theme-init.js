// Blocking same-origin bootstrap precedes styles and Vue, including under script-src 'self'.
;(function () {
  var preference = 'system'
  try {
    var stored = localStorage.getItem('investment-theme')
    if (stored === 'light' || stored === 'dark') preference = stored
  } catch (_) {}
  var dark = preference === 'dark' ||
    (preference === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', dark)
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light'
})()
