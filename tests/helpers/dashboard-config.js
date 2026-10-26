export function replaceDashboardConfig(html, updates) {
  let count = 0;
  const result = html.replace(/(<script type="application\/json" id="dashboard-config">)([\s\S]*?)(<\/script>)/, (match, start, body, end) => {
    count += 1;
    const config = Object.assign(JSON.parse(body), updates);
    const encoded = JSON.stringify(config).replace(/</g, '\\u003c').replace(/>/g, '\\u003e').replace(/&/g, '\\u0026');
    return start + encoded + end;
  });
  if (count !== 1) throw new Error('Dashboard configuration fixture was not found');
  return result;
}
