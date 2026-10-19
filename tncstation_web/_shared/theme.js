/*
 * tncstation theme picker -- shared by every page (inlined by load_page()
 * into <head>, before the page's own style and script).
 *
 * Runs before the page draws, so a saved theme never flashes the
 * default first. Sets <html data-theme="terminal|light|night"> (the
 * palette theme.css applies) and data-theme-choice (what was picked,
 * which may be "auto"). Adds a small <select> to the page header.
 *
 * The choice is kept per browser in localStorage, so a phone and a
 * laptop can use different themes. Storage can be unavailable (private
 * windows, blocked site data); the page then just uses Terminal.
 */
(function () {
  "use strict";

  var KEY = "tncstation.theme";
  var THEMES = [
    ["terminal", "Terminal"],
    ["light", "Light"],
    ["night", "Night (red)"],
    ["auto", "Auto"]
  ];
  var DEFAULT = "terminal";

  var root = document.documentElement;
  var lightQuery = window.matchMedia
    ? window.matchMedia("(prefers-color-scheme: light)")
    : null;

  function isTheme(value) {
    for (var i = 0; i < THEMES.length; i++) {
      if (THEMES[i][0] === value) return true;
    }
    return false;
  }

  function load() {
    try {
      var value = window.localStorage.getItem(KEY);
      return isTheme(value) ? value : DEFAULT;
    } catch (e) {
      return DEFAULT;
    }
  }

  function save(value) {
    try { window.localStorage.setItem(KEY, value); } catch (e) { /* per-visit only */ }
  }

  // "auto" follows the device: Light when it asks for light, else Terminal.
  function resolve(choice) {
    if (choice !== "auto") return choice;
    return lightQuery && lightQuery.matches ? "light" : "terminal";
  }

  var choice = load();

  function apply() {
    root.setAttribute("data-theme-choice", choice);
    root.setAttribute("data-theme", resolve(choice));

    var pickers = document.querySelectorAll("select.themePicker");
    for (var i = 0; i < pickers.length; i++) pickers[i].value = choice;
  }

  function set(value) {
    if (!isTheme(value)) return;
    choice = value;
    save(value);
    apply();
  }

  apply();

  if (lightQuery) {
    var onDeviceChange = function () { if (choice === "auto") apply(); };
    if (lightQuery.addEventListener) lightQuery.addEventListener("change", onDeviceChange);
    else if (lightQuery.addListener) lightQuery.addListener(onDeviceChange);
  }

  // Another tab changed the theme: follow it.
  window.addEventListener("storage", function (event) {
    if (event.key === KEY) {
      choice = load();
      apply();
    }
  });

  function addPicker() {
    var header = document.querySelector(".paneHeader");
    if (!header || header.querySelector("select.themePicker")) return;

    var picker = document.createElement("select");
    picker.className = "themePicker";
    picker.title = "Theme";
    picker.setAttribute("aria-label", "Theme");

    for (var i = 0; i < THEMES.length; i++) {
      var option = document.createElement("option");
      option.value = THEMES[i][0];
      option.textContent = THEMES[i][1];
      picker.appendChild(option);
    }

    picker.value = choice;
    picker.addEventListener("change", function () { set(picker.value); });

    // Beside the page title (the header's first item): the same spot on
    // every page, and the right-hand side is already full on some (map).
    var title = header.firstElementChild;
    if (title && title.tagName === "SPAN") title.appendChild(picker);
    else header.insertBefore(picker, header.firstChild);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", addPicker);
  } else {
    addPicker();
  }

  window.tncstationTheme = {
    get: function () { return choice; },
    resolved: function () { return resolve(choice); },
    set: set,
    themes: THEMES.map(function (t) { return t[0]; })
  };
})();
