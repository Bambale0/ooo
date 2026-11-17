"use strict";
window.NeironychTelegramAuth = async function (user) {
  const error = document.getElementById("login-error");
  if (error) {
    error.hidden = true;
    error.textContent = "";
  }
  try {
    const csrf = document.querySelector('meta[name="tg-login-csrf"]')?.content;
    if (!csrf) throw new Error("missing_login_challenge");
    const response = await fetch("/cabinet/telegram", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...user, csrf })
    });
    if (!response.ok) throw new Error("login_rejected");
    window.location.assign("/cabinet");
  } catch (_error) {
    if (error) {
      error.textContent = "Не удалось войти. Убедитесь, что аккаунт партнёра активен, и повторите попытку.";
      error.hidden = false;
    }
  }
};
