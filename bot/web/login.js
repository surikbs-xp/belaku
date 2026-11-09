// Login page behaviour. Kept separate from app.js so the dashboard bundle is
// never loaded on an unauthenticated page.
(function () {
  "use strict";

  const form = document.getElementById("login-form");
  const input = document.getElementById("password");
  const submit = document.getElementById("login-submit");
  const error = document.getElementById("login-error");

  function show(message) {
    error.textContent = message;
    error.hidden = false;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    submit.disabled = true;
    submit.textContent = "Signing in…";

    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: input.value }),
      });

      if (response.ok) {
        window.location.replace("/");
        return;
      }

      let detail = "Sign in failed";
      try {
        const body = await response.json();
        if (body && body.detail) detail = body.detail;
      } catch (ignored) {
        // Non-JSON error body; the status text is all we have.
      }
      show(detail);
      input.value = "";
      input.focus();
    } catch (err) {
      show("Could not reach the bot. Is it running?");
    } finally {
      submit.disabled = false;
      submit.textContent = "Sign in";
    }
  });
})();
