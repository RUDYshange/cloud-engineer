/* Strongbox web client.
 *
 * Talks to Cognito directly over its JSON API so the page needs no build
 * step and no SDK bundle - it is three static files on S3.
 * The id token is held in sessionStorage and sent as a bearer token;
 * API Gateway verifies it before any Lambda runs.
 */

(function () {
  "use strict";

  var cfg = window.STRONGBOX_CONFIG;
  var IDP = "https://cognito-idp." + cfg.region + ".amazonaws.com/";

  var state = {
    mode: "signIn", // signIn | signUp | confirm
    token: sessionStorage.getItem("sb.token") || null,
    email: sessionStorage.getItem("sb.email") || "",
    files: []
  };

  var $ = function (id) { return document.getElementById(id); };

  // ------------------------------------------------------------------
  // Cognito
  // ------------------------------------------------------------------
  function idp(action, payload) {
    return fetch(IDP, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": "AWSCognitoIdentityProviderService." + action
      },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().then(function (body) {
        if (!res.ok) throw new Error(readable(body));
        return body;
      });
    });
  }

  function readable(body) {
    var msg = body && body.message ? body.message : "That did not work.";
    var type = (body && body.__type) || "";
    if (type.indexOf("NotAuthorized") > -1) return "Email or password is wrong.";
    if (type.indexOf("UserNotConfirmed") > -1) return "Confirm your email first. Enter the code we sent you.";
    if (type.indexOf("UsernameExists") > -1) return "There is already an account on that email.";
    if (type.indexOf("CodeMismatch") > -1) return "That code does not match. Check it and try again.";
    if (type.indexOf("ExpiredCode") > -1) return "That code has expired. Sign up again to get a new one.";
    if (type.indexOf("InvalidPassword") > -1) return "Password needs 10 characters, an uppercase letter and a number.";
    return msg;
  }

  function signIn(email, password) {
    return idp("InitiateAuth", {
      AuthFlow: "USER_PASSWORD_AUTH",
      ClientId: cfg.userPoolClientId,
      AuthParameters: { USERNAME: email, PASSWORD: password }
    }).then(function (body) {
      var token = body.AuthenticationResult && body.AuthenticationResult.IdToken;
      if (!token) throw new Error("Cognito did not return a token.");
      state.token = token;
      state.email = email;
      sessionStorage.setItem("sb.token", token);
      sessionStorage.setItem("sb.email", email);
    });
  }

  // ------------------------------------------------------------------
  // API
  // ------------------------------------------------------------------
  function callApi(path, options) {
    options = options || {};
    var headers = { Authorization: "Bearer " + state.token };
    if (options.body) headers["Content-Type"] = "application/json";

    return fetch(cfg.apiBaseUrl + path, {
      method: options.method || "GET",
      headers: headers,
      body: options.body ? JSON.stringify(options.body) : undefined
    }).then(function (res) {
      if (res.status === 401 || res.status === 403) {
        signOut();
        throw new Error("Your session expired. Sign in again.");
      }
      return res.json().then(function (body) {
        if (!res.ok) throw new Error(body.error || "Request failed.");
        return body;
      });
    });
  }

  function refresh() {
    return callApi("/files").then(function (data) {
      state.files = data.files || [];
      renderUsage(data.usage);
      renderFiles();
    });
  }

  // ------------------------------------------------------------------
  // Upload: ask for a presigned URL, then PUT straight to S3
  // ------------------------------------------------------------------
  function upload(file) {
    var row = pendingRow(file.name);

    return callApi("/files/upload-url", {
      method: "POST",
      body: { name: file.name, sizeBytes: file.size, contentType: file.type || "application/octet-stream" }
    }).then(function (grant) {
      return put(grant.uploadUrl, file, function (pct) {
        row.progress.style.width = pct + "%";
      });
    }).then(function () {
      row.el.remove();
      // The S3 event handler flips the row to AVAILABLE; give it a beat.
      return new Promise(function (r) { setTimeout(r, 900); }).then(refresh);
    }).catch(function (err) {
      row.el.remove();
      showError($("vault-error"), err.message);
      return refresh();
    });
  }

  function put(url, file, onProgress) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("PUT", url);
      xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      };
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 300) resolve();
        else reject(new Error("Storage rejected the upload (" + xhr.status + ")."));
      };
      xhr.onerror = function () { reject(new Error("Network dropped during upload.")); };
      xhr.send(file);
    });
  }

  // ------------------------------------------------------------------
  // Rendering
  // ------------------------------------------------------------------
  function bytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    var units = ["KB", "MB", "GB"], i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return (n < 10 ? n.toFixed(1) : Math.round(n)) + " " + units[i];
  }

  function when(ts) {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleString(undefined, {
      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"
    });
  }

  function renderUsage(usage) {
    usage = usage || { usedBytes: 0, quotaBytes: 0 };
    $("used-figure").textContent = bytes(usage.usedBytes);
    $("quota-figure").textContent = bytes(usage.quotaBytes);
    var pct = usage.quotaBytes ? Math.min(100, (usage.usedBytes / usage.quotaBytes) * 100) : 0;
    $("meter-fill").style.width = pct + "%";
    $("meter").setAttribute("aria-valuenow", Math.round(pct));
    $("who").textContent = "Signed in as " + state.email;
  }

  function renderFiles() {
    var list = $("file-list");
    list.innerHTML = "";
    $("empty").hidden = state.files.length > 0;

    state.files.forEach(function (f) {
      var li = document.createElement("li");
      li.className = "row";

      var name = document.createElement("div");
      name.className = "row-name";
      name.textContent = f.name;

      var meta = document.createElement("div");
      meta.className = "row-meta";
      if (f.status === "AVAILABLE") {
        meta.textContent = bytes(f.sizeBytes) + " · stored " + when(f.uploadedAt || f.createdAt);
      } else {
        meta.innerHTML = '<span class="state-pending">Waiting for the upload to finish</span>';
      }

      var actions = document.createElement("div");
      actions.className = "row-actions";

      if (f.status === "AVAILABLE") {
        var dl = document.createElement("button");
        dl.className = "linkish";
        dl.type = "button";
        dl.textContent = "Download";
        dl.onclick = function () {
          dl.disabled = true;
          callApi("/files/" + f.fileId + "/download-url")
            .then(function (r) { window.location.href = r.downloadUrl; })
            .catch(function (e) { showError($("vault-error"), e.message); })
            .finally(function () { dl.disabled = false; });
        };
        actions.appendChild(dl);
      }

      var rm = document.createElement("button");
      rm.className = "linkish destructive";
      rm.type = "button";
      rm.textContent = "Delete";
      rm.onclick = function () {
        if (!window.confirm("Delete " + f.name + "? This cannot be undone.")) return;
        rm.disabled = true;
        callApi("/files/" + f.fileId, { method: "DELETE" })
          .then(refresh)
          .catch(function (e) { showError($("vault-error"), e.message); rm.disabled = false; });
      };
      actions.appendChild(rm);

      li.appendChild(name);
      li.appendChild(meta);
      li.appendChild(actions);
      list.appendChild(li);
    });
  }

  function pendingRow(filename) {
    var li = document.createElement("li");
    li.className = "row";
    li.innerHTML =
      '<div class="row-name"></div>' +
      '<div class="row-meta"><span class="state-pending">Uploading</span></div>' +
      '<div class="bar"><span style="width:0"></span></div>';
    li.querySelector(".row-name").textContent = filename;
    $("file-list").prepend(li);
    $("empty").hidden = true;
    return { el: li, progress: li.querySelector(".bar span") };
  }

  function showError(el, message) {
    el.textContent = message;
    el.hidden = false;
  }

  function clearError(el) {
    el.hidden = true;
    el.textContent = "";
  }

  // ------------------------------------------------------------------
  // Screens
  // ------------------------------------------------------------------
  function show() {
    var signedIn = Boolean(state.token);
    $("gate").hidden = signedIn;
    $("vault").hidden = !signedIn;
    if (signedIn) {
      refresh().catch(function (e) { showError($("vault-error"), e.message); });
    }
  }

  function setMode(mode) {
    state.mode = mode;
    clearError($("gate-error"));
    var titles = { signIn: "Sign in", signUp: "Create an account", confirm: "Confirm your email" };
    $("gate-title").textContent = titles[mode];
    $("gate-submit").textContent = mode === "confirm" ? "Confirm and continue" : titles[mode];
    $("code-field").hidden = mode !== "confirm";
    $("password").parentElement.hidden = mode === "confirm";
    $("email").parentElement.hidden = mode === "confirm";
    $("pw-hint").hidden = mode !== "signUp";
    $("switch-text").textContent = mode === "signIn" ? "No account yet?" : "Already registered?";
    $("switch-mode").textContent = mode === "signIn" ? "Create one" : "Sign in";
    $("switch-mode").parentElement.hidden = mode === "confirm";
  }

  function signOut() {
    state.token = null;
    state.files = [];
    sessionStorage.removeItem("sb.token");
    show();
    setMode("signIn");
  }

  // ------------------------------------------------------------------
  // Wiring
  // ------------------------------------------------------------------
  $("switch-mode").onclick = function () {
    setMode(state.mode === "signIn" ? "signUp" : "signIn");
  };

  $("gate-submit").onclick = function () {
    var btn = $("gate-submit");
    var email = $("email").value.trim();
    var password = $("password").value;
    clearError($("gate-error"));
    btn.disabled = true;

    var work;
    if (state.mode === "signIn") {
      work = signIn(email, password).then(show);
    } else if (state.mode === "signUp") {
      work = idp("SignUp", {
        ClientId: cfg.userPoolClientId,
        Username: email,
        Password: password,
        UserAttributes: [{ Name: "email", Value: email }]
      }).then(function () {
        state.email = email;
        state.pendingPassword = password;
        setMode("confirm");
      });
    } else {
      work = idp("ConfirmSignUp", {
        ClientId: cfg.userPoolClientId,
        Username: state.email,
        ConfirmationCode: $("code").value.trim()
      }).then(function () {
        return signIn(state.email, state.pendingPassword).then(show);
      });
    }

    work.catch(function (err) { showError($("gate-error"), err.message); })
        .finally(function () { btn.disabled = false; });
  };

  ["email", "password", "code"].forEach(function (id) {
    $(id).addEventListener("keydown", function (e) {
      if (e.key === "Enter") $("gate-submit").click();
    });
  });

  $("sign-out").onclick = signOut;
  $("browse").onclick = function () { $("file-input").click(); };

  $("file-input").onchange = function (e) {
    clearError($("vault-error"));
    Array.prototype.forEach.call(e.target.files, upload);
    e.target.value = "";
  };

  var dz = $("dropzone");
  ["dragenter", "dragover"].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add("hot"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove("hot"); });
  });
  dz.addEventListener("drop", function (e) {
    clearError($("vault-error"));
    Array.prototype.forEach.call(e.dataTransfer.files, upload);
  });

  setMode("signIn");
  show();
})();
