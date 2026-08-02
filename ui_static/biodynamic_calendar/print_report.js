const params = new URLSearchParams(window.location.search);
const key = params.get("key") || "";
const target = document.getElementById("stagedPrintReport");
const printButton = document.getElementById("printWindowBtn");
let reportLoaded = false;

if (key) {
  try {
    const stored = window.localStorage.getItem(key);
    window.localStorage.removeItem(key);
    const report = stored ? JSON.parse(stored) : null;
    if (report && typeof report.html === "string") {
      target.innerHTML = report.html;
      document.title = String(report.title || "Biodynamic Calendar Report");
      reportLoaded = true;
    }
  } catch (err) {
    reportLoaded = false;
  }
}

if (!reportLoaded) {
  target.innerHTML = '<p class="print-report-error">This report is no longer available. Return to the calendar and select Report again.</p>';
}

function printReport() {
  if (reportLoaded) window.print();
}

printButton.addEventListener("click", printReport);
window.addEventListener("load", () => {
  if (reportLoaded) window.setTimeout(printReport, 0);
}, { once: true });
