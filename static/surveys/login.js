(() => {
  'use strict';
  const form = document.querySelector('[data-login-form]');
  if (!form) return;
  const button = form.querySelector('[type="submit"]');
  const status = form.querySelector('[data-login-status]');
  const originalLabel = button.textContent;
  const english = document.documentElement.lang === 'en';
  let pending = false;
  let controller;
  let sequence = 0;
  const restore = () => {
    pending = false;
    button.disabled = false;
    button.textContent = originalLabel;
    form.removeAttribute('aria-busy');
  };
  window.addEventListener('pageshow', () => {
    sequence++;
    if (controller) controller.abort();
    controller = null;
    restore();
    status.textContent = '';
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (pending) return;
    pending = true;
    const attempt = ++sequence;
    const payload = new FormData(form);
    button.disabled = true;
    button.textContent = english ? 'Processing…' : '처리 중…';
    form.setAttribute('aria-busy', 'true');
    status.textContent = english ? 'Please wait.' : '잠시만 기다려 주세요.';
    const currentController = new AbortController();
    controller = currentController;
    const timeout = window.setTimeout(() => currentController.abort(), 30000);
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: payload, credentials: 'same-origin',
        headers: {'X-Login-Request': '1'}, signal: currentController.signal,
      });
      if (sequence !== attempt) return;
      if (!response.ok) throw new Error('request_failed');
      if (response.headers.get('content-type')?.includes('application/json')) {
        const result = await response.json();
        if (typeof result.redirect_url !== 'string') throw new Error('invalid_redirect');
        const target = new URL(result.redirect_url, window.location.href);
        if (target.origin !== window.location.origin) throw new Error('invalid_redirect');
        window.location.assign(target.href);
        return;
      }
      // Server validation errors contain the entered values and new CSRF token.
      // Render the trusted same-origin page, including its retry handler.
      const html = await response.text();
      document.open();
      document.write(html);
      document.close();
    } catch (_) {
      if (sequence !== attempt) return;
      restore();
      status.textContent = english
        ? 'Unable to confirm the result. Please try again. If this continues, reload the page.'
        : '처리 결과를 확인하지 못했습니다. 다시 시도해 주세요. 문제가 계속되면 새로고침해 주세요.';
    } finally {
      window.clearTimeout(timeout);
    }
  });
})();
