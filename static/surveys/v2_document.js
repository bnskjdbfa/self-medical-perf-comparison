(() => {
  const deck = document.querySelector('[data-v2-document-deck]');
  const form = document.querySelector('[data-v2-document-form]');
  if (!deck || !form) return;

  const panels = Array.from(deck.querySelectorAll('[data-document-panel]'));
  let activeIndex = deck.dataset.hasErrors === 'true' ? panels.length - 1 : 0;
  const documentHistoryKey = 'v2-required-document';
  const documentUrl = window.location.href;
  let transitionPending = false;
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const showRequestError = (panel, message) => {
    const error = panel.querySelector('[data-document-request-error]');
    if (!error) return;
    error.textContent = message || '요청을 처리하지 못했습니다. 다시 시도해 주세요.';
    error.hidden = false;
  };

  const clearRequestError = (panel) => {
    const error = panel.querySelector('[data-document-request-error]');
    if (!error) return;
    error.textContent = '';
    error.hidden = true;
  };

  const showPanel = (nextIndex, direction = 1) => {
    const current = panels[activeIndex];
    const next = panels[nextIndex];
    if (!next || next === current || transitionPending) return;

    transitionPending = true;
    current.classList.add('is-leaving');
    window.setTimeout(() => {
      current.hidden = true;
      current.classList.remove('is-active', 'is-ready', 'is-leaving');
      next.hidden = false;
      next.style.setProperty('--v2-document-enter-x', `${direction * 10}px`);
      requestAnimationFrame(() => {
        next.classList.add('is-active');
        requestAnimationFrame(() => next.classList.add('is-ready'));
      });
      activeIndex = nextIndex;
      transitionPending = false;
      window.scrollTo({ top: 0, behavior: reduceMotion ? 'auto' : 'smooth' });
    }, reduceMotion ? 0 : 180);
  };

  panels.forEach((panel, index) => {
    const nextButton = panel.querySelector('[data-document-next]');
    if (nextButton) {
      nextButton.addEventListener('click', async () => {
        if (transitionPending) return;
        if (panel.dataset.consentRecorded !== 'true') {
          const recorded = await submitDocumentConsent(panel, nextButton, false);
          if (!recorded) return;
          panel.dataset.consentRecorded = 'true';
        }
        const nextIndex = index + 1;
        window.history.pushState(
          { v2Flow: documentHistoryKey, documentIndex: nextIndex },
          '',
          documentUrl,
        );
        showPanel(nextIndex);
      });
    }
  });

  const initial = panels[activeIndex];
  panels.forEach((panel, index) => {
    panel.hidden = index !== activeIndex;
    panel.classList.toggle('is-active', index === activeIndex);
  });
  requestAnimationFrame(() => initial?.classList.add('is-ready'));

  window.history.replaceState(
    { ...(window.history.state || {}), v2Flow: documentHistoryKey, documentIndex: activeIndex },
    '',
    documentUrl,
  );

  window.addEventListener('popstate', (event) => {
    const state = event.state;
    if (state?.v2Flow === documentHistoryKey) {
      const nextIndex = Number(state.documentIndex);
      if (Number.isInteger(nextIndex) && nextIndex >= 0 && nextIndex < panels.length) {
        showPanel(nextIndex, nextIndex < activeIndex ? -1 : 1);
        return;
      }
    }

    const fallbackIndex = Math.max(0, activeIndex - 1);
    window.history.pushState(
      { v2Flow: documentHistoryKey, documentIndex: fallbackIndex },
      '',
      documentUrl,
    );
    showPanel(fallbackIndex, -1);
  });

  const submitDocumentConsent = async (panel, button, finalize) => {
    if (form.dataset.submitting === 'true') return false;
    clearRequestError(panel);
    const agreement = panel.querySelector('[data-document-agreement]');
    if (!agreement?.checked) {
      showRequestError(
        panel,
        document.documentElement.lang?.startsWith('en')
          ? 'Please check the required agreement.'
          : '필수 동의 또는 서약 항목을 확인해 주세요.',
      );
      agreement?.focus();
      return false;
    }
    form.dataset.submitting = 'true';
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');

    const payload = new FormData(form);
    payload.set('action', finalize ? 'finalize' : 'record_consent');
    payload.set('document_type', button.dataset.documentConsent || '');
    payload.set('agreed', 'on');

    try {
      const response = await window.fetch(documentUrl, {
        method: 'POST',
        body: payload,
        credentials: 'same-origin',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      let result;
      try {
        result = await response.json();
      } catch (error) {
        result = null;
      }
      if (!response.ok || !result?.ok) {
        showRequestError(panel, result?.error);
        return false;
      }
      if (finalize && result.redirect_url) {
        window.sessionStorage.removeItem('v2RequiredDocumentUiState');
        window.location.assign(result.redirect_url);
      }
      return true;
    } catch (error) {
      showRequestError(panel, '네트워크 오류가 발생했습니다. 현재 화면에서 다시 시도해 주세요.');
      return false;
    } finally {
      form.dataset.submitting = 'false';
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const panel = panels[activeIndex];
    const button = panel?.querySelector('[data-document-finalize]');
    if (!panel || !button) return;
    await submitDocumentConsent(panel, button, true);
  });

  window.addEventListener('pageshow', () => {
    form.dataset.submitting = 'false';
    const button = form.querySelector('button[type="submit"]');
    if (button) {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  });
})();
