/* Telegram widget callback. Signed data is verified again by the server. */
(function () {
  "use strict";
  let submitting = false;
  window.onTelegramAuth = async function (user) {
    const feedback = document.getElementById("login-feedback");
    if (submitting) return;
    submitting = true;
    if (feedback) feedback.textContent = "Проверяем Telegram-профиль…";
    try {
      const response = await fetch("/cabinet/auth/telegram", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(user)
      });
      if (!response.ok) {
        const errors = {
          401: "Подтверждение устарело или неверно. Обновите страницу и войдите снова.",
          403: "Telegram-аккаунт не привязан к активному партнёру.",
          503: "Вход временно недоступен. Попробуйте позже."
        };
        throw new Error(errors[response.status] || "Не удалось войти. Повторите попытку.");
      }
      window.location.assign("/cabinet/overview");
    } catch (error) {
      if (feedback) feedback.textContent = error.message || "Ошибка входа.";
      submitting = false;
    }
  };
}());
