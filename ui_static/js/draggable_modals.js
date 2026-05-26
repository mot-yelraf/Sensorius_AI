(function () {
  if (window.DraggableModals) return;

  const MODAL_SELECTOR = ".modal, .system-settings-shell, .onboard-modal";
  const HANDLE_SELECTOR = ".modal-header, .system-settings-header, .onboard-title";
  const INTERACTIVE_SELECTOR = "button, a, input, select, textarea, label, [contenteditable='true'], [data-no-drag]";
  const MIN_VISIBLE = 56;

  let active = null;

  function clamp(value, min, max) {
    if (min > max) return min;
    return Math.min(Math.max(value, min), max);
  }

  function clampPosition(modal, left, top) {
    const rect = modal.getBoundingClientRect();
    const minLeft = MIN_VISIBLE - rect.width;
    const maxLeft = window.innerWidth - MIN_VISIBLE;
    const minTop = 0;
    const maxTop = window.innerHeight - MIN_VISIBLE;

    return {
      left: clamp(left, minLeft, maxLeft),
      top: clamp(top, minTop, maxTop),
    };
  }

  function positionModal(modal, left, top) {
    const next = clampPosition(modal, left, top);
    modal.style.position = "fixed";
    modal.style.left = `${next.left}px`;
    modal.style.top = `${next.top}px`;
    modal.style.margin = "0";
    modal.classList.add("sai-draggable-positioned");
  }

  function onPointerDown(ev) {
    if (ev.button !== 0) return;

    const handle = ev.target.closest(HANDLE_SELECTOR);
    if (!handle) return;
    if (ev.target.closest(INTERACTIVE_SELECTOR)) return;

    const modal = handle.closest(MODAL_SELECTOR);
    if (!modal || !document.body.contains(modal)) return;

    const rect = modal.getBoundingClientRect();
    modal.style.width = `${rect.width}px`;
    positionModal(modal, rect.left, rect.top);

    active = {
      modal,
      offsetX: ev.clientX - rect.left,
      offsetY: ev.clientY - rect.top,
    };

    modal.classList.add("sai-modal-dragging");
    document.documentElement.classList.add("sai-modal-drag-active");
    ev.preventDefault();
  }

  function onPointerMove(ev) {
    if (!active) return;
    positionModal(active.modal, ev.clientX - active.offsetX, ev.clientY - active.offsetY);
    ev.preventDefault();
  }

  function endDrag() {
    if (!active) return;
    active.modal.classList.remove("sai-modal-dragging");
    document.documentElement.classList.remove("sai-modal-drag-active");
    active = null;
  }

  function reclampPositionedModals() {
    document.querySelectorAll(".sai-draggable-positioned").forEach((modal) => {
      const rect = modal.getBoundingClientRect();
      positionModal(modal, rect.left, rect.top);
    });
  }

  document.addEventListener("pointerdown", onPointerDown);
  document.addEventListener("pointermove", onPointerMove);
  document.addEventListener("pointerup", endDrag);
  document.addEventListener("pointercancel", endDrag);
  window.addEventListener("resize", reclampPositionedModals);

  window.DraggableModals = {
    reclamp: reclampPositionedModals,
  };
})();
