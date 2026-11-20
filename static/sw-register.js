// Register the service worker. Loaded as an external script because Persman's
// Content-Security-Policy (default-src 'self') blocks inline <script>.
(function () {
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/service-worker.js').catch(function () {});
    });
  }
})();
