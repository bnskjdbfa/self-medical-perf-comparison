(() => {
  const appSelector = "[data-manager-app]";

  function app() {
    return document.querySelector(appSelector);
  }

  async function renderUrl(url, { replace = false } = {}) {
    const currentApp = app();
    if (!currentApp) return;
    currentApp.classList.add("is-loading");

    try {
      const response = await fetch(url, {
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      });
      if (!response.ok) {
        window.location.assign(url);
        return;
      }
      const html = await response.text();
      const nextDocument = new DOMParser().parseFromString(html, "text/html");
      const nextApp = nextDocument.querySelector(appSelector);
      if (!nextApp) {
        window.location.assign(url);
        return;
      }
      currentApp.replaceWith(nextApp);
      document.title = nextDocument.title;
      if (replace) {
        history.replaceState({ managerLookup: true }, "", url);
      } else {
        history.pushState({ managerLookup: true }, "", url);
      }
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (_error) {
      window.location.assign(url);
    }
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-manager-nav]");
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    renderUrl(new URL(link.href, window.location.href).toString());
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-manager-form]");
    if (!form) return;
    event.preventDefault();
    const url = new URL(form.action, window.location.href);
    url.search = new URLSearchParams(new FormData(form)).toString();
    renderUrl(url.toString());
  });

  window.addEventListener("popstate", () => {
    renderUrl(window.location.href, { replace: true });
  });
})();
