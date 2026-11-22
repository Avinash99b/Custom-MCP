"use strict";

// Login page script. Uses DOM APIs only (no inline script) to satisfy CSP.
(function () {
  const form = document.getElementById("login-form");
  const errorEl = document.getElementById("error");
  const secretEl = document.getElementById("secret");

  if (!form) {
    return;
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    errorEl.textContent = "";

    let response;
    try {
      response = await fetch("/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ secret: secretEl.value }),
      });
    } catch (err) {
      errorEl.textContent = "Network error.";
      return;
    } finally {
      secretEl.value = "";
    }

    if (response.ok) {
      window.location.replace("/");
      return;
    }

    let message = "Login failed.";
    try {
      const data = await response.json();
      if (data && data.error && data.error.message) {
        message = data.error.message;
      }
    } catch (err) {
      // keep default message
    }
    errorEl.textContent = message;
  });
})();
