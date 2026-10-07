/* Applies the saved or system colour theme before first paint. */
(function () {
    "use strict";
    var stored = null;
    try {
        stored = window.localStorage.getItem("mab-theme");
    } catch (e) {
        stored = null;
    }
    var prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    var theme = stored === "light" || stored === "dark" ? stored : prefersDark ? "dark" : "light";
    document.documentElement.setAttribute("data-bs-theme", theme);
})();
