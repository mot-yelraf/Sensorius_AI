/* Mobile settings navigation keeps the original forms and save handlers in place. */
"use strict";

(() => {
if (window.initMobileSettings) return;
const phoneQuery = "(max-width: 700px), (max-device-width: 700px)";
const viewportOwners = new Set();
let settingsViewport = null;
window.setMobileWorkspaceViewport = function setMobileWorkspaceViewport(root, open, query = phoneQuery) {
  if (!root) return;
  if (open) viewportOwners.add(root);
  else viewportOwners.delete(root);
  for (const owner of viewportOwners) {
    if (!owner.isConnected) viewportOwners.delete(owner);
  }
  if (viewportOwners.size && window.matchMedia(query).matches) {
    if (!document.querySelector('meta[name="viewport"]')) {
      settingsViewport = document.createElement("meta");
      settingsViewport.name = "viewport";
      settingsViewport.content = "width=device-width, initial-scale=1";
      document.head.append(settingsViewport);
    }
  } else if (!viewportOwners.size && settingsViewport) {
    // Reset the viewport directives first; removing the tag alone retains its scale.
    settingsViewport.content = "";
    settingsViewport.remove();
    settingsViewport = null;
  }
};

window.initMobileSettings = function initMobileSettings(root) {
  if (!root || root.__mobileSettings) return root?.__mobileSettings;
  const system = root.id === "setupPiModal";
  const menu = root.querySelector(system ? ".system-settings-menu" : ".switch-dialog-menu");
  const content = root.querySelector(system ? ".system-settings-content" : ".split > section.panel");
  const body = root.querySelector(system ? ".system-settings-body" : ".modal-body");
  if (!menu || !content || !body) return null;
  window.setMobileWorkspaceViewport(root, true);
  const media = window.matchMedia(phoneQuery);
  const paneSelector = ".settings-pane, .sensor-pane-view, .switch-pane";
  const nav = document.createElement("nav");
  nav.className = "mobile-settings-nav";
  nav.setAttribute("aria-label", "Settings navigation");
  const back = document.createElement("button");
  back.type = "button";
  back.textContent = "< Back";
  const title = document.createElement("span");
  title.className = "mobile-settings-title";
  nav.append(back, title);
  body.before(nav);
  menu.classList.add("mobile-settings-menu");
  content.classList.add("mobile-settings-content");
  [...menu.querySelectorAll("button"), ...content.querySelectorAll("details > summary")].forEach((item) => {
    item.dataset.mobileLabel = item.textContent.trim();
    const chevron = document.createElement("span");
    chevron.className = "mobile-settings-chevron";
    chevron.setAttribute("aria-hidden", "true");
    item.append(chevron);
  });
  let pane = null;
  let trigger = null;
  let path = [];
  let desktopOpen = new Map();

  function clearPath() {
    root.querySelectorAll(".mobile-settings-hidden, .mobile-settings-current, .mobile-settings-ancestor").forEach((element) => {
      element.classList.remove("mobile-settings-hidden", "mobile-settings-current", "mobile-settings-ancestor");
    });
  }

  function render(focusTarget) {
    clearPath();
    root.classList.toggle("mobile-settings-root", !pane);
    back.hidden = !pane;
    title.textContent = path.length
      ? path[path.length - 1].querySelector(":scope > summary").dataset.mobileLabel
      : (pane ? trigger?.dataset.mobileLabel || "Settings" : "Settings");
    if (media.matches) {
      content.querySelectorAll("details").forEach((detail) => { detail.open = path.includes(detail); });
      if (path.length) {
        const current = path[path.length - 1];
        current.classList.add("mobile-settings-current");
        // Hide sibling branches without moving inputs outside their owning form.
        for (let branch = current; branch && branch !== pane; branch = branch.parentElement) {
          for (const sibling of branch.parentElement.children) {
            if (sibling !== branch && !sibling.matches(".pane-footer, .sai-live-status, input[type='hidden']")) {
              sibling.classList.add("mobile-settings-hidden");
            }
          }
          if (branch !== current && branch.matches("details")) branch.classList.add("mobile-settings-ancestor");
        }
      }
    }
    body.scrollTop = 0;
    content.scrollTop = 0;
    if (pane) pane.scrollTop = 0;
    if (focusTarget) focusTarget.focus({ preventScroll: true });
  }

  function reset() {
    pane = null;
    trigger = null;
    path = [];
    render();
  }

  root.addEventListener("click", (event) => {
    if (!media.matches) return;
    const button = event.target.closest(".system-menu-btn, #sensorSettingsMenu [data-pane-target], #switchMenuSettings, #switchMenuStatistics");
    if (button && menu.contains(button)) {
      const target = button.dataset.target || button.dataset.paneTarget ||
        (button.id === "switchMenuSettings" ? "switchSettingsPane" : "switchStatisticsPane");
      pane = root.querySelector("#" + target);
      trigger = button;
      path = [];
      render(back);
      return;
    }
    const summary = event.target.closest("summary");
    if (!summary || !content.contains(summary) || !summary.closest(paneSelector)) return;
    event.preventDefault();
    const detail = summary.parentElement;
    if (path.includes(detail)) return;
    pane = summary.closest(paneSelector);
    path.push(detail);
    render(back);
  });

  back.addEventListener("click", () => {
    let focusTarget;
    if (path.length) focusTarget = path.pop().querySelector(":scope > summary");
    else {
      focusTarget = trigger;
      pane = null;
    }
    render(focusTarget);
  });

  function resize() {
    root.classList.toggle("mobile-settings", media.matches);
    if (media.matches) {
      desktopOpen = new Map(Array.from(content.querySelectorAll("details"), detail => [detail, detail.open]));
      reset();
    } else {
      clearPath();
      desktopOpen.forEach((open, detail) => { detail.open = open; });
    }
  }
  // A shared resize dispatcher avoids retaining removed device dialogs.
  root.__mobileSettings = { reset, resize };
  resize();
  return root.__mobileSettings;
};

window.matchMedia(phoneQuery).addEventListener("change", () => {
  document.querySelectorAll("#setupPiModal, #sensorSettingsModal, #switchSettingsModal").forEach((root) => {
    root.__mobileSettings?.resize();
  });
});
})();
