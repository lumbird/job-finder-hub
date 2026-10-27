// Job Finder Hub client helpers. No inline scripts (strict CSP), no build step.
(() => {
  "use strict";

  const csrf = () => document.querySelector('meta[name="csrf-token"]').content;

  // Forms marked data-autosubmit (the theme toggle) submit when a choice changes.
  document.addEventListener("change", (e) => {
    const form = e.target.closest("form[data-autosubmit]");
    if (form) form.requestSubmit();
  });

  // Forms marked data-busy show a spinner on their button while the server works.
  document.addEventListener("submit", (e) => {
    const form = e.target.closest("form[data-busy]");
    if (!form) return;
    const btn = e.submitter || form.querySelector("button[type=submit]");
    if (btn) btn.setAttribute("aria-busy", "true");
  });

  // ---------- copy buttons ----------

  let toastTimer;
  const toast = (msg) => {
    let el = document.querySelector(".toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast";
      el.setAttribute("role", "status");
      document.body.append(el);
    }
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.hidden = true), 1800);
  };

  // The Clipboard API needs HTTPS; over plain HTTP on the home network fall back to execCommand.
  const copyText = async (text, source) => {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    const field = source && "select" in source ? source : Object.assign(document.createElement("textarea"), { value: text });
    if (field !== source) {
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.append(field);
    }
    field.select();
    field.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy");
    if (field !== source) field.remove();
    window.getSelection()?.removeAllRanges();
    return ok;
  };

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    const source = document.querySelector(btn.dataset.copy);
    if (!source) return;
    const text = "value" in source ? source.value : source.textContent;
    try {
      toast((await copyText(text, source)) ? "Copied" : "Couldn't copy. Select the text and copy it.");
    } catch {
      toast("Couldn't copy. Select the text and copy it.");
    }
  });

  // ---------- passkeys ----------

  const b64urlToBuf = (s) => {
    const b64 = s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "=");
    return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)).buffer;
  };
  const bufToB64url = (buf) =>
    btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

  const postJSON = async (url, body) => {
    const res = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify(body ?? {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Something went wrong. Try again.");
    return data;
  };

  const showError = (msg) => {
    const el = document.getElementById("passkey-error");
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
  };

  const friendly = (err) =>
    err && err.name === "NotAllowedError"
      ? "Passkey cancelled or timed out."
      : err && err.name === "InvalidStateError"
        ? "This device already has a passkey for the hub."
        : (err && err.message) || "Passkey didn't work.";

  const supported = () => window.PublicKeyCredential && navigator.credentials;

  const loginBtn = document.getElementById("passkey-login");
  if (loginBtn) {
    if (!supported()) loginBtn.hidden = true;
    loginBtn.addEventListener("click", async () => {
      loginBtn.setAttribute("aria-busy", "true");
      try {
        const opts = await postJSON("/webauthn/login/options");
        opts.challenge = b64urlToBuf(opts.challenge);
        (opts.allowCredentials || []).forEach((c) => (c.id = b64urlToBuf(c.id)));
        const cred = await navigator.credentials.get({ publicKey: opts });
        const r = cred.response;
        const result = await postJSON("/webauthn/login/verify", {
          next: loginBtn.dataset.next || "",
          credential: {
            id: cred.id,
            rawId: bufToB64url(cred.rawId),
            type: cred.type,
            clientExtensionResults: cred.getClientExtensionResults(),
            authenticatorAttachment: cred.authenticatorAttachment,
            response: {
              clientDataJSON: bufToB64url(r.clientDataJSON),
              authenticatorData: bufToB64url(r.authenticatorData),
              signature: bufToB64url(r.signature),
              userHandle: r.userHandle ? bufToB64url(r.userHandle) : null,
            },
          },
        });
        window.location.assign(result.redirect || "/");
      } catch (err) {
        showError(friendly(err));
      } finally {
        loginBtn.removeAttribute("aria-busy");
      }
    });
  }

  const regForm = document.getElementById("passkey-register");
  if (regForm) {
    if (!supported()) regForm.hidden = true;
    regForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = regForm.querySelector("button");
      btn.setAttribute("aria-busy", "true");
      try {
        const opts = await postJSON("/webauthn/register/options");
        opts.challenge = b64urlToBuf(opts.challenge);
        opts.user.id = b64urlToBuf(opts.user.id);
        (opts.excludeCredentials || []).forEach((c) => (c.id = b64urlToBuf(c.id)));
        const cred = await navigator.credentials.create({ publicKey: opts });
        const r = cred.response;
        const result = await postJSON("/webauthn/register/verify", {
          name: regForm.elements.name.value,
          credential: {
            id: cred.id,
            rawId: bufToB64url(cred.rawId),
            type: cred.type,
            clientExtensionResults: cred.getClientExtensionResults(),
            authenticatorAttachment: cred.authenticatorAttachment,
            response: {
              clientDataJSON: bufToB64url(r.clientDataJSON),
              attestationObject: bufToB64url(r.attestationObject),
              transports: r.getTransports ? r.getTransports() : [],
            },
          },
        });
        window.location.assign(result.redirect);
      } catch (err) {
        showError(friendly(err));
      } finally {
        btn.removeAttribute("aria-busy");
      }
    });
  }
})();
