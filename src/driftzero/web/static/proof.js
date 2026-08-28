/* DRIFTZERO workflow and Change Proof display — T130.
 *
 * Rendered from the canonical JSON the API returns, which stays the source of truth:
 * this file reformats it for reading and never recomputes, reorders or supplements it.
 *
 * "Verify integrity" is a real check, not a badge. It re-canonicalises the proof in the
 * browser exactly as the Truth Engine does — sorted keys, no whitespace, the
 * content_hash field excluded — hashes it, and compares. A mismatch is reported as a
 * mismatch.
 */
(function (global) {
  "use strict";

  var API = "/api/v1";

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

  function setStatus(prefix, kind, word, detail) {
    var node = $(prefix);
    if (!node) return;
    node.className = "status " + kind;
    node.classList.remove("hidden");
    var marks = { pass: "✔", fail: "✖", inconclusive: "⚠", waiting: "●", working: "◌" };
    text(prefix + "-mark", marks[kind] || "●");
    text(prefix + "-word", word);
    text(prefix + "-detail", detail || "");
  }

  function showError(title, detail) {
    text("error-title", title);
    text("error-detail", detail || "");
    show("error", true);
  }

  function request(url) {
    return fetch(url).then(
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
      return ["No connection", "Could not reach DRIFTZERO. Try again."];
    }
    if (error.status === 404) {
      var body = error.body || {};
      var code = (body.detail && body.detail.error) || "";
      if (code === "PROOF_NOT_COMPLETE") {
        return [
          "No proof yet",
          "A Change Proof exists only once all seven completion conditions hold."
        ];
      }
      return ["Not found", "No such change."];
    }
    return ["Unavailable", "Please try again."];
  }

  function fact(list, label, value, mono) {
    if (value === null || value === undefined || value === "") return;
    var dt = document.createElement("dt");
    dt.textContent = label;
    var dd = document.createElement("dd");
    dd.textContent = value;
    if (mono) dd.className = "mono";
    var row = document.createElement("div");
    row.className = "fact";
    row.appendChild(dt);
    row.appendChild(dd);
    list.appendChild(row);
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  // ---------------------------------------------------------------- workflow view

  function initWorkflow() {
    var proofLink = $("go-proof");
    var deltaLink = $("go-delta");
    if (deltaLink) deltaLink.href = link("delta");
    if (proofLink) proofLink.href = link("proof");

    Promise.all([
      request(API + "/workflows/" + encodeURIComponent(workflowId())),
      fetch("/ready")
        .then(function (r) {
          return r.ok ? r.json() : null;
        })
        .catch(function () {
          return null;
        })
    ])
      .then(function (results) {
        var state = results[0];
        var ready = results[1];

        // Runtime honesty: whatever the deployment actually reports, never "production".
        if (ready && ready.runtime_mode) {
          text("runtime-mode", ready.runtime_mode);
        }

        var deployed = state.state === "PROOF_COMPLETE";
        text("headline", deployed ? "Change deployed" : "Change in progress");
        text("subhead", "Change " + (state.change_id || "—"));

        if (deployed) {
          setStatus("status", "pass", "Deployed and verified",
            "The work was changed and physically confirmed.");
          show("go-proof", true);
        } else {
          setStatus("status", "waiting", state.state,
            "This change has not completed yet.");
          show("go-proof", false);
        }

        var facts = $("facts");
        clear(facts);
        fact(facts, "Change", state.change_id);
        fact(facts, "Workflow", state.workflow_id, true);
        fact(facts, "State", state.state);
        fact(facts, "Affected work", state.affected_artifact_id);
        if (state.delta) {
          fact(facts, "Requirement", state.delta.requirement_id);
          fact(facts, "Change", state.delta.before_value + " → " + state.delta.after_value);
        }
        fact(facts, "Delivery", state.delivery_status || "Not delivered");
        fact(facts, "Latest verification", state.latest_verification_status || "None yet");
        fact(facts, "Proof", state.proof_id, true);
        fact(facts, "State read from", state.source);
        fact(facts, "Survives restart", state.durable ? "Yes" : "No");

        var timeline = $("timeline");
        clear(timeline);
        var results2 = state.verification_results || [];
        if (!results2.length) {
          var empty = document.createElement("li");
          empty.textContent = "No verification attempts yet.";
          timeline.appendChild(empty);
        } else {
          results2.forEach(function (result, index) {
            var li = document.createElement("li");
            var seq = document.createElement("span");
            seq.className = "seq";
            seq.textContent = "#" + (index + 1);
            var word = document.createElement("span");
            word.className = "result " + result;
            word.textContent = result;
            li.appendChild(seq);
            li.appendChild(word);
            timeline.appendChild(li);
          });
        }

        var states = $("states");
        clear(states);
        (state.state_history || []).concat([state.state]).forEach(function (name, index) {
          var li = document.createElement("li");
          var seq = document.createElement("span");
          seq.className = "seq";
          seq.textContent = "#" + (index + 1);
          var word = document.createElement("span");
          word.textContent = name;
          li.appendChild(seq);
          li.appendChild(word);
          states.appendChild(li);
        });
      })
      .catch(function (error) {
        var parts = describe(error);
        showError(parts[0], parts[1]);
        setStatus("status", "waiting", "Not loaded", parts[1]);
      });
  }

  // ---------------------------------------------------------------- proof view

  /* DRIFTZERO canonical JSON: sorted keys, no insignificant whitespace, UTF-8. Written
   * out here rather than using JSON.stringify's default so the browser produces the same
   * bytes the Truth Engine hashed. */
  function canonical(value) {
    if (value === null || typeof value !== "object") return JSON.stringify(value);
    if (Array.isArray(value)) {
      return "[" + value.map(canonical).join(",") + "]";
    }
    var keys = Object.keys(value).sort();
    return (
      "{" +
      keys
        .map(function (key) {
          return JSON.stringify(key) + ":" + canonical(value[key]);
        })
        .join(",") +
      "}"
    );
  }

  function sha256Hex(input) {
    var bytes = new TextEncoder().encode(input);
    return global.crypto.subtle.digest("SHA-256", bytes).then(function (digest) {
      return Array.prototype.map
        .call(new Uint8Array(digest), function (b) {
          return b.toString(16).padStart(2, "0");
        })
        .join("");
    });
  }

  function initProof() {
    var workflowLink = $("go-workflow");
    if (workflowLink) workflowLink.href = link("workflow");

    request(API + "/workflows/" + encodeURIComponent(workflowId()) + "/proof")
      .then(function (payload) {
        var proof = payload.document || {};
        text("proof-state", "VERIFIED");
        text("headline", "Change deployed");
        text("subhead", "Change " + (proof.change_id || "—") + " is proven complete.");
        setStatus("status", "pass", "Verified",
          "The physical change was observed and adjudicated.");

        var facts = $("facts");
        clear(facts);
        fact(facts, "Source change", proof.change_id);
        fact(facts, "Source procedure", proof.source_procedure_id);
        fact(facts, "Source version", proof.source_version);
        fact(facts, "Affected work", proof.affected_artifact_id);
        if (proof.previous_value && proof.current_value) {
          fact(facts, "Change", proof.previous_value + " → " + proof.current_value);
        }
        fact(facts, "Delivery", proof.delivery_status);
        fact(facts, "Verification", proof.verification_result);
        fact(facts, "Completed", proof.completion_timestamp);
        fact(facts, "Proof id", proof.proof_id, true);
        fact(facts, "Proof content hash", proof.content_hash, true);

        var conditions = $("conditions");
        clear(conditions);
        var conditionList = payload.conditions || proof.completion_conditions || [];
        if (conditionList.length) {
          conditionList.forEach(function (item) {
            var li = document.createElement("li");
            var tick = document.createElement("span");
            tick.className = "tick";
            tick.textContent = "✔";
            var label = document.createElement("span");
            label.textContent = typeof item === "string" ? item : item.condition || "";
            li.appendChild(tick);
            li.appendChild(label);
            conditions.appendChild(li);
          });
        } else {
          var li = document.createElement("li");
          li.textContent = "All seven completion conditions held; a proof exists only then.";
          conditions.appendChild(li);
        }

        var manifest = $("manifest");
        clear(manifest);
        var evidence = proof.evidence_manifest || {};
        fact(manifest, "Source change ref", evidence.source_change_ref, true);
        fact(manifest, "Affected artifact ref", evidence.affected_artifact_ref, true);
        fact(manifest, "Delivery ref", evidence.delivery_ref, true);
        fact(
          manifest,
          "Verification refs",
          (evidence.verification_refs || []).join(", "),
          true
        );
        fact(
          manifest,
          "State chronology",
          (evidence.state_transition_refs || []).join(" → ")
        );

        var timeline = $("timeline");
        clear(timeline);
        (evidence.verification_refs || []).forEach(function (ref, index) {
          var li = document.createElement("li");
          var seq = document.createElement("span");
          seq.className = "seq";
          seq.textContent = "#" + (index + 1);
          var word = document.createElement("span");
          word.textContent = ref;
          li.appendChild(seq);
          li.appendChild(word);
          timeline.appendChild(li);
        });

        var canonicalText = payload.canonical_json || canonical(proof);
        text("canonical", canonicalText);

        var download = $("download");
        if (download) {
          download.href = URL.createObjectURL(
            new Blob([canonicalText], { type: "application/json" })
          );
          download.setAttribute(
            "download",
            "change-proof-" + (proof.proof_id || "driftzero") + ".json"
          );
        }

        var verify = $("verify-integrity");
        if (verify) {
          verify.addEventListener("click", function () {
            setStatus("integrity", "working", "Checking…", "Re-hashing the proof.");
            var material = {};
            Object.keys(proof).forEach(function (key) {
              // The hash preimage deliberately excludes content_hash: a document cannot
              // contain a hash of itself.
              if (key !== "content_hash") material[key] = proof[key];
            });
            sha256Hex(canonical(material))
              .then(function (computed) {
                if (computed === proof.content_hash) {
                  setStatus(
                    "integrity",
                    "pass",
                    "Content hash matches",
                    "Re-hashed in this browser: " + computed
                  );
                } else {
                  setStatus(
                    "integrity",
                    "fail",
                    "Content hash does NOT match",
                    "Recomputed " + computed + ", recorded " + proof.content_hash
                  );
                }
              })
              .catch(function () {
                setStatus(
                  "integrity",
                  "inconclusive",
                  "Could not verify here",
                  "This browser did not provide SHA-256. Verify with the recorded recipe."
                );
              });
          });
        }
      })
      .catch(function (error) {
        var parts = describe(error);
        showError(parts[0], parts[1]);
        text("headline", "No Change Proof");
        text("subhead", parts[1]);
        text("proof-state", "NOT COMPLETE");
        setStatus("status", "waiting", "Not complete", parts[1]);
        show("download", false);
        show("verify-integrity", false);
        show("inspector", false);
      });
  }

  global.DriftZeroProof = {
    initWorkflow: initWorkflow,
    initProof: initProof,
    _canonical: canonical,
    _describe: describe
  };
})(window);
