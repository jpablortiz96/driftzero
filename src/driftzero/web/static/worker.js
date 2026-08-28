/* DRIFTZERO worker surface — T127 delta view, T128 evidence submission.
 *
 * Every fact rendered here comes from the API. This file computes no verdict, holds no
 * workflow state of its own, and has no path that can display success without the
 * server having said so. Where data is missing it says so rather than filling in
 * something plausible.
 *
 * The one value it does compute is the submission id: a SHA-256 of the chosen file's
 * bytes, so retrying the same photo carries the same id. It is a *claim*. The server
 * derives the authoritative submission identity from the bytes it receives, and would
 * ignore a client that lied.
 */
(function (global) {
  "use strict";

  var API = "/api/v1";

  // ---------------------------------------------------------------- helpers

  function $(id) {
    return document.getElementById(id);
  }

  function text(id, value) {
    var node = $(id);
    if (node) node.textContent = value;
  }

  function show(id, visible) {
    var node = $(id);
    if (node) node.classList.toggle("hidden", !visible);
  }

  function workflowId() {
    return new URLSearchParams(global.location.search).get("workflow") || "";
  }

  function link(page) {
    return "/web/" + page + "?workflow=" + encodeURIComponent(workflowId());
  }

  /* Status is set as a word, a mark and a class — in that order of importance. The word
   * is what a screen reader announces and what survives a greyscale screen. */
  function setStatus(kind, word, detail) {
    var node = $("status");
    if (!node) return;
    node.className = "status " + kind;
    var marks = {
      pass: "✔",
      fail: "✖",
      inconclusive: "⚠",
      working: "◌",
      waiting: "●"
    };
    text("status-mark", marks[kind] || "●");
    text("status-word", word);
    text("status-detail", detail || "");
  }

  function showError(title, detail) {
    text("error-title", title);
    text("error-detail", detail || "");
    show("error", true);
  }

  function clearError() {
    show("error", false);
  }

  /* Distinguishes a reachable server that refused from a network that never answered.
   * "Check your connection" is unhelpful advice when the server replied 404. */
  function request(url, options) {
    return fetch(url, options).then(
      function (response) {
        return response
          .json()
          .catch(function () {
            return null;
          })
          .then(function (body) {
            if (!response.ok) {
              var err = new Error("http");
              err.status = response.status;
              err.body = body;
              throw err;
            }
            return body;
          });
      },
      function () {
        var err = new Error("network");
        err.status = 0;
        throw err;
      }
    );
  }

  function describe(error) {
    if (error.status === 0) {
      return ["No connection", "Your device could not reach DRIFTZERO. Try again."];
    }
    if (error.status === 404) {
      return ["Not found", "This change is not available. Check the link you were sent."];
    }
    if (error.status === 409) {
      var detail = (error.body && error.body.detail && error.body.detail.detail) || "";
      return ["Not available right now", detail || "This change cannot be updated here."];
    }
    if (error.status === 413) {
      return ["Photo too large", "Try taking the photo again at a smaller size."];
    }
    if (error.status >= 500) {
      return ["DRIFTZERO is unavailable", "Something went wrong on our side. Try again."];
    }
    return ["That didn't work", "Please try again."];
  }

  function loadStatus() {
    var id = workflowId();
    if (!id) {
      return Promise.reject({ status: 404 });
    }
    return request(API + "/workflows/" + encodeURIComponent(id));
  }

  // ---------------------------------------------------------------- rendering

  /* The delta as the Truth Engine composed it. No second explanation is generated for
   * display, and a missing field is shown as missing. */
  function renderDelta(state) {
    text("change-id", state.change_id || "—");

    var delta = state.delta;
    if (!delta) {
      text("requirement", "Not available yet");
      text("before-value", "—");
      text("after-value", "—");
      text("artifact-context", "This update has not been delivered yet.");
      return false;
    }

    text("requirement", humanise(delta.requirement_id));
    text("before-value", delta.before_value);
    text("after-value", delta.after_value);
    text("artifact-context", "Applies to: " + delta.artifact_id);
    return true;
  }

  /* Renders an identifier for a person to read: "label_position" becomes
   * "Label position". The value is unchanged — this reverses no information and invents
   * none, it only stops a worker meeting a variable name. */
  function humanise(identifier) {
    if (!identifier) return "";
    var words = String(identifier).replace(/[_\-]+/g, " ").trim();
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  function renderSource(state) {
    // Deliberately neutral. The previous version said "Packing step updated", which
    // hardcoded this pilot's domain into product code — a second customer would have
    // been told their change was about packing.
    text("source-name", state.delta ? "Your work has changed" : "Process update");
    text(
      "source-version",
      state.delta
        ? humanise(state.delta.requirement_id) + " on " + state.delta.artifact_id
        : "Change " + (state.change_id || "—")
    );
  }

  /* Maps the server's verdict onto worker language. The mapping is total: every value
   * the server can send has an entry, so an unexpected one cannot silently look like a
   * pass. */
  function renderVerdict(state) {
    var verdict = state.latest_verification_status;
    var done = state.state === "PROOF_COMPLETE";

    if (done) {
      setStatus("pass", "Verified", "Change confirmed. Thank you.");
      show("go-proof", true);
      var proof = $("go-proof");
      if (proof) proof.href = link("proof");
      return "PASS";
    }
    if (verdict === "PASS") {
      setStatus("pass", "Verified", "Change confirmed.");
      return "PASS";
    }
    if (verdict === "FAIL") {
      setStatus(
        "fail",
        "Not done yet",
        "The photo shows the old position. Make the change, then take another photo."
      );
      return "FAIL";
    }
    if (verdict === "INCONCLUSIVE") {
      setStatus(
        "inconclusive",
        "Can't tell from that photo",
        "Take another photo with the label clearly visible."
      );
      return "INCONCLUSIVE";
    }
    setStatus(
      "waiting",
      "Waiting for verification",
      "Make the change, then take a photo to confirm it."
    );
    return null;
  }

  // ---------------------------------------------------------------- T127

  function initDelta() {
    var verify = $("go-verify");
    if (verify) verify.href = link("verify");

    loadStatus()
      .then(function (state) {
        clearError();
        renderSource(state);
        var hasDelta = renderDelta(state);
        renderVerdict(state);
        if (!hasDelta && verify) {
          verify.setAttribute("aria-disabled", "true");
          verify.classList.add("secondary");
        }
      })
      .catch(function (error) {
        var parts = describe(error);
        showError(parts[0], parts[1]);
        text("source-name", "Update unavailable");
        text("source-version", "");
        setStatus("waiting", "Not loaded", parts[1]);
      });
  }

  // ---------------------------------------------------------------- T128

  /* Stable across retries of the same photo, because it is derived from the bytes.
   * Falls back to a size+time value where SubtleCrypto is unavailable (an http origin,
   * for instance) — the server derives its own identity regardless. */
  function submissionId(file) {
    if (!global.crypto || !global.crypto.subtle) {
      return Promise.resolve("client-" + file.size + "-" + file.lastModified);
    }
    return file
      .arrayBuffer()
      .then(function (buffer) {
        return global.crypto.subtle.digest("SHA-256", buffer);
      })
      .then(function (digest) {
        return Array.prototype.map
          .call(new Uint8Array(digest), function (b) {
            return b.toString(16).padStart(2, "0");
          })
          .join("");
      })
      .catch(function () {
        return "client-" + file.size + "-" + file.lastModified;
      });
  }

  function initVerify() {
    var input = $("photo");
    var submit = $("submit");
    var back = $("back");
    if (back) back.href = link("delta");

    loadStatus()
      .then(function (state) {
        clearError();
        text("change-id", state.change_id || "—");
        if (state.delta) {
          text("requirement", humanise(state.delta.requirement_id));
          text("after-value", state.delta.after_value);
          text("reminder", "Take a photo showing the label in its new position.");
        }
        renderVerdict(state);
      })
      .catch(function (error) {
        var parts = describe(error);
        showError(parts[0], parts[1]);
      });

    if (!input || !submit) return;

    input.addEventListener("change", function () {
      clearError();
      var file = input.files && input.files[0];
      if (!file) {
        submit.disabled = true;
        show("preview", false);
        return;
      }
      if (!/^image\//.test(file.type) && file.type !== "") {
        // The browser's type is a hint only — the server decides from the bytes. This
        // check exists to spare the worker a pointless upload, not to establish a fact.
        showError("That isn't a photo", "Choose an image taken with a camera.");
        submit.disabled = true;
        show("preview", false);
        return;
      }

      var preview = $("preview");
      var image = $("preview-image");
      if (preview && image) {
        image.src = URL.createObjectURL(file);
        preview.style.display = "block";
      }
      text("photo-label", "Choose a different photo");
      submit.disabled = false;
      setStatus("waiting", "Ready to send", file.name || "Photo selected.");
    });

    submit.addEventListener("click", function () {
      var file = input.files && input.files[0];
      if (!file) return;

      submit.disabled = true;
      clearError();
      setStatus("working", "Checking your photo", "Analysing the change evidence…");

      submissionId(file)
        .then(function (id) {
          var form = new FormData();
          form.append("file", file, file.name || "evidence.jpg");
          form.append("submission_id", id);
          return request(
            API + "/workflows/" + encodeURIComponent(workflowId()) + "/verify",
            { method: "POST", body: form }
          );
        })
        .then(function () {
          // The response is not trusted as the final word: the workflow is re-read so
          // what the worker sees is the server's state, not this request's echo.
          return loadStatus();
        })
        .then(function (state) {
          var verdict = renderVerdict(state);
          if (verdict === "FAIL" || verdict === "INCONCLUSIVE") {
            text("photo-label", "Take another photo");
            input.value = "";
            show("preview", false);
            submit.disabled = true;
          } else if (verdict === "PASS") {
            show("preview", false);
            submit.disabled = true;
            text("photo-label", "Photo accepted");
          } else {
            submit.disabled = false;
          }
        })
        .catch(function (error) {
          var parts = describe(error);
          showError(parts[0], parts[1]);
          setStatus("waiting", "Not sent", parts[1]);
          submit.disabled = false;
        });
    });
  }

  global.DriftZero = {
    initDelta: initDelta,
    initVerify: initVerify,
    // Exported for the automated tests.
    _describe: describe,
    _humanise: humanise,
    _submissionId: submissionId
  };
})(window);
