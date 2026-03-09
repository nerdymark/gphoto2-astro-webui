/**
 * API client for the gphoto2 Astro WebUI backend.
 */

const BASE = import.meta.env.VITE_API_BASE ?? "";

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, options);
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(text || res.statusText);
  }
  return res.json();
}

// Camera
export const getCameraStatus = () => request("/api/camera/status");
export const getExposure = () => request("/api/camera/exposure");
export const setExposure = (body) =>
  request("/api/camera/exposure", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
export const captureImage = (gallery) =>
  request("/api/camera/capture", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ gallery }),
  });
export const captureBurst = (gallery, count, interval = 0, bulbSeconds = null) =>
  request("/api/camera/burst", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      gallery,
      count,
      interval,
      ...(bulbSeconds != null && { bulb_seconds: bulbSeconds }),
    }),
  });

// Galleries
export const listGalleries = () => request("/api/galleries");
export const createGallery = (name) =>
  request("/api/galleries", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
export const getGallery = (name) => request(`/api/galleries/${encodeURIComponent(name)}`);
export const deleteImage = (gallery, filename) =>
  request(`/api/galleries/${encodeURIComponent(gallery)}/${encodeURIComponent(filename)}`, {
    method: "DELETE",
  });

// Stacking
export const stackImages = (gallery, images, mode, outputName) =>
  request(`/api/galleries/${encodeURIComponent(gallery)}/stack`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ images, mode, output_name: outputName }),
  });

export const imageUrl = (gallery, filename) =>
  `${BASE}/api/images/${encodeURIComponent(gallery)}/${encodeURIComponent(filename)}`;
