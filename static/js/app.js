/* =============================================================================
   Social Booster - progressive enhancements around HTMX.
   All server communication is done by HTMX attributes in the templates; this
   file only handles UI glue: toasts, drag & drop, previews, progress, local
   time formatting, counters and the theme toggle. No eval, no inline code.
   ========================================================================== */
(function () {
    "use strict";

    var TOAST_DELAY = { success: 4000, info: 4000, warning: 7000, danger: 9000 };

    // ------------------------------------------------------------- toasts
    function showToast(level, message) {
        if (!message) return;
        var container = document.getElementById("toast-container");
        if (!container || !window.bootstrap) return;
        var safeLevel = ["success", "info", "warning", "danger"].indexOf(level) >= 0 ? level : "info";

        var toast = document.createElement("div");
        toast.className = "toast align-items-center border-0 text-bg-" + safeLevel;
        toast.setAttribute("role", safeLevel === "danger" ? "alert" : "status");
        toast.setAttribute("aria-live", safeLevel === "danger" ? "assertive" : "polite");
        toast.setAttribute("aria-atomic", "true");

        var wrapper = document.createElement("div");
        wrapper.className = "d-flex";
        var body = document.createElement("div");
        body.className = "toast-body";
        body.textContent = message; // textContent: server/provider text is never parsed as HTML
        var close = document.createElement("button");
        close.type = "button";
        close.className = "btn-close me-2 m-auto" + (safeLevel === "warning" || safeLevel === "info" ? "" : " btn-close-white");
        close.setAttribute("data-bs-dismiss", "toast");
        close.setAttribute("aria-label", "Close");
        wrapper.appendChild(body);
        wrapper.appendChild(close);
        toast.appendChild(wrapper);
        container.appendChild(toast);

        var instance = window.bootstrap.Toast.getOrCreateInstance(toast, { delay: TOAST_DELAY[safeLevel] });
        toast.addEventListener("hidden.bs.toast", function () { toast.remove(); });
        instance.show();
    }
    window.appToast = showToast;

    document.body.addEventListener("app:toast", function (evt) {
        var detail = evt.detail || {};
        showToast(detail.level, detail.message);
    });

    function fallbackMessage(status) {
        if (status === 0) return "Network error. Check your connection and try again.";
        if (status === 403) return "Your session expired. Reload the page and try again.";
        if (status === 404) return "That item no longer exists. Reload the page.";
        if (status === 413) return "The upload is too large.";
        if (status === 429) return "Too many requests. Please wait a moment.";
        if (status >= 500) return "The server had a problem. Please try again.";
        return "The request failed (HTTP " + status + ").";
    }

    document.body.addEventListener("htmx:responseError", function (evt) {
        var xhr = evt.detail.xhr;
        // Errors from our server carry their own toast in HX-Trigger.
        if (xhr && xhr.getResponseHeader("HX-Trigger")) return;
        showToast("danger", fallbackMessage(xhr ? xhr.status : 0));
    });

    document.body.addEventListener("htmx:sendError", function () {
        showToast("danger", fallbackMessage(0));
    });

    // ---------------------------------------------------- upload: helpers
    var uploadForm = document.getElementById("upload-form");
    var fileInput = document.getElementById("file-input");
    var dropzone = document.getElementById("dropzone");
    var previews = document.getElementById("upload-previews");
    var objectUrls = [];

    function clearPreviews() {
        objectUrls.forEach(function (url) { URL.revokeObjectURL(url); });
        objectUrls = [];
        if (previews) previews.textContent = "";
    }

    function renderPreviews(files) {
        clearPreviews();
        files.forEach(function (file) {
            var figure = document.createElement("figure");
            figure.className = "upload-preview";
            var img = document.createElement("img");
            var url = URL.createObjectURL(file);
            objectUrls.push(url);
            img.src = url;
            img.alt = "Preview of " + file.name;
            figure.appendChild(img);
            previews.appendChild(figure);
        });
    }

    function formatMb(bytes) {
        return (bytes / (1024 * 1024)).toFixed(bytes % (1024 * 1024) === 0 ? 0 : 1);
    }

    // Capture phase on document runs *before* HTMX's listener on the input,
    // so invalid files are removed (or the upload cancelled) before sending.
    document.addEventListener("change", function (evt) {
        if (!uploadForm || evt.target !== fileInput) return;
        var maxFiles = parseInt(uploadForm.dataset.maxFiles, 10) || 10;
        var maxBytes = parseInt(uploadForm.dataset.maxBytes, 10) || 10485760;
        var accepted = (uploadForm.dataset.accept || "").split(",");
        var files = Array.prototype.slice.call(fileInput.files || []);
        var problems = [];

        var valid = files.filter(function (file) {
            if (accepted.indexOf(file.type) < 0) {
                problems.push(file.name + " (not a JPEG, PNG or WebP image)");
                return false;
            }
            if (file.size > maxBytes) {
                problems.push(file.name + " (larger than " + formatMb(maxBytes) + " MB)");
                return false;
            }
            return true;
        });
        if (valid.length > maxFiles) {
            problems.push("only the first " + maxFiles + " images were kept");
            valid = valid.slice(0, maxFiles);
        }
        if (problems.length) {
            showToast("warning", "Skipped: " + problems.join("; ") + ".");
        }
        if (!valid.length) {
            evt.stopImmediatePropagation();
            fileInput.value = "";
            clearPreviews();
            return;
        }
        if (valid.length !== files.length && window.DataTransfer) {
            var transfer = new DataTransfer();
            valid.forEach(function (file) { transfer.items.add(file); });
            fileInput.files = transfer.files;
        }
        renderPreviews(valid);
    }, true);

    if (dropzone && fileInput) {
        ["dragenter", "dragover"].forEach(function (name) {
            dropzone.addEventListener(name, function (evt) {
                evt.preventDefault();
                dropzone.classList.add("is-dragover");
            });
        });
        ["dragleave", "dragend", "drop"].forEach(function (name) {
            dropzone.addEventListener(name, function () { dropzone.classList.remove("is-dragover"); });
        });
        dropzone.addEventListener("drop", function (evt) {
            evt.preventDefault();
            if (!evt.dataTransfer || !evt.dataTransfer.files.length || fileInput.disabled) return;
            fileInput.files = evt.dataTransfer.files;
            fileInput.dispatchEvent(new Event("change", { bubbles: true }));
        });
        // Dropping outside the zone must not navigate away from the page.
        window.addEventListener("dragover", function (evt) { evt.preventDefault(); });
        window.addEventListener("drop", function (evt) { evt.preventDefault(); });
    }

    if (uploadForm) {
        var bar = uploadForm.querySelector(".progress-bar");
        var barWrapper = uploadForm.querySelector(".progress");
        var barValue = uploadForm.querySelector(".upload-progress-value");

        uploadForm.addEventListener("htmx:xhr:progress", function (evt) {
            if (!evt.detail.lengthComputable || !evt.detail.total) return;
            // Upload is ~90% of the wait; the rest is server-side analysis.
            var percent = Math.min(95, Math.round((evt.detail.loaded / evt.detail.total) * 90));
            bar.style.width = percent + "%";
            barWrapper.setAttribute("aria-valuenow", String(percent));
            barValue.textContent = percent + "%";
        });
        uploadForm.addEventListener("htmx:afterRequest", function () {
            bar.style.width = "0";
            barWrapper.setAttribute("aria-valuenow", "0");
            barValue.textContent = "0%";
            fileInput.value = "";
            clearPreviews();
        });
    }

    // ------------------------------------------------------ forms & keys
    document.addEventListener("submit", function (evt) {
        if (evt.target.matches(".draft-form, #upload-form")) evt.preventDefault();
    });

    document.addEventListener("keydown", function (evt) {
        if (evt.key !== "Enter" || !evt.target.matches(".draft-form input[name='keywords']")) return;
        evt.preventDefault();
        var button = evt.target.form.querySelector("button[hx-post$='/generate']");
        if (button && !button.disabled) button.click();
    });

    // ------------------------------------------------------- per-card init
    function pad(value) { return String(value).padStart(2, "0"); }

    function toLocalInputValue(date) {
        return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) +
            "T" + pad(date.getHours()) + ":" + pad(date.getMinutes());
    }

    var timezone = "UTC";
    try {
        timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch (e) {
        timezone = "UTC";
    }

    function updateCharCount(counter) {
        var field = document.getElementById(counter.dataset.for);
        if (!field) return;
        var max = parseInt(counter.dataset.max, 10);
        var length = field.value.length;
        counter.textContent = length + " / " + max;
        counter.classList.toggle("text-danger", length > max);
    }

    function updateTagCount(counter) {
        var field = document.getElementById(counter.dataset.for);
        if (!field) return;
        var max = parseInt(counter.dataset.max, 10);
        var count = field.value.split(/[\s,;]+/).filter(function (tag) { return tag.replace(/^#+/, "").length; }).length;
        counter.textContent = count + " / " + max + " tags";
        counter.classList.toggle("text-danger", count > max);
    }

    function initContent(root) {
        root.querySelectorAll(".js-timezone").forEach(function (input) {
            if (!input.value) input.value = timezone;
        });
        root.querySelectorAll(".js-tz-label").forEach(function (label) {
            label.textContent = "(" + timezone + ")";
        });

        var now = new Date();
        var minimum = new Date(now.getTime() + 2 * 60 * 1000);
        var suggestion = new Date(now.getTime() + 60 * 60 * 1000);
        suggestion.setMinutes(Math.ceil(suggestion.getMinutes() / 15) * 15, 0, 0);
        root.querySelectorAll(".js-schedule-input").forEach(function (input) {
            input.min = toLocalInputValue(minimum);
            if (!input.value) input.value = toLocalInputValue(suggestion);
        });

        root.querySelectorAll(".js-char-count").forEach(updateCharCount);
        root.querySelectorAll(".js-tag-count").forEach(updateTagCount);

        root.querySelectorAll("time.js-local-time:not([data-localized])").forEach(function (node) {
            var date = new Date(node.getAttribute("datetime"));
            if (isNaN(date.getTime())) return;
            node.title = node.textContent.trim();
            node.textContent = date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
            node.setAttribute("data-localized", "true");
        });
    }

    document.addEventListener("input", function (evt) {
        var id = evt.target.id;
        if (!id) return;
        document.querySelectorAll(".js-char-count[data-for='" + id + "']").forEach(updateCharCount);
        document.querySelectorAll(".js-tag-count[data-for='" + id + "']").forEach(updateTagCount);
    });

    // ------------------------------------------------------- image modal
    var modal = document.getElementById("image-modal");
    if (modal) {
        modal.addEventListener("show.bs.modal", function (evt) {
            var trigger = evt.relatedTarget;
            if (!trigger) return;
            var img = document.getElementById("image-modal-img");
            img.src = trigger.getAttribute("data-image-src") || "";
            img.alt = trigger.getAttribute("data-image-title") || "";
            document.getElementById("image-modal-title").textContent = img.alt || "Preview";
        });
        modal.addEventListener("hidden.bs.modal", function () {
            document.getElementById("image-modal-img").src = "";
        });
    }

    // -------------------------------------------------------------- theme
    var themeToggle = document.getElementById("theme-toggle");
    if (themeToggle) {
        themeToggle.addEventListener("click", function () {
            var next = document.documentElement.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
            document.documentElement.setAttribute("data-bs-theme", next);
            try {
                window.localStorage.setItem("mab-theme", next);
            } catch (e) {
                /* storage unavailable (private mode): theme simply isn't remembered */
            }
        });
    }

    // ------------------------------------------------------------ startup
    function start() {
        // initContent is idempotent: run it now and for every HTMX-inserted fragment.
        initContent(document);
        if (window.htmx) window.htmx.onLoad(initContent);
        var flashNode = document.getElementById("flash-data");
        if (flashNode) {
            try {
                JSON.parse(flashNode.textContent || "[]").forEach(function (flash) {
                    showToast(flash.level, flash.message);
                });
            } catch (e) {
                /* malformed flash payload: ignore */
            }
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();
