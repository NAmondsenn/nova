// The night sky behind Nova's pages - the settings page and the phone's approval page - with
// the same stars every time, from the same seed. It sets one global, NovaSky, and is loaded
// before the page's own script.

"use strict";

(() => {
  // Random numbers, the same ones each time for the same seed - mulberry32.
  const seeded = (seed) => {
    return () => {
      seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  };

  // Draws the sky on the canvas, unless it is already drawn at its size.
  const draw = (canvas) => {
    const { width, height } = canvas.getBoundingClientRect();
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    if (canvas.width === Math.round(width * ratio) && canvas.height === Math.round(height * ratio)) return;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const sky = canvas.getContext("2d");
    sky.scale(ratio, ratio);

    const night = sky.createLinearGradient(0, 0, 0, height);
    night.addColorStop(0, "#0a1024");
    night.addColorStop(1, "#04060e");
    sky.fillStyle = night;
    sky.fillRect(0, 0, width, height);

    // A faint band of haze across it, like the Milky Way, with more stars in it.
    const band = { x: width * 0.62, y: height * 0.3, angle: -0.42, length: Math.hypot(width, height),
                   across: Math.min(width, height) * 0.22 };
    sky.save();
    sky.translate(band.x, band.y);
    sky.rotate(band.angle);
    sky.scale(1, (band.across * 2) / band.length);
    const haze = sky.createRadialGradient(0, 0, 0, 0, 0, band.length / 2);
    haze.addColorStop(0, "rgba(120, 140, 210, 0.1)");
    haze.addColorStop(0.5, "rgba(90, 100, 180, 0.05)");
    haze.addColorStop(1, "rgba(90, 100, 180, 0)");
    sky.fillStyle = haze;
    sky.fillRect(-band.length / 2, -band.length / 2, band.length, band.length);
    sky.restore();

    // And a little warmth low down, as if from a town far off.
    const warmth = sky.createRadialGradient(width * 0.1, height, 0, width * 0.1, height, Math.max(width, height) * 0.5);
    warmth.addColorStop(0, "rgba(130, 90, 140, 0.08)");
    warmth.addColorStop(1, "rgba(130, 90, 140, 0)");
    sky.fillStyle = warmth;
    sky.fillRect(0, 0, width, height);

    const random = seeded(2026);
    const stars = Math.round((width * height) / 2400);
    for (let i = 0; i < stars; i++) {
      let x = random() * width;
      let y = random() * height;
      if (i % 3 === 0) {
        // In the band: anywhere along it, and mostly near its middle across it.
        const along = (random() - 0.5) * band.length;
        const across = (random() + random() + random() - 1.5) * band.across * 0.9;
        x = band.x + along * Math.cos(band.angle) - across * Math.sin(band.angle);
        y = band.y + along * Math.sin(band.angle) + across * Math.cos(band.angle);
      }
      const size = 0.3 + Math.pow(random(), 4) * 1.4;
      const alpha = (0.2 + random() * 0.65).toFixed(2);
      const tint = random();
      sky.fillStyle = tint < 0.12 ? `rgba(255, 226, 186, ${alpha})`
        : tint > 0.88 ? `rgba(186, 206, 255, ${alpha})` : `rgba(236, 240, 255, ${alpha})`;
      sky.beginPath();
      sky.arc(x, y, size, 0, Math.PI * 2);
      sky.fill();
    }

    // A few bright ones, with a soft glow.
    for (let i = 0; i < Math.round(stars / 120); i++) {
      const x = random() * width;
      const y = random() * height;
      const glow = sky.createRadialGradient(x, y, 0, x, y, 5);
      glow.addColorStop(0, "rgba(255, 248, 235, 0.9)");
      glow.addColorStop(0.25, "rgba(243, 214, 160, 0.3)");
      glow.addColorStop(1, "rgba(243, 214, 160, 0)");
      sky.fillStyle = glow;
      sky.fillRect(x - 5, y - 5, 10, 10);
    }
  };

  globalThis.NovaSky = { draw };
})();
