const MAX_BYTES = 4 * 1024 * 1024;
const pendingImports = new WeakMap();

async function requestJSON(url, options = {}) {
  const response = await fetch(url, {...options, cache: "no-store"});
  let body;
  try {
    body = await response.json();
  } catch {
    throw new Error("Sensorius did not return a valid response. Try again.");
  }
  if (!response.ok) throw new Error(body.error || "Automation file request failed.");
  return body;
}

async function nativeAPI() {
  if (!window.pywebview) return null;
  if (!window.pywebview.api?.open_automation_import) {
    await new Promise(resolve => {
      const done = () => { clearTimeout(timer); window.removeEventListener("pywebviewready", done); resolve(); };
      const timer = setTimeout(done, 2000);
      window.addEventListener("pywebviewready", done, {once: true});
    });
  }
  const api = window.pywebview.api;
  if (!api?.open_automation_import || !api?.save_automation_export) throw new Error("Native file dialogs are unavailable. Reopen the desktop window and try again.");
  return api;
}

async function openFile(api) {
  if (api) {
    const result = await api.open_automation_import();
    if (result.error) throw new Error(result.error);
    return result.cancelled ? null : result;
  }
  let file;
  if (window.showOpenFilePicker) {
    const [handle] = await window.showOpenFilePicker({multiple: false, types: [{description: "Sensorius automation JSON", accept: {"application/json": [".json"]}}]});
    file = await handle.getFile();
  } else {
    file = await new Promise(resolve => {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = ".json,application/json";
      input.hidden = true;
      const finish = file => { input.remove(); resolve(file); };
      input.addEventListener("change", () => finish(input.files?.[0] || null), {once: true});
      input.addEventListener("cancel", () => finish(null), {once: true});
      document.body.append(input);
      input.click();
    });
  }
  if (!file) return null;
  if (!file.name.toLowerCase().endsWith(".json")) throw new Error("Choose a Sensorius automation .json file.");
  if (file.size > MAX_BYTES) throw new Error("Automation file is too large (maximum 4 MiB).");
  let content;
  try {
    content = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer());
  } catch {
    throw new Error("Choose a UTF-8 JSON automation file.");
  }
  return {name: file.name, content};
}

async function saveFile(api, content, filename) {
  if (api) {
    const result = await api.save_automation_export(content, filename);
    if (result.error) throw new Error(result.error);
    return result.cancelled ? null : result.name;
  }
  if (window.showSaveFilePicker) {
    const handle = await window.showSaveFilePicker({suggestedName: filename, types: [{description: "Sensorius automation JSON", accept: {"application/json": [".json"]}}]});
    const writable = await handle.createWritable();
    try {
      await writable.write(content);
      await writable.close();
    } catch (error) {
      await writable.abort().catch(() => {});
      throw error;
    }
    return handle.name;
  }
  const url = URL.createObjectURL(new Blob([content], {type: "application/json;charset=utf-8"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return filename;
}

export async function runAutomationTransfer(modal, operation, reload) {
  if (modal.dataset.automationTransferBusy === "1") return;
  modal.dataset.automationTransferBusy = "1";
  const status = modal.querySelector("#automationArrangeStatus");
  const buttons = [...modal.querySelectorAll("#automationChooser button")].map(button => [button, button.disabled]);
  buttons.forEach(([button]) => { button.disabled = true; });
  const report = message => { if (status) status.textContent = message; };
  try {
    const api = await nativeAPI();
    if (operation === "export") {
      report("Preparing export…");
      const data = await requestJSON("/advanced/automations/export");
      const name = await saveFile(api, data.content, data.filename);
      report(name ? `Exported ${name}.` : "Export cancelled.");
      return;
    }
    let pending = pendingImports.get(modal);
    if (!pending) {
      const file = await openFile(api);
      if (!file) { report("Import cancelled."); return; }
      pending = {...file, batch: crypto.randomUUID()};
      pendingImports.set(modal, pending);
    }
    report("Validating automation file…");
    let preview;
    try {
      preview = await requestJSON("/advanced/automations/import/preview", {method: "POST", headers: {"Content-Type": "application/json"}, body: pending.content});
    } catch (error) {
      pendingImports.delete(modal);
      throw error;
    }
    if (!window.confirm(`Import ${preview.automation_count} automations in ${preview.group_count} groups from “${pending.name}”?\n\nImported copies start disabled. Existing automations remain in place. Review sensor and switch bindings before enabling.`)) {
      pendingImports.delete(modal);
      report("Import cancelled.");
      return;
    }
    report("Importing disabled copies…");
    const result = await requestJSON("/advanced/automations/import", {method: "POST", headers: {"Content-Type": "application/json", "X-Automation-Import-Batch": pending.batch}, body: pending.content});
    await reload();
    pendingImports.delete(modal);
    report(`Imported ${result.rule_ids.length} disabled automations. Review bindings before enabling.`);
  } catch (error) {
    if (error.name === "AbortError") {
      report("File selection cancelled.");
    } else {
      const retry = pendingImports.has(modal) ? " Click Import to retry the same file." : "";
      report((error.message || "Automation file operation failed.") + retry);
    }
  } finally {
    buttons.forEach(([button, disabled]) => { button.disabled = disabled; });
    delete modal.dataset.automationTransferBusy;
  }
}
