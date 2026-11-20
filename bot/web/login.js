// Login page behaviour. Kept separate from app.js so the dashboard bundle is
// never loaded on an unauthenticated page.
(function () {
  "use strict";

  const form = document.getElementById("login-form");
  const input = document.getElementById("password");
  const submit = document.getElementById("login-submit");
  const submitLabel = document.getElementById("login-submit-label");
  const error = document.getElementById("login-error");
  const reveal = document.getElementById("login-reveal");
  const status = document.getElementById("login-status");
  const statusText = document.getElementById("login-status-text");

  function show(message) {
    error.textContent = message;
    error.hidden = false;
  }

  function clearError() {
    error.hidden = true;
    error.textContent = "";
  }

  // -- reveal ---------------------------------------------------------------
  // A long password typed blind is the most common reason a correct one is
  // rejected. The toggle is a button, not a checkbox, so it cannot be submitted
  // and does not appear in the form's data.
  reveal.addEventListener("click", () => {
    const shown = input.type === "text";
    input.type = shown ? "password" : "text";
    reveal.setAttribute("aria-pressed", shown ? "false" : "true");
    reveal.setAttribute("aria-label", shown ? "Show password" : "Hide password");
    input.focus();
    // Keep the caret at the end rather than selecting all, so typing continues.
    const end = input.value.length;
    try {
      input.setSelectionRange(end, end);
    } catch (ignored) {
      // Some browsers refuse selection on a password field; harmless.
    }
  });

  // -- live status ----------------------------------------------------------
  // /api/health is unauthenticated and carries only uptime, so this tells the
  // operator whether the bot is up before they type a password - without
  // exposing a position, a P&L or anything else that needs a session.
  function formatUptime(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m`;
    return `${s}s`;
  }

  async function refreshStatus() {
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      const body = await response.json();
      status.dataset.state = "up";
      statusText.textContent = `Bot running · up ${formatUptime(body.uptime_seconds || 0)}`;
    } catch (err) {
      status.dataset.state = "down";
      statusText.textContent = "Bot unreachable — is the container running?";
    }
  }

  refreshStatus();
  // Re-check while the page sits open. A login page left on a wall display
  // should not claim the bot is up an hour after it died.
  setInterval(refreshStatus, 15000);

  // -- submit ---------------------------------------------------------------
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError();

    if (!input.value) {
      show("Enter the dashboard password.");
      input.focus();
      return;
    }

    submit.disabled = true;
    submitLabel.textContent = "Signing in…";

    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: input.value }),
      });

      if (response.ok) {
        // replace, not assign: the login page must not sit in history where a
        // back button would return to it after a successful sign-in.
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
      // 429 is the rate limiter, and its message already says to wait.
      show(detail);
      input.value = "";
      input.focus();
    } catch (err) {
      show("Could not reach the bot. Is it running?");
    } finally {
      submit.disabled = false;
      submitLabel.textContent = "Sign in";
    }
  });

  // Typing again clears a stale error, so the message does not outlive the
  // attempt that produced it.
  input.addEventListener("input", () => {
    if (!error.hidden) clearError();
  });
})();
