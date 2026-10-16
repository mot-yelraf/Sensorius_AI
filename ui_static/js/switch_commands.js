/* Keep requested switch commands separate from device-confirmed UI state. */
(() => {
  'use strict';

  function refresh(button) {
    const busy = button.dataset.commandRequest === '1' || button.dataset.commandWaiting === '1';
    button.classList.toggle('switch-pending', busy);
    button.disabled = busy;
    if (busy) button.setAttribute('aria-busy', 'true');
    else button.removeAttribute('aria-busy');
  }

  function apply(button, data) {
    const status = button.parentElement?.querySelector('.switch-command-status');
    const explicit = Object.prototype.hasOwnProperty.call(data, 'pending');
    let waiting = button.dataset.commandWaiting === '1';
    if (explicit) waiting = data.pending === true;
    else if (waiting && data.confirmed !== false && String(Boolean(data.state)) === button.dataset.requestedState) waiting = false;
    clearTimeout(button.switchCommandTimeout);
    button.dataset.commandWaiting = waiting ? '1' : '0';
    if (waiting) {
      if (explicit) button.dataset.requestedState = String(Boolean(data.requested_state));
      if (status) status.textContent = `Waiting for device: ${button.dataset.requestedState === 'true' ? 'On' : 'Off'}`;
      const until = data.pending_until ? Number(data.pending_until) * 1000 : Number(button.dataset.pendingUntil) || Date.now() + 45000;
      if (until <= Date.now()) {
        apply(button, { pending: false, command_error: 'Device did not confirm the switch change.' });
        return;
      }
      button.dataset.pendingUntil = String(until);
      button.switchCommandTimeout = setTimeout(() => apply(button, {
        pending: false, command_error: 'Device did not confirm the switch change.',
      }), Math.max(0, until - Date.now()));
    } else {
      if (status) status.textContent = data.command_error || data.command_warning || '';
      delete button.dataset.pendingUntil;
      delete button.dataset.requestedState;
    }
    if (status) status.classList.toggle('switch-command-error', Boolean(data.command_error));
    refresh(button);
  }

  window.SwitchCommands = {
    apply,
    begin(button) { button.dataset.commandRequest = '1'; refresh(button); },
    finish(button) { delete button.dataset.commandRequest; refresh(button); },
  };
})();
