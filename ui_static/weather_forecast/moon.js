(function () {
  const moonSurfaceImage = new Image();
  const moonRenders = new Map();

  function renderMoonDisk(canvas, moon) {
    if (!canvas) return;
    moonRenders.set(canvas, moon);
    const context = canvas.getContext("2d");
    if (!context) return;

    const canonicalIllumination = [0, 15, 50, 85, 100, 85, 50, 15];
    const phaseIndex = Number(moon.index ?? canvas.dataset.phaseIndex);
    let illuminationPercent = Number(moon.illumination);
    if (!Number.isFinite(illuminationPercent)
      || (canvas.hasAttribute("data-phase-moon") && phaseIndex > 0 && illuminationPercent === 0)) {
      illuminationPercent = canonicalIllumination[phaseIndex] ?? 0;
    }
    const illumination = Math.max(0, Math.min(100, illuminationPercent)) / 100;
    const angle = Number(moon.bright_limb_angle || 0) * Math.PI / 180;
    const diskRotation = Number(moon.disk_rotation || 0) * Math.PI / 180;
    const rotationCosine = Math.cos(diskRotation);
    const rotationSine = Math.sin(diskRotation);
    const width = canvas.width;
    const height = canvas.height;
    const centerX = width / 2;
    const centerY = height / 2;
    const radius = Math.min(width, height) * 0.455;
    const lightDepth = 2 * illumination - 1;
    const lightAcross = Math.sqrt(Math.max(0, 1 - lightDepth * lightDepth));
    const lightX = Math.sin(angle) * lightAcross;
    const lightY = Math.cos(angle) * lightAcross;
    const pixels = context.createImageData(width, height);
    let surfacePixels = null;
    if (moonSurfaceImage.complete && moonSurfaceImage.naturalWidth > 0) {
      const surfaceCanvas = document.createElement("canvas");
      surfaceCanvas.width = width;
      surfaceCanvas.height = height;
      const surfaceContext = surfaceCanvas.getContext("2d", {willReadFrequently: true});
      if (surfaceContext) {
        surfaceContext.drawImage(moonSurfaceImage, 0, 0, width, height);
        surfacePixels = surfaceContext.getImageData(0, 0, width, height).data;
      }
    }

    for (let pixelY = 0; pixelY < height; pixelY += 1) {
      for (let pixelX = 0; pixelX < width; pixelX += 1) {
        const x = (pixelX + 0.5 - centerX) / radius;
        const y = (centerY - pixelY - 0.5) / radius;
        const distanceSquared = x * x + y * y;
        if (distanceSquared > 1.025) continue;
        const z = Math.sqrt(Math.max(0, 1 - Math.min(1, distanceSquared)));
        const sunlight = x * lightX + y * lightY + z * lightDepth;
        const terminator = Math.max(0, Math.min(1, (sunlight + 0.018) / 0.036));
        const texture = 0.92 + 0.045 * Math.sin(pixelX * 0.31 + pixelY * 0.17)
          + 0.025 * Math.sin(pixelX * 0.08 - pixelY * 0.23);
        const index = (pixelY * width + pixelX) * 4;
        const textureX = rotationCosine * x - rotationSine * y;
        const textureY = rotationSine * x + rotationCosine * y;
        const sourceX = Math.max(0, Math.min(width - 1, Math.round(centerX + textureX * radius)));
        const sourceY = Math.max(0, Math.min(height - 1, Math.round(centerY - textureY * radius)));
        const sourceIndex = (sourceY * width + sourceX) * 4;
        const sourceRed = surfacePixels ? surfacePixels[sourceIndex] : Math.round(224 * texture);
        const sourceGreen = surfacePixels ? surfacePixels[sourceIndex + 1] : Math.round(211 * texture);
        const sourceBlue = surfacePixels ? surfacePixels[sourceIndex + 2] : Math.round(170 * texture);
        const brightness = 0.035 + terminator * (0.9 + z * 0.065);
        pixels.data[index] = Math.min(255, Math.round(sourceRed * brightness));
        pixels.data[index + 1] = Math.min(255, Math.round(sourceGreen * brightness));
        pixels.data[index + 2] = Math.min(255, Math.round(sourceBlue * brightness + (1 - terminator) * 5));
        pixels.data[index + 3] = distanceSquared <= 1 ? 255 : Math.round((1.025 - distanceSquared) / 0.025 * 255);
      }
    }
    context.putImageData(pixels, 0, 0);
    context.beginPath();
    context.arc(centerX, centerY, radius, 0, Math.PI * 2);
    context.strokeStyle = "rgba(255, 240, 198, 0.28)";
    context.lineWidth = 1.5;
    context.stroke();
    canvas.setAttribute("aria-label", `Observer-local view of the ${moon.name}, ${illuminationPercent} percent illuminated`);
    canvas.title = moon.representative_date
      ? `${moon.name} · local view near lunar transit on ${moon.representative_date}`
      : `${moon.name} · local view now`;
  }

  moonSurfaceImage.addEventListener("load", () => {
    moonRenders.forEach((moon, canvas) => renderMoonDisk(canvas, moon));
  });
  moonSurfaceImage.src = "/ui_static/weather_forecast/moon-surface.png?v=1";
  window.CaelusMoon = Object.freeze({renderMoonDisk});
})();
