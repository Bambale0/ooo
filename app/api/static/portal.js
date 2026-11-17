"use strict";
// Progressive enhancement only. Payments/keys always submit to authenticated BFF.
(() => {
  const sidebar = document.getElementById("portal-sidebar");
  const toggle = document.getElementById("menu-toggle");
  const backdrop = document.getElementById("menu-backdrop");
  const setMenu = (open) => {
    if (!sidebar || !toggle || !backdrop) return;
    sidebar.classList.toggle("open", open);
    backdrop.classList.toggle("open", open);
    backdrop.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
  };
  toggle?.addEventListener("click", () => setMenu(!sidebar?.classList.contains("open")));
  backdrop?.addEventListener("click", () => setMenu(false));
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setMenu(false);
  });
  sidebar?.querySelectorAll("nav a").forEach((link) => {
    link.addEventListener("click", () => setMenu(false));
  });
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm || "Подтвердить действие?")) {
        event.preventDefault();
      }
    });
  });
  document.querySelectorAll("form[action='/cabinet/action']").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type=submit]");
      if (button) {
        button.disabled = true;
        button.textContent = "Обрабатывается…";
      }
    });
  });
  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const text = document.getElementById(button.dataset.copyTarget || "")?.textContent;
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Скопировано ✓";
      } catch (_) {
        button.textContent = "Выделите и скопируйте вручную";
      }
    });
  });
})();
